"""
gen20 — calibrate the world-leaf routing thresholds on the TRAINED model's OWN readout (NOT the ridge probe).

router.Router types an uploaded column by the trained gen20 model's per-dim readout. To decide *whether* a
column is a world type (city/country/u_s_state) vs not-a-world-column, we need a firing threshold per world leaf.
anchor_assignment.npz's thresholds were Youden-J calibrated on the RIDGE PROBE (W*Query17_encoding) — a different
readout scale than the trained qwen_lora+RelBlock model router actually runs, so they mis-fit (a real CITY column
reads city=0.059 but the probe threshold is 0.086 -> miss). This recalibrates against assignment.csv labels using
the same _profile() the router uses, so the gate matches the model.

Builds synthetic columns from assignment.csv tokens (name-like, stride-grouped for variety), runs the trained
model's column readout, and picks per-leaf Youden-J thresholds. Favors the diagonal (city-vs-country-vs-state are
each other's negatives) so the gate both REJECTS non-geo and SEPARATES the three world leaves. The downstream
embedding cell-resolver (Query16, null-below-threshold) is the second safety net per Mani's spec item 3.

  $env:PYTHONUTF8=1; python -m training.calibrate.calibrate_route
"""
from __future__ import annotations
import csv
import json
import sys
from pathlib import Path

import numpy as np

from training.lib.router import Router
from training.corpus.build_review import name_like

R19 = Path(__file__).resolve().parent.parent / "data"
# only the world leaves gen20 actually kept in alloc — the capped.entity leaf set dropped u_s_state (remapped to
# admin-territorial-entity, data-starved), so deriving this auto-drops it and would auto-include state if re-added.
_DIMS = {d["name"] for d in json.load(open(R19 / "alloc.json"))["dims"]}
WORLD_LEAVES = [lf for lf in ("city", "country", "u_s_state") if lf in _DIMS]
COLSIZE = 8                                          # cells per synthetic column
NEG_COLS = 48                                        # how many non-geo columns to build
TARGET_RECALL = 0.90                                 # recall-favoring gate: admit >=90% of real geo columns (the
                                                     # embedding cell-resolver, spec item 3, supplies precision)

# clean/FAMOUS probe columns — the live-demo distribution (famous capitals read LESS 'city' and more 'country/capital'
# than the obscure assignment.csv tokens, so Youden-J on tokens alone over-rejects them). Include them as positives.
PROBE_POS = {
    "city": [["Paris", "Tokyo", "London", "Berlin", "Madrid", "Rome", "Lyon", "Cairo"],
             ["Mumbai", "Osaka", "Toronto", "Sydney", "Lisbon", "Vienna", "Oslo", "Athens"],
             ["Paris", "Lyon", "Berlin", "Tokyo", "Madrid", "Rome"],                # the live-demo column (6 cells)
             ["Paris", "Lyon", "Marseille", "Berlin", "Munich"]],                   # small / few-cell columns
    "country": [["France", "Germany", "Japan", "Brazil", "Canada", "Italy", "Spain", "India"],
                ["Mexico", "Norway", "Egypt", "Kenya", "Chile", "Poland", "Greece", "Peru"],
                ["France", "Germany", "Japan", "Brazil", "Canada", "Italy"]],
    "u_s_state": [["California", "Texas", "Florida", "Ohio", "Georgia", "Nevada", "Oregon", "Maine"],
                  ["Arizona", "Colorado", "Kansas", "Michigan", "Vermont", "Utah", "Idaho", "Iowa"],
                  ["California", "Texas", "Florida", "Ohio", "Georgia", "Nevada"]],
}
# adversarial + realistic non-geo probes (must be REJECTED, or leak to the embedding gate)
PROBE_NEG = [["Alice Smith", "Bob Jones", "Carol White", "Dan Brown", "Eve Black", "Frank Green"],
             ["Widget A", "Gadget X", "Gizmo", "Doohickey", "Sprocket", "Thingamajig"],
             ["Red", "Blue", "Green", "Yellow", "Purple", "Orange"],
             ["shipped", "pending", "delivered", "cancelled", "returned", "processing"]]


CALIB_MODE = "serving-eval-v1"               # bump if the serving encoder mode changes (e.g. eval vs train dropout)


def _fingerprint():
    """A cheap content fingerprint of everything the profiles depend on — the trained model, the LoRA adapter, the
    labels, the dim alloc, and the encoder MODE. If any changes, the cached scores are invalid and we re-profile."""
    import hashlib
    parts = [CALIB_MODE]
    for rel in ("encoder.pt", "encoder_meta.pt", "qwen_lora/adapter_model.safetensors",
                "assignment.csv", "alloc.json"):
        p = R19 / rel
        parts.append(f"{rel}:{p.stat().st_size if p.exists() else 0}:{int(p.stat().st_mtime) if p.exists() else 0}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def _columns(tokens, colsize=COLSIZE):
    """stride-group a sorted token list into columns so each column spans the alphabet (variety, not 'all Aa-cities')."""
    toks = sorted({t for t in tokens if name_like(t)})
    ncol = max(1, len(toks) // colsize)
    cols = [[] for _ in range(ncol)]
    for i, t in enumerate(toks):
        cols[i % ncol].append(t)
    return [c for c in cols if len(c) >= 3]


def main():
    rows = list(csv.DictReader(open(R19 / "assignment.csv", encoding="utf-8")))
    pos_tokens = {lf: [r["Token"] for r in rows if r.get(lf) == "1"] for lf in WORLD_LEAVES}
    neg_tokens = [r["Token"] for r in rows if not any(r.get(lf) == "1" for lf in WORLD_LEAVES)]

    # build labeled columns: each world leaf's positives + a sample of non-geo negatives
    labeled = []                                                            # (label, [cells], is_probe)
    for lf in WORLD_LEAVES:
        for col in _columns(pos_tokens[lf]):
            labeled.append((lf, col, False))
        for col in PROBE_POS[lf]:                                           # famous/clean demo-distribution positives
            labeled.append((lf, col, True))
    negcols = _columns(neg_tokens)
    stride = max(1, len(negcols) // NEG_COLS)
    for col in negcols[::stride][:NEG_COLS]:
        labeled.append(("neg", col, False))
    for col in PROBE_NEG:                                                   # adversarial + realistic non-geo
        labeled.append(("neg", col, True))
    print("calibration columns: " + ", ".join(f"{lf}={sum(1 for l,_,_ in labeled if l==lf)}"
                                                for lf in WORLD_LEAVES + ["neg"]))

    # profile every column once -> the per-dim readout s; record the 3 world-leaf raw dims. Cache the (slow, CPU)
    # profiles so threshold-margin tuning is instant — but FINGERPRINT the cache on the model + data + encoder MODE so
    # it auto-invalidates when any of those change (else thresholds regenerate from stale scores). `--profile` forces.
    cache = R19 / "route_calib_scores.json"
    fp = _fingerprint()
    cached = json.load(open(cache)) if cache.exists() else None
    if cached and isinstance(cached, dict) and cached.get("fp") == fp and "--profile" not in sys.argv:
        S = [(label, d, p) for label, d, p in cached["scores"]]
        print(f"loaded {len(S)} cached profiles (fingerprint match; pass --profile to force)")
    else:
        if cached:
            print("cache MISS (model/data/mode changed or --profile) -> re-profiling in eval mode")
        r = Router()
        S = []                                                             # (label, {leaf: raw_dim}, is_probe)
        for k, (label, cells, is_probe) in enumerate(labeled):
            s = r._profile(cells, header=None)
            if s is None:
                continue
            S.append((label, {lf: float(s[r.di[lf]]) for lf in WORLD_LEAVES}, is_probe))
            if (k + 1) % 20 == 0:
                print(f"  profiled {k+1}/{len(labeled)}")
        json.dump({"fp": fp, "scores": S}, open(cache, "w"))

    # recall-favoring per leaf: threshold = the TARGET_RECALL-percentile of POSITIVE scores so >=90% of real geo
    # columns pass the gate. Negatives = every other column (other geo leaves + non-geo). The embedding resolver
    # (spec item 3) supplies precision downstream, so the gate is intentionally permissive (favors recall over FPR).
    thr = {}
    for lf in WORLD_LEAVES:
        pos = np.array([d[lf] for label, d, _ in S if label == lf])
        neg = np.array([d[lf] for label, d, _ in S if label != lf])
        probe = np.array([d[lf] for label, d, p in S if label == lf and p])     # famous/clean/demo columns
        # The city signal is weak+noisy (real cities ~0.05-0.08, clear non-geo ~0.01); favor RECALL hard (admit ~98% of
        # positives AND keep a margin below the weakest demo probe) — color-like adversarial leaks are gated by the
        # downstream embedding cell-resolver (spec item 3), not here. country/u_s_state separate cleanly so this barely
        # moves them. 2nd-percentile => ~98% recall; probe.min()*0.85 guarantees the live-demo columns clear the gate.
        t = min(float(np.quantile(pos, 0.02)), float(probe.min()) * 0.85)
        recall = (pos >= t).mean()
        fpr = (neg >= t).mean()
        thr[lf] = round(t, 4)
        print(f"  {lf:12s} thr={t:.4f}  recall={recall:.2f}  fpr={fpr:.2f}  "
              f"(P={len(pos)}, pos_min={pos.min():.3f}, probe_min={probe.min():.3f}, neg_max={neg.max():.3f})")

    out = R19 / "route_thresholds.json"
    json.dump(thr, open(out, "w"), indent=2)
    print(f"\nwrote {out}: {thr}")


if __name__ == "__main__":
    main()
