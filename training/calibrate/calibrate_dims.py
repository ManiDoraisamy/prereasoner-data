"""
gen20 — calibrate per-dim thresholds for /dimension (dim19) on the TRAINED model's OWN readout, REPLACING the
ridge-probe thresholds dim19 currently reads from anchor_assignment.npz.

Codex P1: anchor_assignment.npz's per-dim thresholds were Youden-J'd on the FROZEN Query17 ridge probe
(W * Query17_encoding) — a different readout scale than the qwen_lora + RelBlock model that dim19 actually runs. So
/dimension can show missing or spurious taxonomy dims. This recalibrates against the SAME Router._profile() readout the
served model uses: for every TAXONOMY node dim + STRUCT datatype dim, build labeled synthetic columns from
assignment.csv (a leaf L's column fires its whole P279 path LEAF_PATH[L]; a cell_value's struct dim = is_num/is_str),
profile each column once with the trained model, and pick the Youden-J threshold that best separates the columns that
SHOULD fire the dim from those that should not. Writes dim_thresholds.json; dim19 loads it, falling back to
anchor_assignment.npz only when it is absent.

  $env:PYTHONUTF8=1; python -m training.calibrate.calibrate_dims
"""
from __future__ import annotations
import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.lib.router import Router                                          # noqa: E402
from training.corpus.build_review import name_like, NODE_DIMS, STRUCT, LEAF_PATH  # noqa: E402

R19 = ROOT / "training/data"
COLSIZE = 8
MIN_POS = 3                                          # need >=3 positive columns to calibrate a dim (else keep ridge)


def _columns(tokens, colsize=COLSIZE):
    """stride-group a sorted token list into synthetic columns spanning the alphabet (variety, not 'all Aa')."""
    toks = sorted({t for t in tokens if name_like(t)})
    ncol = max(1, len(toks) // colsize)
    cols = [[] for _ in range(ncol)]
    for i, t in enumerate(toks):
        cols[i % ncol].append(t)
    return [c for c in cols if len(c) >= 3]


def _youden(pos, neg):
    """cut maximizing TPR-FPR over candidate thresholds (every distinct score). None if either side is empty."""
    if len(pos) == 0 or len(neg) == 0:
        return None
    cand = sorted(set(np.concatenate([pos, neg]).tolist()))
    best_t, best_j = cand[0], -2.0
    for t in cand:
        j = (pos >= t).mean() - (neg >= t).mean()
        if j > best_j:
            best_j, best_t = j, t
    return float(best_t)


def main():
    rows = list(csv.DictReader(open(R19 / "assignment.csv", encoding="utf-8")))
    leaves = [lf for lf in LEAF_PATH if lf in NODE_DIMS]
    pos_tokens = {lf: [r["Token"] for r in rows if r.get("Category") == "cell_value" and r.get(lf) == "1"]
                 for lf in leaves}
    # labeled columns: (cells, fired-taxonomy-dim set = the leaf's full path, is_str/is_num)
    labeled = []
    for lf in leaves:
        path = set(LEAF_PATH[lf])
        for col in _columns(pos_tokens[lf]):
            labeled.append((col, path))
    print(f"{len(labeled)} labeled columns over {len(leaves)} leaves; profiling with the trained model...", flush=True)

    r = Router()
    profs = []                                                               # (score_vector, fired-dim set)
    for k, (col, fired) in enumerate(labeled):
        s = r._profile(col, header=None)
        if s is not None:
            profs.append((s, fired))
        if (k + 1) % 25 == 0:
            print(f"  profiled {k+1}/{len(labeled)}", flush=True)

    # per TAXONOMY node dim: positives = columns whose leaf-path includes the dim; negatives = the rest. Youden-J.
    thr, kept = {}, 0
    for d in NODE_DIMS:
        if d not in r.di:
            continue
        pos = np.array([float(s[r.di[d]]) for s, fired in profs if d in fired])
        neg = np.array([float(s[r.di[d]]) for s, fired in profs if d not in fired])
        if len(pos) < MIN_POS:
            continue
        t = _youden(pos, neg)
        if t is not None:
            thr[d] = round(t, 4); kept += 1
    # struct datatype dims fire by token kind (is_num for numeric cells). Calibrate is_str/is_num the same way using a
    # numeric vs name-like column split so /dimension's datatype tags also match the trained readout.
    num_cols = _columns([r_["Token"] for r_ in rows if r_.get("is_num") == "1"][:400])
    str_cols = _columns([r_["Token"] for r_ in rows if r_.get("is_str") == "1" and r_.get("Category") == "cell_value"][:400])
    sprofs = [("num", r._profile(c, header=None)) for c in num_cols] + \
             [("str", r._profile(c, header=None)) for c in str_cols]
    sprofs = [(lab, s) for lab, s in sprofs if s is not None]
    for d, lab in (("is_num", "num"), ("is_str", "str")):
        if d not in r.di:
            continue
        pos = np.array([float(s[r.di[d]]) for l_, s in sprofs if l_ == lab])
        neg = np.array([float(s[r.di[d]]) for l_, s in sprofs if l_ != lab])
        t = _youden(pos, neg)
        if t is not None:
            thr[d] = round(t, 4); kept += 1

    out = R19 / "dim_thresholds.json"
    json.dump(thr, open(out, "w"), indent=2)
    print(f"\nwrote {out}: {kept} trained-model per-dim thresholds "
          f"(taxonomy median={np.median([v for k,v in thr.items() if k in NODE_DIMS]):.4f})", flush=True)


if __name__ == "__main__":
    main()
