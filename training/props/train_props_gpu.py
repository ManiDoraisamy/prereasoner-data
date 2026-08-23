#!/usr/bin/env python3
"""Property fine-tune (GPU) — UN-FREEZE qwen_lora + MSE-anchor the schema.org property corpus so properties become
separable in the ONE shared RelBlock readout. Tests the hypothesis that reshaping the encoder geometry (which the
FROZEN re-anchor could not do: full-test per-property AUC 0.47) recovers the per-property signal the raw embedding
carries (per-property LR ceiling ~0.77). Held-out FULL-test per-property AUC eval + keep-best.

No InfoNCE (altlabel_pairs.jsonl unavailable) — the un-freeze + MSE + held-out keep-best is the bounded experiment.

Stage 3 of the schema.org-property pipeline (see training/props/pipeline.md). Reuses the gen20 encoder/model/harness
from training.lib + training.train — the property trainer only differs in the corpus + the un-freeze + keep-best.

WARM-START: the base gen20 LoRA + RelBlock this fine-tune resumes from live in the DATA dir as
`qwen_lora/`, `unified_meta.pt`, `unified_model.pt` — the artifacts written by `training.train.train_unified`
(shipped equivalently as engine/data/{qwen_lora, encoder_meta.pt, encoder.pt}). Copy them into DATA before running.
The base alloc must be present as `alloc20.json` — do `cp DATA/alloc.json DATA/alloc20.json` after Stage 2 first.

  $env:PYTHONUTF8=1; python -m training.props.train_props_gpu --steps 600 --lr 2e-4
"""
from __future__ import annotations
import argparse, json, os, random
import numpy as np, torch
import torch.nn.functional as F

from training.lib.edges import fam_dims_map                       # noqa: E402
from training.lib.relblock import RelBlockModel                   # noqa: E402  (gen20's Runtime11Model, renamed)
from training.train.train_multitask import load, fam_report       # noqa: E402
from training.train.train_unified import pack, pack_csv, collate_live, evaluate   # noqa: E402
from training.lib.encoder import LiveQwen                         # noqa: E402
from training.props.eval_intent import (  # noqa: E402
    calibrate_intent_thresholds,
    intent_metrics,
    load_eval,
)

HERE = os.path.dirname(os.path.abspath(__file__))              # training/props/
TRAIN_DIR = os.environ.get("PREREASONER_TRAIN_DIR", HERE)
DATA = os.path.join(TRAIN_DIR, "data"); os.makedirs(DATA, exist_ok=True)
REPO = os.path.dirname(os.path.dirname(HERE))                  # repo root (training/props -> training -> repo)
ENGINE_DATA = os.environ.get("PREREASONER_ENGINE_DATA", os.path.join(REPO, "engine", "data"))

R = DATA


def calculation_contrastive_loss(enc, rows, dev, *, temperature=0.07, grad=True):
    """InfoNCE over named intent prototypes and schema-column operands."""
    if not rows:
        return torch.zeros((), device=dev)
    texts = sorted({
        text for row in rows
        for text in (row["query"], row["positive"], *row.get("negatives", ()))
    })
    embeddings = enc.encode(texts, grad=grad, max_len=64)
    vectors = {text: F.normalize(embeddings[index], dim=-1) for index, text in enumerate(texts)}
    losses = []
    for row in rows:
        query = vectors[row["query"]]
        candidates = [row["positive"], *row.get("negatives", ())]
        logits = torch.stack([query @ vectors[candidate] for candidate in candidates]) / temperature
        losses.append(F.cross_entropy(logits.unsqueeze(0), torch.zeros(1, dtype=torch.long, device=dev)))
    return torch.stack(losses).mean()


@torch.no_grad()
def calculation_contrastive_accuracy(enc, rows, dev, *, batch_size=24):
    if not rows:
        return 0.0, {}
    encoder_training = enc.qwen.training
    enc.qwen.eval()
    correct = 0
    per_kind = {}
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        texts = sorted({
            text for row in batch
            for text in (row["query"], row["positive"], *row.get("negatives", ()))
        })
        embeddings = enc.encode(texts, grad=False, max_len=64)
        vectors = {text: F.normalize(embeddings[index], dim=-1) for index, text in enumerate(texts)}
        for row in batch:
            candidates = [row["positive"], *row.get("negatives", ())]
            scores = [float(vectors[row["query"]] @ vectors[candidate]) for candidate in candidates]
            hit = int(max(range(len(scores)), key=scores.__getitem__) == 0)
            correct += hit
            keys = (
                str(row.get("kind") or "unknown"),
                f"{row.get('kind') or 'unknown'}:{row.get('label') or 'unknown'}",
            )
            for key in keys:
                passed, total = per_kind.get(key, (0, 0))
                per_kind[key] = (passed + hit, total + 1)
    result = correct / len(rows), {
        key: passed / total for key, (passed, total) in sorted(per_kind.items())
    }
    enc.qwen.train(encoder_training)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=600)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--anch-n", type=int, default=20)
    ap.add_argument("--intent-n", type=int, default=6)
    ap.add_argument("--max-units", type=int, default=128)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--eval-every", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--calculation-n", type=int, default=6)
    ap.add_argument("--calculation-weight", type=float, default=0.35)
    ap.add_argument("--calculation-temperature", type=float, default=0.07)
    ap.add_argument("--min-intent-accuracy", type=float, default=0.80)
    ap.add_argument("--min-calculation-accuracy", type=float, default=0.80)
    ap.add_argument("--max-bad-intent-probes", type=int, default=0)
    args = ap.parse_args()
    torch.manual_seed(args.seed); rng = random.Random(args.seed)
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    alloc = json.load(open(os.path.join(R, "alloc20.json"))); nc = alloc["n_content"]
    aid = {d["name"]: d["dim_id"] for d in alloc["dims"]}; fam_dims = fam_dims_map(alloc)

    enc = LiveQwen(dev, lora_r=args.lora_r, warm_lora=os.path.join(R, "qwen_lora"), serving=False)   # UN-FREEZE the LoRA
    cfg = torch.load(os.path.join(R, "unified_meta.pt"), map_location="cpu", weights_only=False)["cfg"]
    model = RelBlockModel(in_dim=cfg["in_dim"], H=cfg["H"], layers=cfg["layers"], heads=cfg["heads"],
                          nc=nc, n_edge=cfg["n_edge"]).to(dev)
    sd = torch.load(os.path.join(R, "unified_model.pt"), map_location="cpu"); msd = model.state_dict()
    model.load_state_dict({k: v for k, v in sd.items() if k in msd and v.shape == msd[k].shape}, strict=False)

    tax = [p for p in (pack_csv(t, aid, fam_dims, nc) for t in load(os.path.join(R, "units_train.jsonl"))) if p]
    sql = [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in load(os.path.join(R, "sql_graphs_train.jsonl"))) if p]
    join = [p for p in (pack(g, aid, fam_dims, nc, args.max_units) for g in load(os.path.join(R, "join_graphs_train.jsonl"))) if p]
    # intent AUGMENTATION (augment_intent.py): the serving phrasings — "how many"/"number of" -> COUNT,
    # "sum of"/"how much" -> SUM, "in <place>" as NONE — anchored, not left to emergent generalization.
    aug_p = os.path.join(R, "intent_aug_train.jsonl")
    aug = []
    if os.path.exists(aug_p):
        for graph in load(aug_p):
            packed = pack(graph, aid, fam_dims, nc, args.max_units)
            if packed:
                packed["expect"] = str(graph.get("expect"))
                aug.append(packed)
    intent_classes = ("None", "COUNT", "SUM", "AVG")
    aug_by_class = {
        label: [packed for packed in aug if packed["expect"] == label]
        for label in intent_classes
    }
    if aug and any(not rows for rows in aug_by_class.values()):
        raise SystemExit("intent augmentation must contain None, COUNT, SUM, and AVG rows")
    base_pool = sql + join
    test_full = [p for p in (pack_csv(t, aid, fam_dims, nc) for t in load(os.path.join(R, "units_test.jsonl"))) if p]
    # held-out intent eval (NEVER trained on): keep-best selects on it so the property-optimal
    # checkpoint can no longer ship with drifted intent (the first run's COUNT regression).
    ieval = load_eval(aid, fam_dims, nc, max_units=args.max_units)
    if not ieval:
        print("WARNING: no intent_eval.jsonl — keep-best falls back to property AUC only "
              "(run `python -m training.props.augment_intent` first)", flush=True)
    icalibration = load_eval(
        aid,
        fam_dims,
        nc,
        path=os.path.join(R, "intent_calibration.jsonl"),
        max_units=args.max_units,
    )
    if not icalibration:
        raise SystemExit(
            "intent calibration data is required; run "
            "`python -m training.props.augment_intent` first"
        )
    calculation_train_path = os.path.join(R, "calculation_contrastive_train.jsonl")
    calculation_eval_path = os.path.join(R, "calculation_contrastive_eval.jsonl")
    calculation_train = load(calculation_train_path) if os.path.exists(calculation_train_path) else []
    calculation_eval = load(calculation_eval_path) if os.path.exists(calculation_eval_path) else []
    if not calculation_train or not calculation_eval:
        raise SystemExit(
            "calculation contrastive data is required; run "
            "`python -m training.props.calculation_contrastive` first"
        )
    print(f"dev={dev} | nc={nc} | tax{len(tax)}+sql{len(sql)}+join{len(join)}+aug{len(aug)} | test {len(test_full)} "
          f"| intent-cal/eval {len(icalibration)}/{len(ieval)} "
          f"| calc {len(calculation_train)}/{len(calculation_eval)} "
          f"| LoRA-trainable {sum(p.numel() for p in enc.parameters() if p.requires_grad)/1e6:.2f}M",
          flush=True)

    def propauc():
        means, out = fam_report(alloc, *evaluate(enc, model, test_full, nc, enc.hdim, dev), fams={"taxonomy"})
        rows = [(a, p) for _, a, p in out["taxonomy"] if a is not None and p >= 5]
        mean = float(np.mean([a for a, _ in rows])) if rows else 0.0
        return mean, sum(a >= 0.75 for a, _ in rows), sum(a >= 0.85 for a, _ in rows), len(rows)

    def intenteval():
        """(op-accuracy, mean intent AUC, #failing named probes) on the held-out intent eval."""
        if not ieval:
            return None
        thresholds = calibrate_intent_thresholds(
            enc, model, icalibration, aid, nc, enc.hdim, dev,
        )
        acc, _, probes, iauc = intent_metrics(
            enc, model, ieval, aid, nc, enc.hdim, dev, thresholds,
        )
        bad_probes = tuple(name for name, *_rest, ok in probes if not ok)
        return acc, iauc, len(bad_probes), thresholds, bad_probes

    def calculationeval():
        return calculation_contrastive_accuracy(enc, calculation_eval, dev)

    params = [p for p in enc.parameters() if p.requires_grad] + list(model.parameters())
    opt = torch.optim.AdamW(params, lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.steps)
    ntax = max(1, int(args.anch_n * 0.6))
    nintent = min(len(aug), max(0, args.intent_n), max(0, args.anch_n - ntax))
    nbase = max(0, args.anch_n - ntax - nintent)
    intent_quota = {
        label: nintent // len(intent_classes) + (index < nintent % len(intent_classes))
        for index, label in enumerate(intent_classes)
    }
    print(f"batch strata: property={ntax} base={nbase} intent={intent_quota} "
          f"calculation={args.calculation_n}",
          flush=True)
    m = propauc(); it = intenteval(); calc = calculationeval()
    print(f"[pre] prop mean-AUC(n>=5)={m[0]:.3f}  >=0.75:{m[1]}  >=0.85:{m[2]}  (of {m[3]})"
          + (f"  | intent acc={it[0]:.3f} auc={it[1]:.3f} bad-probes={it[2]}" if it else "")
          + (f" {list(it[4])}" if it and it[4] else "")
          + f"  | calc acc={calc[0]:.3f} {calc[1]}", flush=True)
    best = -1.0
    for step in range(1, args.steps + 1):
        ba = (
            rng.sample(tax, min(ntax, len(tax)))
            + rng.sample(base_pool, min(nbase, len(base_pool)))
            + [
                packed
                for label in intent_classes
                for packed in rng.sample(aug_by_class[label], intent_quota[label])
            ]
        )
        texts = sorted({t for p in ba for t in p["texts"]})
        V = enc.encode(texts, grad=True); tv = {t: V[i] for i, t in enumerate(texts)}
        x, E, kp, ct, cm = collate_live(ba, tv, nc, enc.hdim, dev)
        mse = ((model(x, E, kp)["content"][cm] - ct[cm]) ** 2).mean()
        calculation_batch = rng.sample(
            calculation_train, min(args.calculation_n, len(calculation_train))
        )
        calculation_loss = calculation_contrastive_loss(
            enc,
            calculation_batch,
            dev,
            temperature=args.calculation_temperature,
        )
        loss = mse + args.calculation_weight * calculation_loss
        opt.zero_grad(); loss.backward(); opt.step(); sched.step()
        if step % args.eval_every == 0 or step == 1:
            m = propauc(); it = intenteval(); calc = calculationeval()
            # COMBINED keep-best: property quality AND serving-mirror intent accuracy. The first run
            # selected on prop AUC alone and shipped a checkpoint whose drifted intent broke COUNT
            # at serving — intent now carries equal weight (both metrics live in [0,1]).
            score = m[0] + (it[0] if it else 0.0) + calc[0]
            eligible = (
                (it is None or it[0] >= args.min_intent_accuracy)
                and (it is None or it[2] <= args.max_bad_intent_probes)
                and calc[0] >= args.min_calculation_accuracy
            )
            flag = ""
            if eligible and score > best:
                best = score
                enc.qwen.save_pretrained(os.path.join(R, "qwen_lora_props"))
                torch.save(model.state_dict(), os.path.join(R, "encoder_props.pt"))
                torch.save(
                    {
                        "alloc": alloc,
                        "cfg": {**cfg, "nc": nc},
                        "intent_thresholds": it[3],
                    },
                    os.path.join(R, "encoder_props_meta.pt"),
                )
                flag = "  <- best saved"
            print(f"[{step:4d}] mse={mse.item():.4f} calc-loss={calculation_loss.item():.4f} "
                  f"prop mean-AUC(n>=5)={m[0]:.3f}  >=0.75:{m[1]}  >=0.85:{m[2]}"
                  + (f"  | intent acc={it[0]:.3f} auc={it[1]:.3f} bad-probes={it[2]}" if it else "")
                  + (f" {list(it[4])}" if it and it[4] else "")
                  + f"  | calc acc={calc[0]:.3f} {calc[1]}"
                  + ("  | below release floor" if not eligible else "")
                  + flag, flush=True)
    if best < 0:
        raise SystemExit("no checkpoint cleared the intent and calculation release floors")
    print(f"DONE  best property+intent+calculation = {best:.3f}; gate with "
          f"`python -m training.props.eval_intent --ckpt props` and the full regression suite", flush=True)


if __name__ == "__main__":
    main()
