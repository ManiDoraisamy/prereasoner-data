"""Post-process: join Probe A structural flags (per_example_shape.json) with Probe D+ outcomes
(full_eval_per_example_<tag>.json) into the strict five-way decomposition + a heuristic
strict-miss family attribution, cross-tabbed by difficulty. Prints markdown-ready tables
for RESULTS.md.

The strict-miss family attribution is HEURISTIC (compares pred vs gold SQL shape) and must be
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


TABLE_RE = re.compile(r"\b(?:from|join)\s+([\"`\[]?)([A-Za-z_][A-Za-z0-9_]*)\1", re.I)


def sql_tables(sql):
    """Table identifiers named after FROM/JOIN. Regex-level, so subquery internals count too;
    that is why the nested/setop family is attributed BEFORE the table-set comparison."""
    return {m.group(2).lower() for m in TABLE_RE.finditer(sql or "")}


def attribute(rec):
    """Heuristic first-divergence stage for an ANSWERED-WRONG example."""
    gold = rec["gold"]; gf = gold_flags(gold)
    pred = (rec.get("sql") or "").lower()
    if gf["nested"] or gf["setop"]:
        return "structural(nested/setop)"
    gold_tabs, pred_tabs = sql_tables(gold), sql_tables(pred)
    if gold_tabs != pred_tabs:
        if gold_tabs < pred_tabs:
            return "table-set(superset/over-join)"
        if pred_tabs < gold_tabs:
            return "table-set(subset/missing-join)"
        return "table-set(different)"
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
    # Outcome-first: Probe A's static "blocked" flag is advisory (the planner strict-solves some
    # flagged examples), so it may only label a MISS, never hide a correct answer.
    # pool_oracle records score the SERVING outcome from the rank-0 pool entry (the record's own
    # strict/lenient are the oracle ceiling) and additionally count, per miss family, how many
    # misses have a strict-correct candidate anywhere in the pool ("rescuable by ranking alone").
    rescuable = collections.Counter()
    pool_mode = False
    for rec in d:
        diff = rec["difficulty"]
        sh = shape.get(rec["db_id"] + "|" + rec["question"], {})
        blocked = sh.get("blocked")
        pool = rec.get("pool")
        if pool is not None:
            pool_mode = True
            top1 = next((entry for entry in pool if entry["rank"] == 0), None)
            ok = top1 is not None and "error" not in top1
            strict = bool(top1 and top1.get("strict"))
            lenient = bool(top1 and top1.get("lenient"))
            rec = {**rec, "sql": (top1 or {}).get("sql")}
            in_pool = (rec.get("oracle_rank") or {}).get("strict") is not None
        else:
            ok, strict, lenient = rec.get("ok"), rec.get("strict"), rec.get("lenient")
            in_pool = False
        c = four[diff]; c["n"] += 1
        if strict:
            c["correct"] += 1
            path_of_correct[rec.get("path", "?")] += 1
            continue
        if not ok:
            c["error"] += 1
            err_stage[rec.get("stage", "?")] += 1
            continue
        if blocked:
            fam = "grammar-blocked(static, Probe A)"
            c["impossible"] += 1
        elif lenient:
            fam = "projection/row-shape(lenient-only)"
            c["lenient_only"] += 1
        else:
            fam = attribute(rec)
            c["wrong"] += 1
        wrong_attr[fam] += 1
        rescuable[fam] += bool(in_pool)

    tot = collections.Counter()
    for diff in DIFFS:
        tot.update(four[diff])
    N = tot["n"]

    misses = N - tot["correct"]
    P = print
    P("### Strict-miss decomposition (paste into RESULTS.md)\n")
    P(f"Headline (config={summ['config']}, selection={summ.get('selection')}, n={N}):")
    P(f"  scalar-gold accuracy (clean)  : {summ['scalar_gold_accuracy_pct']}%  (n={summ['scalar_gold_n']})")
    P(f"  correct lenient (generous UB) : {summ['correct_lenient_pct']}%")
    P(f"  correct strict  (harsh LB)    : {summ['correct_strict_pct']}%")
    P(f"  answered {summ['answered_pct']}%  error {summ['error_pct']}%")
    P(f"  routing: {summ.get('path_histogram')}   strict-correct-by-path: {dict(path_of_correct)}")
    P("")
    P("Five-way decomposition (strict=correct):")
    P("| difficulty | n | impossible | error | lenient-only | answered-wrong | strict-correct |")
    P("|---|--:|--:|--:|--:|--:|--:|")
    for diff in DIFFS + ["all"]:
        c = tot if diff == "all" else four[diff]
        label = "**all**" if diff == "all" else diff
        P(f"| {label} | {c['n']} | {c['impossible']} ({pct(c['impossible'],c['n'])}%) | "
          f"{c['error']} ({pct(c['error'],c['n'])}%) | {c['lenient_only']} ({pct(c['lenient_only'],c['n'])}%) | "
          f"{c['wrong']} ({pct(c['wrong'],c['n'])}%) | {c['correct']} ({pct(c['correct'],c['n'])}%) |")
    P("")
    P(f"Error-stage histogram (of {tot['error']} errors): {dict(err_stage.most_common())}")
    P("")
    P(f"Strict-miss families (of {misses} strict misses = {N} - {tot['correct']} strict-correct; "
      "first divergence, HEURISTIC — SPOT-CHECK):")
    if pool_mode:
        P("| family | count | % of strict misses | in-pool (rescuable by ranking) |")
        P("|---|--:|--:|--:|")
        P(f"| error (no answer) | {tot['error']} | {pct(tot['error'], misses)}% | 0 |")
        for k, v in wrong_attr.most_common():
            P(f"| {k} | {v} | {pct(v, misses)}% | {rescuable[k]} ({pct(rescuable[k], v)}%) |")
    else:
        P("| family | count | % of strict misses |")
        P("|---|--:|--:|")
        P(f"| error (no answer) | {tot['error']} | {pct(tot['error'], misses)}% |")
        for k, v in wrong_attr.most_common():
            P(f"| {k} | {v} | {pct(v, misses)}% |")
    if pool_mode and "pool_oracle" in summ:
        po = summ["pool_oracle"]
        P("")
        P(f"Pool-oracle ceiling: strict {summ['correct_strict_pct']}%  lenient {summ['correct_lenient_pct']}%  "
          f"(same-run serving top-1 strict {po['top1_strict']} = {pct(po['top1_strict'], N)}%)")

    json.dump({"five_way_by_difficulty": {d: dict(four[d]) for d in DIFFS},
               "totals": dict(tot), "strict_misses": misses,
               "error_stage": dict(err_stage),
               "strict_miss_families": dict(wrong_attr),
               "rescuable_in_pool": dict(rescuable),
               "strict_correct_by_path": dict(path_of_correct)},
              open(os.path.join(RES, f"decomposition_{tag}.json"), "w"), indent=2)
    P(f"\nwrote results/decomposition_{tag}.json")


if __name__ == "__main__":
    main()
