#!/usr/bin/env python3
"""Held-out INTENT eval — mirrors serving's read_op_model so training can select on it.

The first property fine-tune regressed COUNT intent ("how many customers in France": SUM 0.121
edged COUNT 0.110 -> argmax flip -> read_op_model returned None) because train_props_gpu's
keep-best selected on property AUC only, over a test set with ZERO intent examples. This module
is the missing selection signal:

  * per-question OPERATOR prediction exactly like engine/encoder_overlay.read_op_model:
    score(op) = max over CANDIDATE question-token rows (operand tokens — column names + their
    split words — excluded, as serving does; fall back to all q rows if all collide) of the final
    content readout at that intent dim; accept iff score >= the checkpoint's independently calibrated
    gate;
    prediction = accepted argmax else None. Correct iff prediction == the variant's "expect".
  * per-dim intent AUC over the same held-out graphs (the smooth signal), via the shared
    evaluate()/fam_report() harness.

Eval data: data/intent_eval.jsonl written by augment_intent.py (hash-held-out variants + the
handcrafted serving probes). NEVER train on that file.

  python -m training.props.eval_intent --ckpt props    # the freshly trained checkpoint (default)
  python -m training.props.eval_intent --ckpt engine   # the shipped engine/data model (baseline)
"""
from __future__ import annotations
import argparse, os

import torch

from training.lib.edges import fam_dims_map
from training.lib.relblock import RelBlockModel
from training.lib.encoder import LiveQwen
from training.train.train_multitask import load, fam_report
from training.train.train_unified import pack, collate_live, evaluate

HERE = os.path.dirname(os.path.abspath(__file__))              # training/props/
TRAIN_DIR = os.environ.get("PREREASONER_TRAIN_DIR", HERE)
DATA = os.path.join(TRAIN_DIR, "data")
REPO = os.path.dirname(os.path.dirname(HERE))
ENGINE_DATA = os.environ.get("PREREASONER_ENGINE_DATA", os.path.join(REPO, "engine", "data"))

# Backward-compatible gates for checkpoints predating calibrated metadata.
LEGACY_THRESHOLDS = {"COUNT": 0.05, "SUM": 0.30, "AVG": 0.30}
OPS = {"COUNT": "intent_agg_count", "SUM": "intent_agg_sum", "AVG": "intent_agg_avg"}


def pack_eval(graphs, aid, fam_dims, nc, max_units=128):
    """pack() each eval graph, carrying (q-unit row indices, per-row text, the operand set, expect,
    probe name) alongside. pack preserves unit order, so raw-graph q indices == packed row indices.
    operand = non-q unit texts + their split words (== serving's column-name/cell exclusion set);
    scoring excludes q rows whose text is an operand, mirroring read_op_model's `cand` filter."""
    out = []
    for g in graphs:
        p = pack(g, aid, fam_dims, nc, max_units)
        if not p:
            continue
        qunits = [(i, u) for i, u in enumerate(g["units"]) if u.get("group") == "q"]
        p["qidx"] = [i for i, _ in qunits]
        p["qtext"] = [str(u["text"]).lower() for _, u in qunits]
        operand = set()
        for u in g["units"]:
            if u.get("group") != "q":
                t = str(u.get("text", "")).lower()
                operand.add(t); operand.update(t.split())
        p["operand"] = operand
        p["expect"] = g.get("expect")
        p["probe"] = g.get("probe")
        out.append(p)
    return out


def read_op_mirror(scores, thresholds=None):
    """The read_op_model accept rule on a {op: score} dict -> COUNT|SUM|AVG|None."""
    thresholds = thresholds or LEGACY_THRESHOLDS
    op, sc = max(scores.items(), key=lambda kv: kv[1])
    return op if sc >= thresholds[op] else None


@torch.no_grad()
def intent_score_rows(enc, model, packed, aid, nc, hdim, dev):
    """Collect serving-faithful scores while keeping checkpoint evaluation deterministic."""
    encoder_training = enc.qwen.training
    model_training = model.training
    enc.qwen.eval()
    model.eval()
    did = {op: aid[d] for op, d in OPS.items()}
    rows = []
    for i in range(0, len(packed), 4):
        batch = packed[i:i + 4]
        texts = sorted({t for p in batch for t in p["texts"]})
        V = enc.encode(texts, grad=False, max_len=48)                  # max_len 48 == serving (engine/tables MAX_LEN)
        tv = {t: V[j] for j, t in enumerate(texts)}
        x, E, kp, ct, cm = collate_live(batch, tv, nc, hdim, dev)
        cp = model(x, E, kp)["content"]
        for b, p in enumerate(batch):
            # candidate q rows = those whose text is NOT an operand (serving's cand filter); fall
            # back to all q rows if every token collides (serving does the same `or list(range)`).
            cand = [r for r, t in zip(p["qidx"], p["qtext"]) if t not in p["operand"]] or p["qidx"]
            scores = {}
            for op, d in did.items():
                col = cp[b, cand, d]
                scores[op] = float(col.max()) if col.numel() else 0.0   # empty-cand guard (== serving default 0.0)
            rows.append((p["expect"], p["probe"], scores))
    enc.qwen.train(encoder_training)
    model.train(model_training)
    return rows


def thresholds_from_score_rows(rows):
    """Fit conservative per-op gates on an independent calibration split.

    Argmax chooses the operation before thresholding at serving time, so each gate is calibrated
    only on rows where that operation wins. Ties prefer the higher threshold.
    """
    thresholds = {}
    for op in OPS:
        relevant = [
            (float(scores[op]), expect == op)
            for expect, _probe, scores in rows
            if max(scores, key=scores.get) == op
        ]
        if not relevant:
            thresholds[op] = LEGACY_THRESHOLDS[op]
            continue
        values = sorted({score for score, _ in relevant})
        epsilon = max(1.0, *(abs(value) for value in values)) * 1e-6
        candidates = (
            [values[0] - epsilon]
            + [(left + right) / 2.0 for left, right in zip(values, values[1:])]
            + [values[-1] + epsilon]
        )
        best_correct, best_threshold = -1, LEGACY_THRESHOLDS[op]
        for threshold in candidates:
            correct = sum((score >= threshold) == positive for score, positive in relevant)
            if (correct, threshold) > (best_correct, best_threshold):
                best_correct, best_threshold = correct, threshold
        thresholds[op] = float(best_threshold)
    return thresholds


def calibrate_intent_thresholds(enc, model, packed, aid, nc, hdim, dev):
    if not packed:
        raise ValueError("intent calibration split is empty")
    return thresholds_from_score_rows(
        intent_score_rows(enc, model, packed, aid, nc, hdim, dev)
    )


@torch.no_grad()
def intent_metrics(enc, model, packed, aid, nc, hdim, dev, thresholds=None):
    """Return heldout accuracy, per-class accuracy, named probes, and mean intent AUC."""
    thresholds = thresholds or LEGACY_THRESHOLDS
    rows = intent_score_rows(enc, model, packed, aid, nc, hdim, dev)
    n = ok = 0
    per = {}
    probe_rows = []
    for expect, probe, scores in rows:
        pred = read_op_mirror(scores, thresholds)
        correct = pred == expect
        n += 1; ok += correct
        key = str(expect)
        a, total = per.get(key, (0, 0)); per[key] = (a + correct, total + 1)
        if probe:
            probe_rows.append((probe, expect, pred, scores, correct))
    acc = ok / max(n, 1)
    # smooth signal: per-dim AUC over the same graphs (intent family; dims with positives)
    alloc_stub = {"dims": [{"name": k, "family": "intent", "dim_id": v} for k, v in aid.items()
                           if k.startswith("intent")]}
    means, _ = fam_report(alloc_stub, *evaluate(enc, model, packed, nc, hdim, dev), fams={"intent"})
    i_auc = means.get("intent")
    return acc, {k: (a / t if t else 0.0, t) for k, (a, t) in per.items()}, probe_rows, (i_auc or 0.0)


def load_eval(aid, fam_dims, nc, path=None, max_units=128):
    p = path or os.path.join(DATA, "intent_eval.jsonl")
    if not os.path.exists(p):
        return []
    return pack_eval(load(p), aid, fam_dims, nc, max_units)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", choices=["props", "engine"], default="props",
                    help="props = DATA/encoder_props* + qwen_lora_props; engine = the shipped engine/data model")
    ap.add_argument("--meta"); ap.add_argument("--model"); ap.add_argument("--lora")
    ap.add_argument("--eval", default=None, help="eval jsonl (default DATA/intent_eval.jsonl)")
    args = ap.parse_args()
    if args.ckpt == "engine":
        meta_p = args.meta or os.path.join(ENGINE_DATA, "encoder_meta.pt")
        model_p = args.model or os.path.join(ENGINE_DATA, "encoder.pt")
        lora_p = args.lora or os.path.join(ENGINE_DATA, "qwen_lora")
    else:
        meta_p = args.meta or os.path.join(DATA, "encoder_props_meta.pt")
        model_p = args.model or os.path.join(DATA, "encoder_props.pt")
        lora_p = args.lora or os.path.join(DATA, "qwen_lora_props")

    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    meta = torch.load(meta_p, map_location="cpu", weights_only=False)
    alloc, cfg = meta["alloc"], meta["cfg"]; nc = alloc["n_content"]
    aid = {d["name"]: d["dim_id"] for d in alloc["dims"]}
    fam_dims = fam_dims_map(alloc)
    enc = LiveQwen(dev, warm_lora=lora_p, serving=True)
    m = RelBlockModel(in_dim=cfg["in_dim"], H=cfg["H"], layers=cfg["layers"], heads=cfg["heads"],
                      nc=nc, n_edge=cfg["n_edge"]).to(dev)
    m.load_state_dict(torch.load(model_p, map_location="cpu")); m.eval()

    packed = load_eval(aid, fam_dims, nc, args.eval)
    if not packed:
        print("no eval graphs (run `python -m training.props.augment_intent` first)"); return 1
    thresholds = meta.get("intent_thresholds", LEGACY_THRESHOLDS)
    acc, per, probes, i_auc = intent_metrics(
        enc, m, packed, aid, nc, enc.hdim, dev, thresholds,
    )
    print(f"\nckpt={args.ckpt} ({os.path.basename(model_p)}, nc={nc}) | eval graphs: {len(packed)}")
    print("intent thresholds: " + " ".join(
        f"{op}={float(thresholds[op]):.4f}" for op in OPS
    ))
    print(f"intent op-accuracy (read_op_model mirror): {acc:.3f}   mean intent AUC: {i_auc:.3f}")
    for k in ("COUNT", "SUM", "AVG", "None"):
        if k in per:
            a, t = per[k]; print(f"    {k:6s} {a:.3f}  (n={t})")
    print("\nnamed probes (expect -> pred | scores):")
    for name, expect, pred, scores, correct in probes:
        s = " ".join(f"{k}={v:.3f}" for k, v in scores.items())
        print(f"  {'OK ' if correct else 'BAD'}  {name:32s} {str(expect):6s} -> {str(pred):6s} | {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
