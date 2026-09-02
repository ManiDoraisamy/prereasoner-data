"""Post-process: join Probe A structural flags (per_example_shape.json) with Probe D+ outcomes
(full_eval_per_example_full.json) into the clean four-way decomposition + a heuristic answered-wrong
stage attribution, cross-tabbed by difficulty. Prints markdown-ready tables for RESULTS.md §4.

The answered-wrong sub-attribution is HEURISTIC (compares pred vs gold SQL shape) and must be
spot-checked — it is reported as an indication, not ground truth.
"""
from __future__ import annotations
import collections
import json
import os
import re

HERE = os.path.dirname(__file__)
RES = os.path.abspath(os.path.join(HERE, "..", "results"))
DIFFS = ["easy", "medium", "hard", "extra"]


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def load(name):
    return json.load(open(os.path.join(RES, name), encoding="utf-8"))


def gold_flags(gold):
    g = gold.lower()
    return {
        "nested": bool(re.search(r"\(\s*select", g)),
        "setop": bool(re.search(r"\b(intersect|union|except)\b", g)),
        "join": " join " in g,
        "groupby": "group by" in g,
        "where": "where" in g,
        "orderby": "order by" in g,
        "agg": bool(re.search(r"\b(count|sum|avg|min|max)\s*\(", g)),
        "distinct": "distinct" in g,
    }


def attribute(rec):
    """Heuristic first-divergence stage for an ANSWERED-WRONG example."""
    gold = rec["gold"]; gf = gold_flags(gold)
    pred = (rec.get("sql") or "").lower()
    if gf["nested"] or gf["setop"]:
        return "structural(nested/setop)"
    if gf["join"] and " join " not in pred:
        return "join(missing)"
    pagg = bool(re.search(r"\b(count|sum|avg|min|max)\s*\(", pred))
    if not gf["agg"] and pagg:
        return "projection(forced-agg)"        # gold is a projection; we aggregated
    if gf["agg"] and not pagg:
        return "operator(missing-agg)"
    if gf["agg"] and pagg:
        gops = set(re.findall(r"\b(count|sum|avg|min|max)\s*\(", gold.lower()))
        pops = set(re.findall(r"\b(count|sum|avg|min|max)\s*\(", pred))
        if gops != pops:
            return "operator(wrong-agg)"
    if gf["where"] and "where" not in pred:
        return "filter(missing)"
    if gf["groupby"] and "group by" not in pred:
        return "grouping(missing)"
    if gf["orderby"] and "order by" not in pred:
        return "ordering(missing)"
    return "projection/other"


def main():
    import sys
    tag = sys.argv[1] if len(sys.argv) > 1 else "full"
    shape = {r["db_id"] + "|" + r["question"]: r for r in load("per_example_shape.json")}
    d = load(f"full_eval_per_example_{tag}.json")
    summ = load(f"full_eval_{tag}.json")

    # four-way decomposition per difficulty
    four = collections.defaultdict(collections.Counter)     # diff -> Counter of buckets
    err_stage = collections.Counter()
    wrong_attr = collections.Counter()
    path_of_correct = collections.Counter()
    for rec in d:
        diff = rec["difficulty"]
        sh = shape.get(rec["db_id"] + "|" + rec["question"], {})
        blocked = sh.get("blocked")
        c = four[diff]; c["n"] += 1
        if blocked:
            c["impossible"] += 1
            continue
        if not rec.get("ok"):
            c["error"] += 1
            err_stage[rec.get("stage", "?")] += 1
            continue
        if rec.get("lenient"):
            c["correct"] += 1
            path_of_correct[rec.get("path", "?")] += 1
        else:
            c["wrong"] += 1
            wrong_attr[attribute(rec)] += 1

    tot = collections.Counter()
    for diff in DIFFS:
        tot.update(four[diff])
    N = tot["n"]

    P = print
    P("### §4 tables (paste into RESULTS.md)\n")
    P(f"Headline (config=gold_tables, n={N}):")
    P(f"  scalar-gold accuracy (clean)  : {summ['scalar_gold_accuracy_pct']}%  (n={summ['scalar_gold_n']})")
    P(f"  correct lenient (generous UB) : {summ['correct_lenient_pct']}%")
    P(f"  correct strict  (harsh LB)    : {summ['correct_strict_pct']}%")
    P(f"  answered {summ['answered_pct']}%  error {summ['error_pct']}%")
    P(f"  routing: {summ.get('path_histogram')}   correct-by-path: {dict(path_of_correct)}")
    P("")
    P("Four-way decomposition (lenient=correct):")
    P("| difficulty | n | impossible | error | answered-wrong | correct |")
    P("|---|--:|--:|--:|--:|--:|")
    for diff in DIFFS:
        c = four[diff]
        P(f"| {diff} | {c['n']} | {c['impossible']} ({pct(c['impossible'],c['n'])}%) | "
          f"{c['error']} ({pct(c['error'],c['n'])}%) | {c['wrong']} ({pct(c['wrong'],c['n'])}%) | "
          f"{c['correct']} ({pct(c['correct'],c['n'])}%) |")
    P(f"| **all** | {N} | {tot['impossible']} ({pct(tot['impossible'],N)}%) | "
      f"{tot['error']} ({pct(tot['error'],N)}%) | {tot['wrong']} ({pct(tot['wrong'],N)}%) | "
      f"{tot['correct']} ({pct(tot['correct'],N)}%) |")
    P("")
    P(f"Error-stage histogram (of {tot['error']} errors): {dict(err_stage.most_common())}")
    P("")
    P(f"Answered-wrong heuristic attribution (of {tot['wrong']}, first divergence — SPOT-CHECK):")
    for k, v in wrong_attr.most_common():
        P(f"   {k:28s} {v:4d}  ({pct(v,tot['wrong'])}%)")

    json.dump({"four_way_by_difficulty": {d: dict(four[d]) for d in DIFFS},
               "totals": dict(tot), "error_stage": dict(err_stage),
               "wrong_attribution": dict(wrong_attr),
               "correct_by_path": dict(path_of_correct)},
              open(os.path.join(RES, f"decomposition_{tag}.json"), "w"), indent=2)
    P(f"\nwrote results/decomposition_{tag}.json")


if __name__ == "__main__":
    main()
