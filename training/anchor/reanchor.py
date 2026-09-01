"""
gen20 — RE-ANCHOR the RelBlock readout on the CLEAN capped.entity corpus, nc=93.

Same bounded-CPU recipe as reanchor(gen19), three changes for gen20: (1) alloc.json (nc=93, 74 taxonomy dims, the
data-starved leaves dropped); (2) the taxonomy units come from training/data/units_{train,test}.jsonl (real
capped.entity instances, not the noisy bge map); (3) the served gen20 RelBlock body is warm-started by SHAPE
(strict body load), and the nc=93 content readout re-initialises and re-anchors fresh. The frozen Qwen+LoRA encoder
is UNCHANGED. SQL/join (intent) graphs are replayed from gen10/19 so intent/struct don't regress.

  $env:PYTHONUTF8=1; python -m training.anchor.reanchor --steps 1500
"""
from __future__ import annotations
import os
import argparse
import json
import random
import sys
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.edges import fam_dims_map                                    # noqa: E402
from training.lib.relblock import RelBlockModel                                  # noqa: E402
from training.train.train_multitask import load, fam_report                               # noqa: E402
from training.train.train_unified import pack, pack_csv, collate_live, evaluate          # noqa: E402
from training.lib.encoder import LiveQwen                                      # noqa: E402

R10, R11, R19, R20 = ROOT / "training/data", ROOT / "training/data", ROOT / "training/data", ROOT / "training/data"


def encode_all(enc, texts, bs=256):
    tv = {}
    for i in range(0, len(texts), bs):
        chunk = texts[i:i + bs]
        V = enc.encode(chunk, grad=False).detach()
        for j, t in enumerate(chunk):
            tv[t] = V[j]
        print(f"    encoded {min(i + bs, len(texts))}/{len(texts)}", flush=True)
    return tv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=1500)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--anch-n", type=int, default=24)
    ap.add_argument("--max-units", type=int, default=128)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    torch.manual_seed(args.seed)
    rng = random.Random(args.seed)
    dev = torch.device(os.environ.get("DEVICE", "cuda" if torch.cuda.is_available() else "cpu"))

    alloc = json.load(open(R20 / "alloc.json"))
    nc = alloc["n_content"]
    aid = {d["name"]: d["dim_id"] for d in alloc["dims"]}
    fam_dims = fam_dims_map(alloc)

    enc = LiveQwen(dev, warm_lora=str(R19 / "qwen_lora"), serving=True)               # FROZEN encoder, unchanged
    cfg = torch.load(R19 / "encoder_meta.pt", map_location="cpu", weights_only=True)["cfg"]
    model = RelBlockModel(in_dim=cfg["in_dim"], H=cfg["H"], layers=cfg["layers"], heads=cfg["heads"],
                           nc=nc, n_edge=cfg["n_edge"]).to(dev)
    sd = torch.load(R19 / "encoder.pt", map_location="cpu", weights_only=True)
    msd = model.state_dict()
    filt = {k: v for k, v in sd.items() if k in msd and v.shape == msd[k].shape}      # body matches; nc=124 readout skipped
    model.load_state_dict(filt, strict=False)
    print(f"warm-start: loaded {len(filt)}/{len(msd)} body params; {len(sd) - len(filt)} nc-dependent skipped", flush=True)
    model.train()

    tax = [p for p in (pack_csv(t, aid, fam_dims, nc) for t in load(R20 / "units_train.jsonl")) if p]
    sql = [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in load(R10 / "sql_graphs_train.jsonl")) if p]
    join = [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in load(R11 / "join_graphs_train.jsonl")) if p]
    anch_pool = tax + sql + join
    test_anch = ([p for p in (pack_csv(t, aid, fam_dims, nc) for t in load(R20 / "units_test.jsonl")) if p][:60]
                 + [p for p in (pack(g, aid, fam_dims, nc, args.max_units)
                                for g in load(R10 / "sql_graphs_test.jsonl")) if p][:20])
    texts = sorted({t for p in anch_pool for t in p["texts"]})
    print(f"alloc nc={nc} | tax{len(tax)}+sql{len(sql)}+join{len(join)} | {len(texts)} unique texts | dev={dev}", flush=True)

    cache_p = R20 / "reanchor_emb_cache.pt"
    cache = torch.load(cache_p, map_location="cpu", weights_only=True) if cache_p.exists() else {}
    r19c = R19 / "reanchor_emb_cache.pt"
    if r19c.exists():
        rc = torch.load(r19c, map_location="cpu", weights_only=True)
        for t in texts:                                                              # reuse shared sql/join embeddings
            if t not in cache and t in rc:
                cache[t] = rc[t]
    missing = [t for t in texts if t not in cache]
    print(f"embedding cache: {len(texts) - len(missing)} hit / {len(missing)} to encode (frozen, one pass)", flush=True)
    if missing:
        cache.update(encode_all(enc, missing))
        torch.save(cache, cache_p)
    tv = {t: cache[t] for t in texts}

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.steps)
    cm0, _ = fam_report(alloc, *evaluate(enc, model, test_anch, nc, enc.hdim, dev), fams={"taxonomy", "struct", "intent"})
    print(f"[pre] {cm0}", flush=True)
    pool = sql + join
    ntax = max(1, int(args.anch_n * 0.6))
    for step in range(1, args.steps + 1):
        ba = (rng.sample(tax, min(ntax, len(tax)))
              + rng.sample(pool, min(args.anch_n - ntax, len(pool))))
        x, E, kp, ct, cm = collate_live(ba, tv, nc, enc.hdim, dev)
        mse = ((model(x, E, kp)["content"][cm] - ct[cm]) ** 2).mean()
        opt.zero_grad(); mse.backward(); opt.step(); sched.step()
        if step % 250 == 0 or step == 1:
            cm_, _ = fam_report(alloc, *evaluate(enc, model, test_anch, nc, enc.hdim, dev),
                                fams={"taxonomy", "struct", "intent"})
            print(f"[{step:4d}] mse={mse.item():.4f} | {cm_}", flush=True)
            if step % 250 == 0:                                              # CHECKPOINT: always have a saved model on disk
                torch.save(model.state_dict(), R20 / "encoder.pt")
                torch.save({"alloc": alloc, "cfg": {**cfg, "nc": nc}}, R20 / "encoder_meta.pt")   # serving reads BOTH alloc + cfg
                print(f"       checkpoint saved at step {step}", flush=True)

    torch.save(model.state_dict(), R20 / "encoder.pt")
    torch.save({"alloc": alloc, "cfg": {**cfg, "nc": nc}}, R20 / "encoder_meta.pt")   # serving reads BOTH alloc + cfg
    print(f"saved encoder.pt + encoder_meta.pt (nc={nc}); encoder qwen_lora UNCHANGED", flush=True)


if __name__ == "__main__":
    main()
