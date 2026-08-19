"""
gen20 — train the UNIFIED encoder for the taxonomy allocation (alloc = struct 9 + taxonomy 105 + intent 10 = 124).

SAME recipe as gen17 (one Qwen2.5-0.5B is BOTH a metric space AND the anchored per-unit encoder):
  CONTRASTIVE (InfoNCE) on Wikidata altLabel pairs           -> metric geometry (entity resolution)
  ANCHORING   (MSE) on the gen20 column-graph corpus     -> the taxonomy node-dims, re-encoded live by the LoRA Qwen
              + the sql/join graphs (reused)                 -> the intent dims
WARM-START: the Qwen LoRA metric space from gen17/qwen_lora, the RelBlock body from unified_model.pt. The content
read-out is a SLICE h[:, :, -nc:] (NOT an nc-sized weight), so the body loads directly though nc shrinks 215->124; the
last-124 hidden coords simply re-anchor to alloc's dims.

  CPU smoke:  python -m training.train.train_taxonomy --smoke
  GPU run:    python -m training.train.train_taxonomy --steps 1500 --lam 1.0
Validation each report: held-out alias->canonical recall@1 (metric) + per-family AUC (taxonomy/struct/intent survived).
"""
from __future__ import annotations
import os
import argparse, json, random, sys
from pathlib import Path
import torch, torch.nn as nn

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.edges import fam_dims_map                           # noqa: E402
from training.lib.relblock import RelBlockModel                         # noqa: E402
from training.train.train_multitask import load, fam_report                       # noqa: E402
from training.train.train_unified import pack, pack_csv, collate_live, evaluate, recall_at_1   # noqa: E402
from training.lib.encoder import LiveQwen                           # noqa: E402  (canonical encoder — shared w/ router)

R9, R10, R11, R17, R19 = (ROOT / "training/data", ROOT / "training/data", ROOT / "training/data",
                          ROOT / "training/data", ROOT / "training/data")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=1500)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--lam", type=float, default=1.0, help="anchoring (MSE) weight; contrastive weight = 1")
    ap.add_argument("--pair-n", type=int, default=128)
    ap.add_argument("--anch-n", type=int, default=8)
    ap.add_argument("--temp", type=float, default=0.05)
    ap.add_argument("--smoke", action="store_true", help="tiny CPU run to prove the recipe")
    ap.add_argument("--max-units", type=int, default=128)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--no-warm", action="store_true", help="skip gen17 warm-start (fresh LoRA + RelBlock)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    # LEGACY GUARD: train_taxonomy is the RETIRED from-scratch trainer — it bootstraps a fresh LoRA+RelBlock from the gen17
    # base (needs altlabel_pairs.jsonl + sql_base_meta.pt + unified_model.pt) and is NOT part of the gen20 loop. The
    # gen20 self-contained training path is build_from_entity -> anchor_head -> reanchor (orchestrated by pipeline.py).
    # Fail clearly here instead of a confusing missing-file traceback deep in the run.
    _miss = [p.name for p in (R17 / "altlabel_pairs.jsonl", R10 / "sql_base_meta.pt", R17 / "unified_model.pt") if not p.exists()]
    if _miss:
        sys.stderr.write("train_taxonomy is LEGACY (from-scratch gen17 bootstrap); missing bootstrap artifacts: "
                         + ", ".join(_miss) + ".\nThe gen20 training path is build_from_entity -> anchor_head -> "
                         "reanchor (see pipeline.py) — use that.\n")
        sys.exit(2)
    if args.smoke:
        args.steps, args.pair_n, args.anch_n = 6, 32, 3
    torch.manual_seed(args.seed); rng = random.Random(args.seed)
    dev = torch.device(os.environ.get("DEVICE", "cuda" if torch.cuda.is_available() else "cpu"))

    alloc = json.load(open(R19 / "alloc.json")); nc = alloc["n_content"]
    aid = {d["name"]: d["dim_id"] for d in alloc["dims"]}; fam_dims = fam_dims_map(alloc)

    # contrastive pairs (+ held-out) + a canonical pool for recall@1 (reuse gen17 altLabel pairs)
    pairs = [(p["surface"], p["canonical"]) for p in load(R17 / "altlabel_pairs.jsonl")]
    rng.shuffle(pairs)
    n_te = 40 if args.smoke else 500
    train_pairs, test_pairs = pairs[n_te:], pairs[:n_te]
    canon_pool = sorted({c for _, c in pairs})
    if args.smoke:
        train_pairs = train_pairs[:400]; canon_pool = sorted({c for _, c in test_pairs} | set(canon_pool[:300]))

    # anchoring corpus (texts only; re-encoded live): gen20 taxonomy column-graphs + sql/join graphs for intent
    lim = 30 if args.smoke else 0
    def take(xs): return xs[:lim] if lim else xs
    tax = [p for p in (pack_csv(t, aid, fam_dims, nc) for t in take(load(R19 / "units_train.jsonl"))) if p]
    sql = [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in take(load(R10 / "sql_graphs_train.jsonl"))) if p]
    join = [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in take(load(R11 / "join_graphs_train.jsonl"))) if p]
    test_anch = ([p for p in (pack_csv(t, aid, fam_dims, nc) for t in take(load(R19 / "units_test.jsonl"))) if p][:60]
                 + [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in take(load(R10 / "sql_graphs_test.jsonl"))) if p][:20])
    anch_pool = tax + sql + join

    warm = None if args.no_warm else (R17 / "qwen_lora")
    enc = LiveQwen(dev, lora_r=args.lora_r, warm_lora=warm, serving=False)   # TRAINING: dropout + grad checkpointing on
    cfg10 = torch.load(R10 / "sql_base_meta.pt", map_location="cpu", weights_only=False)["cfg"]
    model = RelBlockModel(in_dim=enc.hdim, H=cfg10["H"], layers=cfg10["layers"], heads=cfg10["heads"], nc=nc).to(dev)
    if not args.no_warm:                                                     # body is nc-independent (content = -nc: slice)
        miss, unexp = model.load_state_dict(torch.load(R17 / "unified_model.pt", map_location="cpu"), strict=False)
        print(f"warm-started RelBlock body from unified_model.pt (missing {len(miss)}, unexpected {len(unexp)})", flush=True)
    params = [p for p in enc.parameters() if p.requires_grad] + list(model.parameters())
    opt = torch.optim.AdamW(params, lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.steps)
    print(f"alloc nc={nc} | pairs {len(train_pairs)}tr/{len(test_pairs)}te pool {len(canon_pool)} | "
          f"anch tax{len(tax)}+sql{len(sql)}+join{len(join)} | "
          f"LoRA-trainable {sum(p.numel() for p in enc.parameters() if p.requires_grad)/1e6:.2f}M | dev={dev}", flush=True)
    print(f"[pre] recall@1={recall_at_1(enc, test_pairs, canon_pool):.3f}", flush=True)

    for step in range(1, args.steps + 1):
        bp = rng.sample(train_pairs, min(args.pair_n, len(train_pairs)))
        ba = rng.sample(anch_pool, min(args.anch_n, len(anch_pool)))
        texts = sorted({t for a, c in bp for t in (a, c)} | {t for p in ba for t in p["texts"]})
        V = enc.encode(texts, grad=True); tv = {t: V[i] for i, t in enumerate(texts)}
        za = torch.stack([tv[a] for a, c in bp]); zc = torch.stack([tv[c] for a, c in bp])
        za = za / za.norm(dim=1, keepdim=True).clamp(min=1e-9); zc = zc / zc.norm(dim=1, keepdim=True).clamp(min=1e-9)
        logits = za @ zc.t() / args.temp
        infonce = nn.functional.cross_entropy(logits, torch.arange(len(bp), device=dev))
        x, E, kp, ct, cm = collate_live(ba, tv, nc, enc.hdim, dev)
        mse = ((model(x, E, kp)["content"][cm] - ct[cm]) ** 2).mean()
        loss = infonce + args.lam * mse
        opt.zero_grad(); loss.backward(); opt.step(); sched.step()
        if step % (3 if args.smoke else 250) == 0 or step == 1:
            r = recall_at_1(enc, test_pairs, canon_pool)
            cm_, _ = fam_report(alloc, *evaluate(enc, model, test_anch, nc, enc.hdim, dev),
                                fams={"taxonomy", "struct", "intent"})
            print(f"[{step:4d}] loss={loss.item():.3f} (nce={infonce.item():.3f} mse={mse.item():.4f}) "
                  f"recall@1={r:.3f} | {cm_}", flush=True)

    R19.mkdir(parents=True, exist_ok=True)
    enc.qwen.save_pretrained(str(R19 / "qwen_lora"))
    torch.save({"alloc": alloc, "cfg": model.cfg}, R19 / "encoder_meta.pt")
    torch.save(model.state_dict(), R19 / "encoder.pt")
    print(f"saved LoRA adapter + RelBlock to {R19}", flush=True)


if __name__ == "__main__":
    main()
