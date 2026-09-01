"""
gen20 — anchor_head.py: the gen20 ANCHOR step. Ridge-probes the named dims off the FROZEN qwen_lora encoder on the
build_from_entity CSVs (capped.entity data) and writes the two artifacts validate_data gates on:
  - anchor_assignment.npz  (W, thr, dims == alloc's 93 names, in dim_id order) — the ridge head dim19 reads as its base
  - inference.csv          with PASS / Accuracy / R2 filled (held-out generalization of the probe)

Why not reuse anchor_assignment.py: that reads build_review.build_split (the OLD bge cache), not the capped.entity data
gen20 actually trained on. This reads the EXISTING on-disk assignment.csv (train, true targets) + inference.csv
(test), recomputing each test row's 0/1 target EXACTLY like build_from_entity.trow()/carry_sql() (taxonomy from CATCOLS,
struct from the token kind, SQL intent from the train SQL rows). It does NOT re-run build_from_entity — its capped query
has no ORDER BY, so a re-run would desync the units the served model trained on.

  $env:PYTHONUTF8=1; python -m training.anchor.anchor_head
"""
from __future__ import annotations
import os
import csv
import json
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.encoder import LiveQwen                                       # noqa: E402

OUT = ROOT / "training/data"
META = ["Source", "Token", "Example", "Category"]
NUMRE = re.compile(r"^[\s$£€]*-?[\d.,]+%?$")                                    # identical to build_from_entity


def ridge(X, Y, lam=10.0):
    X1 = np.hstack([X, np.ones((len(X), 1))])
    return np.linalg.solve(X1.T @ X1 + lam * np.eye(X1.shape[1]), X1.T @ Y)     # (hdim+1, n_dims)


def best_thr(y, s):
    if y.sum() == 0:
        return float(s.max()) + 1.0                                            # never fire a dim with no positives
    bt, bf = float(s.max()) + 1.0, -1.0
    for t in np.unique(s):
        p = s >= t
        tp = (p & (y == 1)).sum(); fp = (p & (y == 0)).sum(); fn = (~p & (y == 1)).sum()
        f1 = tp / (tp + 0.5 * (fp + fn) + 1e-9)
        if f1 > bf:
            bf, bt = f1, float(t)
    return bt


def encode_all(enc, texts, cache, bs=256):
    miss = [t for t in texts if t not in cache]
    print(f"  embedding: {len(texts) - len(miss)} cache hit / {len(miss)} to encode", flush=True)
    for i in range(0, len(miss), bs):
        chunk = miss[i:i + bs]
        V = enc.encode(chunk, grad=False).detach().cpu().numpy().astype(np.float64)
        for j, t in enumerate(chunk):
            cache[t] = V[j]
        print(f"    encoded {min(i + bs, len(miss))}/{len(miss)}", flush=True)
    return cache


def main():
    alloc = json.load(open(OUT / "alloc.json"))
    DIMS = [d["name"] for d in sorted(alloc["dims"], key=lambda x: x["dim_id"])]
    dimset = set(DIMS)
    tr = list(csv.DictReader(open(OUT / "assignment.csv", encoding="utf-8")))
    te = list(csv.DictReader(open(OUT / "inference.csv", encoding="utf-8")))
    ccols = [c for c in te[0] if c.startswith("category_")]

    # SQL token -> its intent dims, learnt from the TRAIN SQL rows (the only place the intent target survives)
    sql_intent = {r["Token"].lower(): {d for d in DIMS if d.startswith("intent_") and r.get(d) == "1"}
                  for r in tr if r["Category"] in ("SQL_kw", "SQL_op")}

    def target(r):                                                             # == build_from_entity.trow()/carry_sql()
        t = {d: 0 for d in DIMS}
        if r["Category"] in ("SQL_kw", "SQL_op"):
            for d in sql_intent.get(r["Token"].lower(), ()):
                t[d] = 1
            return t
        tok = r["Token"]
        if r["Category"] == "cell_value":
            isnum = bool(NUMRE.match(tok))
            t["is_num"], t["is_str"] = (1, 0) if isnum else (0, 1)
            t["num_frac"] = 1 if (isnum and "." in tok) else 0
        else:
            t["is_str"] = 1
        for c in ccols:
            if r[c] in dimset:                                                 # co-fire only KEPT node dims on the path
                t[r[c]] = 1
        return t

    Ytr = np.array([[float(r.get(d) or 0) for d in DIMS] for r in tr], dtype=float)   # train targets already on disk
    Yte = np.array([[target(r)[d] for d in DIMS] for r in te], dtype=float)            # test dims blank -> recompute

    import torch
    dev = torch.device(os.environ.get("DEVICE", "cuda" if torch.cuda.is_available() else "cpu"))
    cache_p = OUT / "reanchor_emb_cache.pt"
    raw = torch.load(cache_p, map_location="cpu", weights_only=True) if cache_p.exists() else {}
    cache = {t: (v.cpu().numpy().astype(np.float64) if torch.is_tensor(v) else np.asarray(v, dtype=np.float64))
             for t, v in raw.items()}
    uniq = sorted({r["Token"] for r in tr + te})
    if [t for t in uniq if t not in cache]:
        enc = LiveQwen(dev, warm_lora=str(OUT / "qwen_lora"), serving=True)     # frozen, eval (dropout off)
        cache = encode_all(enc, uniq, cache)
    V = np.array([cache[t] for t in uniq])
    V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    ix = {t: i for i, t in enumerate(uniq)}
    Xtr = V[[ix[r["Token"]] for r in tr]]
    Xte = V[[ix[r["Token"]] for r in te]]

    W = ridge(Xtr, Ytr)
    Str = np.hstack([Xtr, np.ones((len(Xtr), 1))]) @ W
    Ste = np.hstack([Xte, np.ones((len(Xte), 1))]) @ W
    thr = np.array([best_thr(Ytr[:, i], Str[:, i]) for i in range(len(DIMS))])
    pred01 = (Ste >= thr).astype(int)

    base = META + ccols
    out_rows = []
    for j, r in enumerate(te):
        true_on = [i for i in range(len(DIMS)) if Yte[j][i] == 1]
        ok = (not pred01[j].any()) if not true_on else all(pred01[j][i] == 1 for i in true_on)   # recall the fired dims
        acc = float((pred01[j] == Yte[j].astype(int)).mean())
        sst = float(((Yte[j] - Yte[j].mean()) ** 2).sum())
        r2 = 1.0 - float(((Ste[j] - Yte[j]) ** 2).sum()) / sst if sst > 1e-9 else 0.0
        out_rows.append({**{k: r[k] for k in base}, **{d: int(pred01[j][i]) for i, d in enumerate(DIMS)},
                         "Accuracy": round(acc, 3), "R2": round(r2, 3), "PASS": int(bool(ok))})
    with open(OUT / "inference.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=base + DIMS + ["Accuracy", "R2", "PASS"]); w.writeheader()
        for ro in out_rows:
            w.writerow(ro)
    np.savez(OUT / "anchor_assignment.npz", W=W, thr=thr, dims=np.array(DIMS))

    npass = sum(ro["PASS"] for ro in out_rows)
    print(f"anchor_head: {len(DIMS)} dims | inference {len(out_rows)} rows PASS {npass}/{len(out_rows)} = "
          f"{npass / len(out_rows):.3f} | mean Acc {np.mean([ro['Accuracy'] for ro in out_rows]):.3f}")
    print(f"saved anchor_assignment.npz (dims={len(DIMS)}) + filled inference.csv PASS")


if __name__ == "__main__":
    main()
