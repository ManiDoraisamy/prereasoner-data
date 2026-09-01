"""
gen20 — DATA VALIDATION GATE. Asserts the invariants a clean anchoring set must satisfy, and EXITS NON-ZERO on any
violation, so a leaky / contradictory / duplicated set can never silently be declared "final" again (Mani had to find
these by hand once — never again).

ERRORS (fail the pipeline):
  - any (Token, Category) with >1 distinct target  (a token is ONE vector -> ONE target; contradictions can't be learnt)
  - any exact Example shared by assignment + inference            (column-level leak)
  - any cell_value Token shared by assignment + inference         (entity-level leak; column-name aliases are exempt)
  - any exact-duplicate training row                              (x32 overweighting)
  - any active taxonomy leaf (accepted|added) with 0 training rows  (a target the data never trains)
  - dim columns must equal alloc's dims, in order
WARN (report only): active leaves with no TEST row (unevaluated); clusters below the coherence threshold.

  $env:PYTHONUTF8=1; python -m training.calibrate.validate_data
"""
from __future__ import annotations
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from training.corpus import build_review as br                             # noqa: E402

OUT = ROOT / "training/data"
DIMS = br.STRUCT + br.NODE_DIMS + br.INTENT


def rows(p):
    return list(csv.DictReader(open(p, encoding="utf-8")))


def main():
    tr = rows(OUT / "assignment.csv")
    te = rows(OUT / "inference.csv")
    tax = {r["qid"]: r for r in rows(OUT / "taxonomy.csv")}
    active = {q for q, r in tax.items() if r["status"] in ("accepted", "added")}
    errs, warns = [], []

    # 1. contradictions — TRAIN ONLY (inference.csv dims are blank by design = the probe's to fill), CASE-INSENSITIVE
    # (physics/PHYSICS are one token to the encoder).
    sig = defaultdict(set)
    for r in tr:
        if r["Category"] in ("column_name", "cell_value"):
            sig[(br.norm(r["Token"]), r["Category"])].add(tuple(d for d in DIMS if r.get(d) == "1"))
    con = [k for k, v in sig.items() if len(v) > 1]
    if con:
        errs.append(f"{len(con)} contradictory (norm Token,Category) targets, e.g. {[k[0] for k in con[:4]]}")

    # 2. split leakage — exact Example + CASE-INSENSITIVE cell_value entity overlap (column-name aliases exempt: closed vocab)
    ov_ex = {r["Example"] for r in tr} & {r["Example"] for r in te}
    if ov_ex:
        errs.append(f"{len(ov_ex)} Example(s) in BOTH assignment+inference")
    ov_val = {br.norm(r["Token"]) for r in tr if r["Category"] == "cell_value"} & \
             {br.norm(r["Token"]) for r in te if r["Category"] == "cell_value"}
    if ov_val:
        errs.append(f"{len(ov_val)} cell_value entity(ies) in BOTH splits (leak), e.g. {sorted(ov_val)[:5]}")

    # 3. duplication
    full = Counter((r["Source"], r["Token"], r["Example"], r["Category"]) for r in tr)
    dup = [k for k, c in full.items() if c > 1]
    if dup:
        errs.append(f"{len(dup)} duplicate training rows (worst x{max(full.values())})")

    # 4. every active leaf has training data
    src_tr = Counter(r["Source"] for r in tr)
    src_te = Counter(r["Source"] for r in te)
    no_train = sorted(q for q in active if src_tr.get(q, 0) == 0)
    if no_train:
        errs.append(f"{len(no_train)} active leaves with 0 train rows (dead targets): "
                    f"{[(q, br.LEAF_QID and [l for l,qq in br.LEAF_QID.items() if qq==q][:1]) for q in no_train[:4]]}")
    no_test = sorted(q for q in active if src_te.get(q, 0) == 0)
    if no_test:
        warns.append(f"{len(no_test)} active leaves with no TEST row (unevaluated, e.g. {no_test[:4]})")

    # 5. SCHEMA CONSISTENCY across all four artifacts (assignment == alloc == npz) + inference must be FILLED. This is
    # the check that would have caught the stale-npz / blank-inference state: anchor must run AFTER build_review.
    import numpy as np                                                      # noqa: E402
    alloc = json.load(open(OUT / "alloc.json"))
    adims = [d["name"] for d in alloc["dims"]]
    csv_dims = [c for c in tr[0] if c in set(DIMS)]
    if csv_dims != adims:
        errs.append(f"assignment dim columns != alloc ({len(csv_dims)} vs {len(adims)})")
    npzf = OUT / "anchor_assignment.npz"
    if npzf.exists():
        z = np.load(npzf, allow_pickle=False)
        npz_dims = [str(d) for d in z["dims"]] if "dims" in z else []
        if npz_dims != adims:
            errs.append(f"anchor_assignment.npz dims != alloc ({len(npz_dims)} vs {len(adims)}) — STALE head, re-anchor")
    else:
        errs.append("anchor_assignment.npz missing — run anchor_assignment")
    nfilled = sum(1 for r in te if r.get("PASS", "") != "")
    if nfilled != len(te):
        errs.append(f"inference.csv NOT filled: {nfilled}/{len(te)} rows have PASS (anchor must run after build_review)")

    # coherence warn
    cl = json.load(open(OUT / "clusters.json", encoding="utf-8"))
    inc = sum(1 for c in cl if c.get("coherence", 1.0) < 0.80)
    warns.append(f"{inc} clusters below coherence 0.80 (skipped by reconcile)")

    print(f"assignment {len(tr)} rows / inference {len(te)} rows / {len(active)} active leaves")
    for w in warns:
        print(f"  WARN: {w}")
    if errs:
        print("VALIDATION FAILED:")
        for e in errs:
            print(f"  ERROR: {e}")
        sys.exit(1)
    print("VALIDATION PASSED — clean (no contradictions, no leak, no dups, every active leaf trained).")


if __name__ == "__main__":
    main()
