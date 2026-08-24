"""
gen11 JOINT multi-task fine-tune. Warm-starts from gen10 and trains THREE tasks on the SAME model /
SAME reserved dims (alloc_multitask = gen10's 214 dims + clause_join prepended at dim_id 0 -> warm-start aligned):
  CSV task   (gen9 corpus, precomputed unit_emb.npy)  -> struct/nsm/ace anchoring (gen9 dims)  [KEPT]
  SQL task   (gen10 single-table sql_graphs)          -> intent/srole/clause (gen10 dims)       [KEPT]
  JOIN task  (gen11 multi-table join_graphs)          -> intent/srole/clause + clause_join (NEW)    [NEW]
Mixing the CSV + single-table SQL tasks keeps those dims from drifting; the JOIN task teaches the cross-table
E_FK edge bias + the `join` clause + qualified t.col field binding. Qwen is frozen (only encodes). To fit the
10-min encode cap, the single-table SQL texts are REUSED from gen10's cache and only NEW texts are encoded.

Run (CPU smoke): python -m training.train.train_multitask --limit-csv 60 --limit-sql 150 --steps 40
Run (resume):    python -m training.train.train_multitask --steps 1200 --resume
"""
from __future__ import annotations
import argparse, json, os, random, sys
from pathlib import Path
import numpy as np, torch
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.walker import build_from_units
from training.lib.edges import edges, fam_dims_map
from training.lib.relblock import RelBlockModel
from engine.model_revisions import QWEN_MODEL_ID as MODEL_ID, QWEN_REVISION as MODEL_REVISION



def auc(scores, labels):
    s, y = np.asarray(scores, float), np.asarray(labels, int)
    p, n = int((y == 1).sum()), int((y == 0).sum())
    if p == 0 or n == 0:
        return None, p, n
    order = s.argsort(); ranks = np.empty(len(s)); ranks[order] = np.arange(1, len(s) + 1)
    return float((ranks[y == 1].sum() - p * (p + 1) / 2) / (p * n)), p, n


def load(p):
    return [json.loads(l) for l in Path(p).read_text(encoding="utf-8").splitlines() if l.strip()]


def pack_sql(graph, aid, fam_dims, nc, t2v, max_units=256):
    units = graph["units"]
    S = len(units)
    if S < 2 or S > max_units:
        return None
    x = np.stack([t2v[u["text"]] for u in units]).astype(np.float32)
    E = edges(units)
    ct = np.zeros((S, nc), np.float32); cm = np.zeros((S, nc), bool)
    for ui, u in enumerate(units):
        fired = set(u["fired"])
        for fam in u["sup"]:
            for dn in fam_dims.get(fam, []):
                d = aid.get(dn)
                if d is not None:
                    cm[ui, d] = True
                    if dn in fired:
                        ct[ui, d] = 1.0
    return {"x": x, "E": E, "ct": ct, "cm": cm, "S": S}


def pack_csv(t, aid, fam_dims, nc, emb, idx):
    u = build_from_units(t, aid, fam_dims, nc)
    if not u:
        return None
    rows = [idx.get(tx) for tx in u["texts"]]
    if any(r is None for r in rows):
        return None
    return {"x": emb[rows].astype(np.float32), "E": u["E"], "ct": u["ct"], "cm": u["cm"], "S": u["S"]}


def collate(batch, nc, hin, dev):
    S = max(p["S"] for p in batch); B = len(batch)
    x = torch.zeros(B, S, hin); E = torch.zeros(B, S, S, dtype=torch.long)
    kp = torch.ones(B, S, dtype=torch.bool); ct = torch.zeros(B, S, nc); cm = torch.zeros(B, S, nc, dtype=torch.bool)
    for b, p in enumerate(batch):
        s = p["S"]
        x[b, :s] = torch.from_numpy(p["x"]); E[b, :s, :s] = torch.from_numpy(p["E"])
        kp[b, :s] = False; ct[b, :s] = torch.from_numpy(p["ct"]); cm[b, :s] = torch.from_numpy(p["cm"])
    return x.to(dev), E.to(dev), kp.to(dev), ct.to(dev), cm.to(dev)


@torch.no_grad()
def evaluate(model, pool, nc, hin, dev):
    if not pool:
        return [[] for _ in range(nc)], [[] for _ in range(nc)]
    model.eval()
    ps = [[] for _ in range(nc)]; pl = [[] for _ in range(nc)]
    for i in range(0, len(pool), 8):
        x, E, kp, ct, cm = collate(pool[i:i + 8], nc, hin, dev)
        cp = model(x, E, kp)["content"]
        for cid in range(nc):
            m = cm[:, :, cid]
            if m.any():
                ps[cid].extend(cp[:, :, cid][m].float().cpu().numpy().tolist())
                pl[cid].extend((ct[:, :, cid][m] >= 0.5).int().cpu().numpy().tolist())
    model.train()
    return ps, pl


def fam_report(alloc, ps, pl, fams=None):
    out = {}
    for d in alloc["dims"]:
        if fams and d["family"] not in fams:
            continue
        a, p, n = auc(ps[d["dim_id"]], pl[d["dim_id"]])
        out.setdefault(d["family"], []).append((d["name"], a, p))
    means = {}
    for fam, rows in out.items():
        vs = [a for _, a, p in rows if a is not None and p >= 3]
        means[fam] = round(float(np.mean(vs)), 3) if vs else None
    return means, out


def encode(texts, dev, bs=256, max_len=24):
    from transformers import AutoModel, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    qwen = AutoModel.from_pretrained(
        MODEL_ID, revision=MODEL_REVISION, low_cpu_mem_usage=True
    ).float().to(dev).eval()
    hdim = qwen.config.hidden_size
    out = np.zeros((len(texts), hdim), np.float32)
    with torch.no_grad():
        for i in range(0, len(texts), bs):
            chunk = texts[i:i + bs]
            enc = tok(chunk, return_tensors="pt", padding=True, truncation=True, max_length=max_len).to(dev)
            h = qwen(**enc).last_hidden_state
            m = enc["attention_mask"].unsqueeze(-1).float()
            out[i:i + len(chunk)] = ((h * m).sum(1) / m.sum(1).clamp(min=1.0)).float().cpu().numpy()
            if i % (bs * 40) == 0:
                print(f"  encoded {i + len(chunk)}/{len(texts)}", flush=True)
    del qwen
    return out, hdim


def get_embeddings(texts, dev, r10, r11, encode_only):
    """Reuse gen10's sql_emb cache for overlapping texts; encode only NEW ones. -> (t2v, hin)."""
    texts = sorted(set(texts))
    cache = r11 / "sql_emb.npz"
    if cache.exists() and not encode_only:
        d = np.load(cache, allow_pickle=True)
        if list(d["texts"]) == texts:
            print(f"loaded gen11 SQL embedding cache ({len(texts)} texts)", flush=True)
            return {t: d["emb"][i] for i, t in enumerate(texts)}, int(d["emb"].shape[1])
        print("gen11 cache stale -> rebuilding", flush=True)
    have = {}
    prev = r10 / "sql_emb.npz"
    if prev.exists():
        d = np.load(prev, allow_pickle=True)
        have = {t: d["emb"][i] for i, t in enumerate(list(d["texts"]))}
        print(f"reusing {sum(t in have for t in texts)}/{len(texts)} texts from gen10 cache", flush=True)
    missing = [t for t in texts if t not in have]
    if missing:
        print(f"encoding {len(missing)} NEW texts on {dev} ...", flush=True)
        vecs, _ = encode(missing, dev)
        for i, t in enumerate(missing):
            have[t] = vecs[i]
    hin = int(next(iter(have.values())).shape[0])
    emb = np.stack([have[t] for t in texts]).astype(np.float32)
    np.savez(cache, emb=emb, texts=np.array(texts, dtype=object))
    print(f"saved gen11 SQL embedding cache ({len(texts)} texts, dim {hin}) -> {cache}", flush=True)
    return {t: emb[i] for i, t in enumerate(texts)}, hin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=1200)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--csv-n", type=int, default=6)               # per-batch counts: CSV / single-table SQL / JOIN
    ap.add_argument("--sql-n", type=int, default=5)
    ap.add_argument("--join-n", type=int, default=5)
    ap.add_argument("--limit-csv", type=int, default=0)
    ap.add_argument("--limit-sql", type=int, default=0)
    ap.add_argument("--max-units", type=int, default=256)
    ap.add_argument("--encode-only", action="store_true", help="build embedding cache, then exit (fits 10-min cap)")
    ap.add_argument("--resume", action="store_true", help="continue from an existing gen11 checkpoint")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--r9", type=Path, default=ROOT / "training/data")
    ap.add_argument("--r10", type=Path, default=ROOT / "training/data")
    ap.add_argument("--r11", type=Path, default=ROOT / "training/data")
    ap.add_argument("--out", type=Path, default=ROOT / "training/data")
    args = ap.parse_args()
    torch.manual_seed(args.seed); rng = random.Random(args.seed)
    dev = torch.device(os.environ.get("DEVICE", "cuda" if torch.cuda.is_available() else "cpu"))

    alloc = json.load(open(args.r11 / "alloc_multitask.json")); nc = alloc["n_content"]
    aid = {d["name"]: d["dim_id"] for d in alloc["dims"]}; fam_dims = fam_dims_map(alloc)

    sql_tr = load(args.r10 / "sql_graphs_train.jsonl"); sql_te = load(args.r10 / "sql_graphs_test.jsonl")
    join_tr = load(args.r11 / "join_graphs_train.jsonl"); join_te = load(args.r11 / "join_graphs_test.jsonl")
    if args.limit_sql:
        sql_tr = sql_tr[:args.limit_sql]; sql_te = sql_te[:max(40, args.limit_sql // 8)]
    texts = [u["text"] for g in (sql_tr + sql_te + join_tr + join_te) for u in g["units"]]
    t2v, hin = get_embeddings(texts, dev, args.r10, args.r11, args.encode_only)
    if args.encode_only:
        return

    pool_sql = [p for p in (pack_sql(g, aid, fam_dims, nc, t2v, args.max_units) for g in sql_tr) if p]
    test_sql = [p for p in (pack_sql(g, aid, fam_dims, nc, t2v, args.max_units) for g in sql_te) if p]
    pool_join = [p for p in (pack_sql(g, aid, fam_dims, nc, t2v, args.max_units) for g in join_tr) if p]
    test_join = [p for p in (pack_sql(g, aid, fam_dims, nc, t2v, args.max_units) for g in join_te) if p]

    emb = np.load(args.r9 / "unit_emb.npy"); idx = json.load(open(args.r9 / "unit_idx.json", encoding="utf-8"))
    assert emb.shape[1] == hin, f"emb dim {emb.shape[1]} != Qwen {hin}"
    csv_tr = load(args.r9 / "units_train.jsonl"); csv_te = load(args.r9 / "units_test.jsonl")
    if args.limit_csv:
        csv_tr = csv_tr[:args.limit_csv]; csv_te = csv_te[:max(20, args.limit_csv // 4)]
    pool_csv = [p for p in (pack_csv(t, aid, fam_dims, nc, emb, idx) for t in csv_tr) if p]
    test_csv = [p for p in (pack_csv(t, aid, fam_dims, nc, emb, idx) for t in csv_te) if p]
    test_csv, test_sql = test_csv[:50], test_sql[:120]

    cfg10 = torch.load(args.r10 / "sql_base_meta.pt", map_location="cpu", weights_only=False)["cfg"]
    model = RelBlockModel(in_dim=hin, H=cfg10["H"], layers=cfg10["layers"], heads=cfg10["heads"], nc=nc).to(dev)
    if args.resume and (args.out / "multitask_model.pt").exists():
        model.load_state_dict(torch.load(args.out / "multitask_model.pt", map_location="cpu"))
        print("resumed from gen11 checkpoint", flush=True)
    else:
        copied, grown = model.warm_start(torch.load(args.r10 / "sql_base_model.pt", map_location="cpu"))
        print(f"warm-start from gen10: copied {copied} tensors, grew {grown} edge-bias", flush=True)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    print(f"CSV {len(pool_csv)}/{len(test_csv)} | SQL {len(pool_sql)}/{len(test_sql)} | "
          f"JOIN {len(pool_join)}/{len(test_join)} | nc={nc} dev={dev}", flush=True)

    def sample():
        return (rng.sample(pool_csv, min(args.csv_n, len(pool_csv)))
                + rng.sample(pool_sql, min(args.sql_n, len(pool_sql)))
                + rng.sample(pool_join, min(args.join_n, len(pool_join))))

    def report(step, loss):
        cm_, _ = fam_report(alloc, *evaluate(model, test_csv, nc, hin, dev), fams={"struct", "nsm_cat", "nsm_prime", "ace"})
        sm, _ = fam_report(alloc, *evaluate(model, test_sql, nc, hin, dev), fams={"intent", "srole", "clause"})
        jm, jo = fam_report(alloc, *evaluate(model, test_join, nc, hin, dev), fams={"intent", "srole", "clause"})
        print(f"[{step:5d}] loss={loss:.4f} | CSV {cm_} | SQL {sm} | JOIN {jm}", flush=True)
        return jo

    for step in range(1, args.steps + 1):
        x, E, kp, ct, cm = collate(sample(), nc, hin, dev)
        loss = ((model(x, E, kp)["content"][cm] - ct[cm]) ** 2).mean()
        opt.zero_grad(); loss.backward(); opt.step()
        if step % 200 == 0 or step == 1:
            report(step, loss.item())
            if step >= 200:
                args.out.mkdir(parents=True, exist_ok=True)
                torch.save({"alloc": alloc, "cfg": model.cfg}, args.out / "multitask_meta.pt")
                torch.save(model.state_dict(), args.out / "multitask_model.pt")

    args.out.mkdir(parents=True, exist_ok=True)
    torch.save({"alloc": alloc, "cfg": model.cfg}, args.out / "multitask_meta.pt")
    torch.save(model.state_dict(), args.out / "multitask_model.pt")
    jo = report(args.steps, loss.item())
    print("\n=== FINAL gen11 (held-out) — chance 0.5 ===")
    for fam in ("intent", "srole", "clause"):
        det = "  ".join(f"{nm.split('_', 1)[1]}={a}" for nm, a, p in jo.get(fam, []) if a is not None and p >= 2)
        print(f"  JOIN {fam:7} | {det}")
    print(f"\nsaved multitask_meta.pt + multitask_model.pt to {args.out}")


if __name__ == "__main__":
    main()
