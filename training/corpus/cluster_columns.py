"""
gen20 — SCALED clustering over ALL ~100k local CSVs (not a 2.5k sample). Streaming read so memory stays flat;
bge-small (GPU if available) embeds each column's "header: values"; MiniBatchKMeans (NOT O(n^2) agglomerative) clusters
the ~278k column vectors; each cluster named by its most frequent non-stop header. Writes columns.csv + clusters.json.

Run OVERNIGHT (offline). On CPU this is hours (mostly the 100k file reads + 278k bge encodes); on a GPU box it is
~minutes for the encode. After it finishes, run the LLM renamer (see offline-job.md) then reconcile_taxonomy.

  $env:PYTHONUTF8=1; python -m training.corpus.cluster_columns [--max-files N] [--k 1500]
"""
from __future__ import annotations
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.embedder import Embedder, _MODEL_ID                            # noqa: E402
from training.corpus.build_review import read_cols, name_like, snake       # noqa: E402
from training.corpus.discover_csv_types import SCAN                        # noqa: E402

OUT = ROOT / "training/data"
STOP = set("id ids uuid hash index idx key code type status note time date timestamp source dataset model url label "
           "count total sum avg min max amount price qty number percent rate flag value".split())


def gpu_encoder():
    """bge-small on CUDA if available (else the CPU Embedder). sentence-transformers-free: raw HF, mean-pool [CLS]."""
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
        if not torch.cuda.is_available():
            return None
        tok = AutoTokenizer.from_pretrained(_MODEL_ID)
        mdl = AutoModel.from_pretrained(_MODEL_ID).half().cuda().eval()

        def enc(texts, batch=512):
            out = np.zeros((len(texts), mdl.config.hidden_size), np.float32)
            for i in range(0, len(texts), batch):
                ch = texts[i:i + batch]
                t = tok(ch, return_tensors="pt", padding=True, truncation=True, max_length=64).to("cuda")
                with torch.no_grad():
                    h = mdl(**t).last_hidden_state[:, 0]
                    h = torch.nn.functional.normalize(h, p=2, dim=1)
                out[i:i + len(ch)] = h.float().cpu().numpy()
            return out
        print("using GPU bge encoder", flush=True)
        return enc
    except Exception as e:                                                  # noqa: BLE001
        print("no GPU encoder:", repr(e)[:80], flush=True)
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-files", type=int, default=0)                     # 0 = ALL files
    ap.add_argument("--k", type=int, default=1500)                          # MiniBatchKMeans clusters
    args = ap.parse_args()

    files = []
    with open(SCAN, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:                                              # noqa: BLE001
                continue
            sc = [c for c, t in (d.get("col_types") or {}).items() if t == "string"]
            if sc:
                files.append((d["hexsha"], sc))
    if args.max_files:
        files = files[:args.max_files]
    print(f"{len(files)} files; reading columns (streaming)...", flush=True)

    cols = []
    for n, (hx, sc) in enumerate(files):
        cv = read_cols(hx, sc[:4])
        for c in sc[:4]:
            vals = [v for v in cv.get(c, []) if name_like(v)]
            if len(vals) >= 4:
                cols.append((str(c), vals[:8]))
        if n % 5000 == 0:
            print(f"  {n}/{len(files)} files, {len(cols)} columns", flush=True)
    print(f"{len(cols)} columns total; encoding with bge...", flush=True)

    enc = gpu_encoder()
    texts = [f"{c}: " + "; ".join(v) for c, v in cols]
    X = (enc(texts) if enc else Embedder().encode(texts)).astype(np.float32)
    X /= (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)

    from sklearn.cluster import MiniBatchKMeans
    print(f"MiniBatchKMeans(k={args.k}) over {len(X)} vectors...", flush=True)
    lab = MiniBatchKMeans(n_clusters=args.k, batch_size=4096, n_init=3, random_state=0).fit_predict(X)

    clusters = {}
    for i, (c, vals) in enumerate(cols):
        cl = clusters.setdefault(int(lab[i]), {"headers": Counter(), "values": [], "idx": []})
        cl["headers"][snake(c)] += 1; cl["values"] += vals[:3]; cl["idx"].append(i)

    def cname(cl):
        for h, _ in cl["headers"].most_common():
            if h and h not in STOP:
                return h
        return cl["headers"].most_common(1)[0][0]

    rows = []
    for cid, cl in clusters.items():
        if len(cl["idx"]) < 3:
            continue
        cen = X[cl["idx"]].mean(0); cen /= (np.linalg.norm(cen) + 1e-9)    # save the centroid (rollup confusability needs it)
        rows.append({"cluster": cid, "name": cname(cl), "n": len(cl["idx"]),
                     "headers": [h for h, _ in cl["headers"].most_common(6)],
                     "values": list(dict.fromkeys(cl["values"]))[:10], "centroid": cen.tolist(),
                     "members": [{"header": cols[i][0], "values": cols[i][1]} for i in cl["idx"][:60]]})
    rows.sort(key=lambda r: -r["n"])
    with open(OUT / "columns.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["cluster", "name", "renamed", "n_columns", "headers", "sample_values"])
        for r in rows:
            w.writerow([r["cluster"], r["name"], "", r["n"], " | ".join(r["headers"]), " ; ".join(map(str, r["values"]))])
    json.dump(rows, open(OUT / "clusters.json", "w", encoding="utf-8"))
    print(f"\nwrote columns.csv + clusters.json: {len(rows)} clusters (>=3 cols), {sum(r['n'] for r in rows)} columns")


if __name__ == "__main__":
    main()
