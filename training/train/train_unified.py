"""
gen17 — UNIFIED ENCODER joint fine-tune. Un-freezes Qwen2.5-0.5B (LoRA) and trains it IN THE LOOP with a joint
objective, so ONE model is both a metric space (entity resolution) AND the anchored per-unit encoder:
  CONTRASTIVE (InfoNCE) on Wikidata altLabel pairs (Bombay~Mumbai, the-US~United States)  -> metric geometry
  ANCHORING   (MSE) on the gen11 corpus, but with units RE-ENCODED by the live (fine-tuned) Qwen  -> keeps
              ace/datatype/intent/srole/clause readouts valid on the NEW vectors.
Unlike train_multitask (Qwen frozen, cached unit_emb.npy), here Qwen is trainable and re-encodes every step, so the RelBlock
anchors on the moving representation. The RelBlock warm-starts from multitask_model.pt.

  CPU smoke:  python -m training.train.train_unified --smoke
  GPU run:    python -m training.train.train_unified --steps 1500 --lam 1.0
Validation each report: held-out alias->canonical recall@1 (metric) + per-family AUC (readouts survived).
"""
from __future__ import annotations
import os
import argparse, json, random, sys
from pathlib import Path
import numpy as np, torch, torch.nn as nn

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.walker import build_from_units                       # noqa: E402
from training.lib.edges import edges, fam_dims_map                 # noqa: E402
from training.lib.relblock import RelBlockModel                        # noqa: E402
from training.train.train_multitask import load, fam_report                 # noqa: E402
from engine.model_revisions import QWEN_MODEL_ID as MODEL_ID, QWEN_REVISION as MODEL_REVISION  # noqa: E402

R9, R10, R11, R17 = ROOT / "training/data", ROOT / "training/data", ROOT / "training/data", ROOT / "training/data"


class LiveQwen(nn.Module):
    """LoRA-wrapped Qwen used AS an encoder: tokenize -> last_hidden_state -> mean-pool -> per-unit vector, WITH
    gradients through the LoRA adapters. This is the unified encoder being trained."""
    def __init__(self, dev, lora=True, lora_r=16):
        super().__init__()
        from transformers import AutoModel, AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
        if self.tok.pad_token is None:
            self.tok.pad_token = self.tok.eos_token
        m = AutoModel.from_pretrained(
            MODEL_ID, revision=MODEL_REVISION, low_cpu_mem_usage=True
        ).float()
        if lora:
            from peft import LoraConfig, get_peft_model
            cfg = LoraConfig(r=lora_r, lora_alpha=2 * lora_r, lora_dropout=0.05, bias="none",
                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
            m = get_peft_model(m, cfg)
        self.qwen = m.to(dev); self.dev = dev
        self.qwen.gradient_checkpointing_enable()                     # recompute activations in backward (big mem save)
        if hasattr(self.qwen, "enable_input_require_grads"):
            self.qwen.enable_input_require_grads()                    # needed for ckpt + LoRA (frozen base)
        self.hdim = self.qwen.config.hidden_size if hasattr(self.qwen, "config") else 896

    def encode(self, texts, max_len=24, grad=True, bs=96):
        """sub-batch internally so no single Qwen forward is huge (a 4k-string eval encode would OOM otherwise).
        torch.cat preserves the grad graph across chunks, so training backprop still flows through every text."""
        texts = list(texts)
        ctx = torch.enable_grad() if grad else torch.no_grad()
        outs = []
        for i in range(0, len(texts), bs):
            enc = self.tok(texts[i:i + bs], return_tensors="pt", padding=True, truncation=True,
                           max_length=max_len).to(self.dev)
            with ctx, torch.autocast(self.dev.type, dtype=torch.bfloat16, enabled=(self.dev.type == "cuda")):
                h = self.qwen(**enc).last_hidden_state                # bf16 + grad-ckpt -> small per-chunk memory
            m = enc["attention_mask"].unsqueeze(-1).float()
            outs.append((h.float() * m).sum(1) / m.sum(1).clamp(min=1.0))
        return torch.cat(outs, 0)                                     # (n, hdim), fp32


def pack(graph, aid, fam_dims, nc, max_units=256):
    """precompute a graph's TEXTS + edges + anchoring targets (NO embeddings — x is built live each step)."""
    units = graph["units"]; S = len(units)
    if S < 2 or S > max_units:
        return None
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
    return {"texts": [u["text"] for u in units], "E": edges(units), "ct": ct, "cm": cm, "S": S}


def pack_csv(t, aid, fam_dims, nc):
    u = build_from_units(t, aid, fam_dims, nc)
    if not u:
        return None
    return {"texts": list(u["texts"]), "E": u["E"], "ct": u["ct"], "cm": u["cm"], "S": u["S"]}


def collate_live(batch, tv, nc, hin, dev):
    """build the RelBlock inputs, taking x from the LIVE text->vector map `tv` (keeps grad through Qwen)."""
    S = max(p["S"] for p in batch); B = len(batch)
    x = torch.zeros(B, S, hin, device=dev)
    E = torch.zeros(B, S, S, dtype=torch.long, device=dev)
    kp = torch.ones(B, S, dtype=torch.bool, device=dev)
    ct = torch.zeros(B, S, nc, device=dev); cm = torch.zeros(B, S, nc, dtype=torch.bool, device=dev)
    for b, p in enumerate(batch):
        s = p["S"]
        x[b, :s] = torch.stack([tv[t] for t in p["texts"]])
        E[b, :s, :s] = torch.from_numpy(p["E"]).to(dev)
        kp[b, :s] = False
        ct[b, :s] = torch.from_numpy(p["ct"]).to(dev); cm[b, :s] = torch.from_numpy(p["cm"]).to(dev)
    return x, E, kp, ct, cm


@torch.no_grad()
def evaluate(enc, model, pool, nc, hin, dev):
    encoder_training = enc.qwen.training
    model_training = model.training
    enc.qwen.eval()
    model.eval()
    ps = [[] for _ in range(nc)]; pl = [[] for _ in range(nc)]
    for i in range(0, len(pool), 4):
        batch = pool[i:i + 4]
        texts = sorted({t for p in batch for t in p["texts"]})
        V = enc.encode(texts, grad=False); tv = {t: V[j] for j, t in enumerate(texts)}
        x, E, kp, ct, cm = collate_live(batch, tv, nc, hin, dev)
        cp = model(x, E, kp)["content"]
        for cid in range(nc):
            m = cm[:, :, cid]
            if m.any():
                ps[cid].extend(cp[:, :, cid][m].float().cpu().numpy().tolist())
                pl[cid].extend((ct[:, :, cid][m] >= 0.5).int().cpu().numpy().tolist())
    enc.qwen.train(encoder_training)
    model.train(model_training)
    return ps, pl


@torch.no_grad()
def recall_at_1(enc, test_pairs, canon_pool):
    cv = enc.encode([c for c in canon_pool], grad=False)
    cv = cv / cv.norm(dim=1, keepdim=True).clamp(min=1e-9)
    av = enc.encode([a for a, c in test_pairs], grad=False)
    av = av / av.norm(dim=1, keepdim=True).clamp(min=1e-9)
    sims = av @ cv.t()
    hit = sum(canon_pool[int(sims[i].argmax())] == c for i, (a, c) in enumerate(test_pairs))
    return hit / len(test_pairs)


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
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    if args.smoke:
        args.steps, args.pair_n, args.anch_n = 6, 32, 3
    torch.manual_seed(args.seed); rng = random.Random(args.seed)
    dev = torch.device(os.environ.get("DEVICE", "cuda" if torch.cuda.is_available() else "cpu"))

    alloc = json.load(open(R11 / "alloc_multitask.json")); nc = alloc["n_content"]
    aid = {d["name"]: d["dim_id"] for d in alloc["dims"]}; fam_dims = fam_dims_map(alloc)

    # contrastive pairs (+ held-out) + a canonical pool for recall@1
    pairs = [(p["surface"], p["canonical"]) for p in load(R17 / "altlabel_pairs.jsonl")]
    rng.shuffle(pairs)
    n_te = 40 if args.smoke else 500
    train_pairs, test_pairs = pairs[n_te:], pairs[:n_te]
    canon_pool = sorted({c for _, c in pairs})
    if args.smoke:
        train_pairs = train_pairs[:400]; canon_pool = sorted({c for _, c in test_pairs} | set(canon_pool[:300]))

    # anchoring corpus (texts only; re-encoded live)
    lim = 30 if args.smoke else 0
    def take(xs): return xs[:lim] if lim else xs
    sql = [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in take(load(R10 / "sql_graphs_train.jsonl"))) if p]
    join = [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in take(load(R11 / "join_graphs_train.jsonl"))) if p]
    csv = [p for p in (pack_csv(t, aid, fam_dims, nc) for t in take(load(R9 / "units_train.jsonl"))) if p]
    test_anch = ([p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in take(load(R10 / "sql_graphs_test.jsonl"))) if p][:40]
                 + [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in take(load(R11 / "join_graphs_test.jsonl"))) if p][:40])
    anch_pool = sql + join + csv

    enc = LiveQwen(dev, lora=True, lora_r=args.lora_r)
    cfg10 = torch.load(R10 / "sql_base_meta.pt", map_location="cpu", weights_only=False)["cfg"]
    model = RelBlockModel(in_dim=enc.hdim, H=cfg10["H"], layers=cfg10["layers"], heads=cfg10["heads"], nc=nc).to(dev)
    model.load_state_dict(torch.load(R11 / "multitask_model.pt", map_location="cpu"))
    params = [p for p in enc.parameters() if p.requires_grad] + list(model.parameters())
    opt = torch.optim.AdamW(params, lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.steps)   # decay -> settle (run #1 dipped at end)
    print(f"pairs {len(train_pairs)}tr/{len(test_pairs)}te pool {len(canon_pool)} | anch {len(anch_pool)} | "
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
                                fams={"ace", "struct", "intent", "srole", "clause"})
            print(f"[{step:4d}] loss={loss.item():.3f} (nce={infonce.item():.3f} mse={mse.item():.4f}) "
                  f"recall@1={r:.3f} | {cm_}", flush=True)

    R17.mkdir(parents=True, exist_ok=True)
    enc.qwen.save_pretrained(str(R17 / "qwen_lora"))
    torch.save({"alloc": alloc, "cfg": model.cfg}, R17 / "unified_meta.pt")
    torch.save(model.state_dict(), R17 / "unified_model.pt")
    print(f"saved LoRA adapter + RelBlock to {R17}", flush=True)


if __name__ == "__main__":
    main()
