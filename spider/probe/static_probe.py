"""Probe A (architectural envelope) + Probe B (taxonomy/world-knowledge coverage) — STATIC.

Neither needs the model or Postgres: they read the gold SQL (dev.json, with the official pre-parsed
`sql` dict) and the schemas (tables.json), and characterise how much of Spider even falls inside the
PreReasoner engine's structural reach — the ceiling any amount of linking work could hit.

Envelope is grounded in what the engine's planners actually emit (verified against engine/compose.py,
engine/primitives.py, engine/tables.py):
  * ONE flattened base relation: a single table, or an FK star-join of the DB's tables, optionally a
    world-meaning join. Analytical primitives (filter / group_agg / having / topn / sort / yoy /
    running / share / divide, agg in COUNT/SUM/AVG/MIN/MAX) then stack on that one base.
  * NO nested-subquery primitive, NO set-operation (INTERSECT/UNION/EXCEPT) primitive, and the
    single FK join_plan cannot express a SELF-join. These are hard, planner-agnostic blockers.
"""
from __future__ import annotations
import argparse
import collections
import json
import os

from hardness import eval_hardness, shape

DIFFS = ["easy", "medium", "hard", "extra"]


def pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def classify_envelope(sh):
    """Return (blocked_reason | None). None => structurally reachable by the compose/slot planners."""
    if sh["set_op"]:
        return "set_op"                       # INTERSECT / UNION / EXCEPT — no primitive
    if sh["nested_pred"] or sh["from_subquery"]:
        return "nested_subquery"              # correlated / IN(SELECT) / FROM(SELECT) — no primitive
    if sh["self_join"]:
        return "self_join"                    # same table twice — the single FK join_plan can't express it
    return None


def simple_single_table(sh):
    """The absolute-simplest tier: one table, one (or zero) predicate, no composition."""
    return (sh["n_from_tables"] <= 1 and not sh["join"] and not sh["group_by"] and not sh["having"]
            and not sh["order_by"] and not sh["limit"] and not sh["set_op"] and not sh["nested_pred"]
            and not sh["from_subquery"] and sh["n_where"] <= 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "results"))
    args = ap.parse_args()

    dev = json.load(open(os.path.join(args.data, "dev.json"), encoding="utf-8"))
    tables = {t["db_id"]: t for t in json.load(open(os.path.join(args.data, "tables.json"), encoding="utf-8"))}

    # ---------------- Probe A: envelope ----------------
    by_diff = collections.Counter()
    blocked_by_diff = collections.defaultdict(collections.Counter)   # diff -> reason -> n
    reachable_by_diff = collections.Counter()
    simple_by_diff = collections.Counter()
    feat_counts = collections.Counter()
    reason_total = collections.Counter()
    agg_total = collections.Counter()
    per_example = []

    for ex in dev:
        sh = shape(ex["sql"])
        diff = eval_hardness(ex["sql"])
        by_diff[diff] += 1
        reason = classify_envelope(sh)
        if reason:
            blocked_by_diff[diff][reason] += 1
            reason_total[reason] += 1
        else:
            reachable_by_diff[diff] += 1
            # feature histogram over the *reachable* set (what the linking must handle)
            for k in ("join", "group_by", "having", "order_limit", "distinct", "has_or", "has_like"):
                if sh[k]:
                    feat_counts[k] += 1
            if sh["n_where"] >= 2:
                feat_counts["multi_where(>=2)"] += 1
            if sh["n_select"] >= 2:
                feat_counts["multi_select(>=2)"] += 1
            if simple_single_table(sh):
                simple_by_diff[diff] += 1
        for a in sh["aggs"]:
            agg_total[a] += 1
        per_example.append({"db_id": ex["db_id"], "difficulty": diff, "blocked": reason,
                            "question": ex["question"], "gold": ex["query"], **sh})

    total = len(dev)
    n_blocked = sum(reason_total.values())
    n_reach = total - n_blocked
    n_simple = sum(simple_by_diff.values())

    # ---------------- Probe B: taxonomy / world-knowledge coverage ----------------
    # The 42 live leaves are Wikidata entity TYPES; only city + country are backed by world tables.
    WORLD_HINTS = {"city", "cities", "town", "country", "countries", "nation", "nationality",
                   "state", "province", "continent"}
    dbs = sorted({ex["db_id"] for ex in dev})
    dbs_with_world_col = []
    world_col_hits = collections.Counter()
    total_cols = 0
    for db in dbs:
        t = tables[db]
        cols = [c[1].lower() for c in t["column_names_original"] if c[0] >= 0]
        total_cols += len(cols)
        hit = sorted({w for c in cols for w in WORLD_HINTS
                      if w == c or w in c.replace("_", " ").split()})
        if hit:
            dbs_with_world_col.append((db, hit))
            for w in hit:
                world_col_hits[w] += 1

    summary = {
        "total_examples": total,
        "by_difficulty": {d: by_diff[d] for d in DIFFS},
        "envelope": {
            "hard_blocked": {"n": n_blocked, "pct": pct(n_blocked, total),
                             "by_reason": {r: {"n": reason_total[r], "pct": pct(reason_total[r], total)}
                                           for r in reason_total}},
            "reachable_optimistic_ceiling": {"n": n_reach, "pct": pct(n_reach, total)},
            "simple_single_table": {"n": n_simple, "pct": pct(n_simple, total)},
            "reachable_feature_histogram": {k: {"n": v, "pct_of_reachable": pct(v, n_reach)}
                                            for k, v in feat_counts.most_common()},
        },
        "aggregate_ops_over_all": dict(agg_total),
        "envelope_cross_tab_by_difficulty": {
            d: {"total": by_diff[d],
                "hard_blocked": sum(blocked_by_diff[d].values()),
                "blocked_by_reason": dict(blocked_by_diff[d]),
                "reachable": reachable_by_diff[d],
                "simple_single_table": simple_by_diff[d],
                "reachable_pct": pct(reachable_by_diff[d], by_diff[d])}
            for d in DIFFS},
        "coverage": {
            "world_knowledge_required_by_gold": 0,   # Spider DBs are self-contained: gold never joins external facts
            "world_table_leaves": ["city", "country"],
            "dev_dbs": len(dbs),
            "dbs_with_a_world_typable_column": len(dbs_with_world_col),
            "world_col_header_hits": dict(world_col_hits),
            "dbs_with_world_col_detail": dbs_with_world_col,
            "total_columns_in_dev_schemas": total_cols,
        },
    }

    os.makedirs(args.out, exist_ok=True)
    json.dump(summary, open(os.path.join(args.out, "static_probe.json"), "w"), indent=2)
    json.dump(per_example, open(os.path.join(args.out, "per_example_shape.json"), "w"), indent=2)

    # ---------------- console report ----------------
    P = print
    P("=" * 78); P("PROBE A — ARCHITECTURAL ENVELOPE (static, gold SQL)"); P("=" * 78)
    P(f"dev examples: {total}")
    P("difficulty mix: " + ", ".join(f"{d}={by_diff[d]} ({pct(by_diff[d],total)}%)" for d in DIFFS))
    P("")
    P(f"HARD-BLOCKED (no planner can emit): {n_blocked} ({pct(n_blocked,total)}%)")
    for r, n in reason_total.most_common():
        P(f"    {r:16s} {n:4d}  ({pct(n,total)}%)")
    P(f"REACHABLE (optimistic structural ceiling): {n_reach} ({pct(n_reach,total)}%)")
    P(f"    of which SIMPLE single-table (no composition): {n_simple} ({pct(n_simple,total)}%)")
    P("")
    P("Reachable-set feature histogram (what the linking/operand binding must handle):")
    for k, v in feat_counts.most_common():
        P(f"    {k:20s} {v:4d}  ({pct(v,n_reach)}% of reachable)")
    P("")
    P("Cross-tab by Spider difficulty:")
    P(f"    {'diff':8s} {'total':>6s} {'blocked':>8s} {'reachable':>10s} {'reach%':>7s} {'simple':>7s}")
    for d in DIFFS:
        b = sum(blocked_by_diff[d].values())
        P(f"    {d:8s} {by_diff[d]:6d} {b:8d} {reachable_by_diff[d]:10d} "
          f"{pct(reachable_by_diff[d],by_diff[d]):6.1f}% {simple_by_diff[d]:7d}")
    P("")
    P("=" * 78); P("PROBE B — TAXONOMY / WORLD-KNOWLEDGE COVERAGE (static)"); P("=" * 78)
    P("world-knowledge required by ANY gold query: 0  (Spider DBs are self-contained —")
    P("  every gold query references only its own DB; the engine's world-join adds nothing here)")
    P(f"world-table leaves available: city, country")
    P(f"dev DBs: {len(dbs)}  |  DBs with a header that could type to city/country/state: "
      f"{len(dbs_with_world_col)}")
    P(f"header hits: {dict(world_col_hits)}")
    P(f"total columns across dev schemas: {total_cols}")
    P("")
    P("wrote: results/static_probe.json, results/per_example_shape.json")


if __name__ == "__main__":
    main()
