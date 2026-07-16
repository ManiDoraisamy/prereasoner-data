"""Probe D+ (headline): reproduce the PreReasoner system's text-to-SQL on Spider, fully OFFLINE.

The live stack routes each question (ComposedWorldQuery._composed):
  * a question whose LEARNED primitive head fires a DEPTH primitive
    (EXCL/RATIO/TOPN/SHARE/TIME/HAVING/SORT/DIVIDE/RUNNING) -> the ComposeEngine view stack;
  * everything else -> the delegate, whose SQL for a self-contained (no-world) table is the tables.py
    slot-filler (SELECT/projection, value-matched WHERE, >/<, dates, GROUP BY, ORDER BY, LIMIT, argmax,
    a single FK JOIN, COUNT/SUM/AVG/MIN/MAX).

Both SQL generators assemble a SQL STRING that is executor-agnostic; the live system runs it on Postgres,
the offline/test path (`TableQuery.execute` / `ComposeEngine.run`) runs the SAME SQL on in-memory SQLite.
For self-contained Spider DBs (world=None) the two are equivalent, so this is a FAITHFUL reproduction of the
system's SQL, minus (all honestly noted): world-knowledge resolution (irrelevant to Spider), and the clarify
gate (which would turn some wrong answers into refusals — refusals still score wrong on Spider, so omitting it
makes this an UPPER bound on the live number, not a lower one).

Denotation is compared on the REAL gold rows; we report the clean SCALAR-gold accuracy (unambiguous),
lenient containment (generous UB), and strict row-set equality (harsh LB) so the true number is bracketed.
"""
from __future__ import annotations
import argparse
import collections
import json
import os
import sys
import warnings

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

warnings.filterwarnings("ignore")

from hardness import eval_hardness
from evalutil import load_capped, build_mem_db, exec_sql_timed, run_with_timeout
from spider_eval import compare, gold_table_names, spider_foreign_keys

DIFFS = ["easy", "medium", "hard", "extra"]
# Mirrors the LIVE routing gate — keep in sync with engine.world_compose.ComposedWorldQuery.DEPTH_PRIMS.
# The Spider-only trim of TOPN/SORT/TIME was REVERTED: it lifted the benchmark but broke live composite view
# stacks (test_geo). This mirror tracks the live gate, so the Spider number here reflects real behavior.
DEPTH_PRIMS = frozenset({"EXCL", "RATIO", "TOPN", "SHARE", "TIME", "HAVING", "SORT", "DIVIDE", "RUNNING", "GROUP"})


def slot_predict(enc, tabs, question):
    """Drive the tables.py slot-filler: ingest -> schema -> plan -> assemble -> guard -> execute (SQLite)."""
    norm, fks = enc.ingest(tabs)
    sch, colidx, tmap = enc.schema(norm, fks)
    slots, join, involved, _ = enc.plan(question, sch, norm, fks)
    sql, toks = enc.assemble(slots, join, involved, sch)
    ok, why = enc.guard(sql)
    if not ok:
        return {"ok": False, "error": f"guard: {why}", "stage": "guard", "sql": sql, "path": "slot"}
    cols, rows = enc.execute(tmap, sch, sql)
    return {"ok": True, "sql": sql, "rows": [list(r) for r in rows], "path": "slot",
            "plan": ["+".join(k for k in ("agg" if slots["agg"] else "",
                                          "where" if slots["where"] else "",
                                          "group" if slots["group_by"] else "",
                                          "order" if slots["order_by"] else "",
                                          "join" if join else "") if k)]}


def ast_predict(enc, tabs, question, schema_fks=None, rank_model=None):
    """Run semantic AST ranking, then execution-rerank a bounded candidate prefix."""
    norm, discovered_fks = enc.ingest(tabs)
    fks = schema_fks if schema_fks is not None else discovered_fks
    sch, _, tmap = enc.schema(norm, fks)
    candidates = enc.search_ast(question, sch, norm, fks, rank_model=rank_model)
    from engine.sql_rank import execute_and_rerank
    from engine.sql_schema import SchemaGraph

    def execute(sql):
        ok, why = enc.guard(sql)
        if not ok:
            raise RuntimeError(f"guard: {why}")
        return enc.execute(tmap, sch, sql)

    graph = SchemaGraph.from_planner(sch, fks)
    executions = execute_and_rerank(question, candidates, graph, execute, max_candidates=5)
    errors = [execution.error for execution in executions if execution.error]
    for execution in executions:
        candidate = execution.candidate
        if execution.error:
            continue
        rows = execution.rows
        ok, why = enc.guard(candidate.sql)
        if not ok:
            errors.append(f"guard: {why}")
            continue
        return {"ok": True, "sql": candidate.sql, "rows": [list(row) for row in rows], "path": "ast",
                "plan": list(candidate.evidence), "candidate_count": len(candidates),
                "executed_candidate_count": len(executions), "candidate_score": round(candidate.score, 4),
                "rank_features": dict(candidate.features)}
    detail = errors[0] if errors else "no connected AST candidate"
    return {"ok": False, "error": detail, "stage": "ast_search", "path": "ast"}


def compose_predict(eng, tabs, question):
    res = eng.run(tabs, question, world=None)
    ans = res.get("answer")
    return {"ok": True, "sql": (res["views"][-1]["sql"] if res.get("views") else None),
            "rows": ans["rows"] if ans else None, "path": "compose",
            "plan": res.get("plan"), "primitives": res.get("primitives")}


# the DEPTH composition views — mirrors engine.world_compose.ComposedWorldQuery, SPLIT by whether the slot-filler
# can also express them. ENGINE_ONLY (yoy/running/share/divide/having) the slot-filler cannot do -> always stand on
# compose. SLOT_OVERLAP (topn/sort/time_filter) the slot-filler ALSO does WITH projection+WHERE -> stand on compose
# only when a WORLD join is in the plan (world_join/world_filter). On Spider world=None, so a world join NEVER
# appears -> SLOT_OVERLAP always falls to the slot-filler (recovering the projection/sort losses), while the live
# world composites (world join present) still stand. Keep in sync with world_compose.py.
_ENGINE_ONLY_VIEWS = {"yoy", "running", "share", "divide", "having"}
_SLOT_OVERLAP_VIEWS = {"topn", "sort", "time_filter"}


def predict(enc, eng, reader, tabs, question, planner="slot", schema_fks=None,
            rank_model=None):
    """Route like the live system (gate -> compose -> stand-or-fall-back -> delegate/slot). Any
    unrecovered exception is caught and attributed to a stage."""
    try:
        depth = bool(reader.present(question) & DEPTH_PRIMS)
    except Exception:                            # noqa: BLE001
        depth = False
    if depth:
        try:
            r = compose_predict(eng, tabs, question)
            plan = r.get("plan") or []
            world = any(op in ("world_join", "world_filter") for op in plan)   # never true on Spider (world=None)
            if (any(op in _ENGINE_ONLY_VIEWS for op in plan)
                    or (world and any(op in _SLOT_OVERLAP_VIEWS for op in plan))):
                return r                          # genuine composition (or a world composite) — stand on it
        except Exception:                         # noqa: BLE001 — live serve() delegates on engine error
            pass
    try:
        return ast_predict(enc, tabs, question, schema_fks, rank_model) if planner == "ast" else slot_predict(enc, tabs, question)
    except Exception as e:                        # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        stage = ("join_build" if ("ambiguous" in str(e) or ("join" in str(e).lower()))
                 else "assemble_exec")
        return {"ok": False, "error": msg, "stage": stage, "path": planner}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--dbs", required=True)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "results"))
    ap.add_argument("--per-diff", type=int, default=0)
    ap.add_argument("--config", default="gold_tables", choices=["gold_tables", "whole_db"])
    ap.add_argument("--planner", default="slot", choices=["slot", "ast"],
                    help="fallback text-to-SQL planner; ast enables deterministic AST search and ranking")
    ap.add_argument("--ranker-model", default="",
                    help="optional frozen Phase 6 ranker JSON (AST planner only)")
    ap.add_argument("--cap", type=int, default=5000, help="row cap per table (bounds exec; only wta_1 is capped)")
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    rank_model = None
    if args.ranker_model:
        if args.planner != "ast":
            ap.error("--ranker-model requires --planner ast")
        from engine.sql_learned_rank import load_ranker_model
        rank_model = load_ranker_model(args.ranker_model)

    dev = json.load(open(os.path.join(args.data, "dev.json"), encoding="utf-8"))
    tables_meta = {t["db_id"]: t for t in json.load(open(os.path.join(args.data, "tables.json"), encoding="utf-8"))}
    ast_fks = {db_id: spider_foreign_keys(meta) for db_id, meta in tables_meta.items()} if args.planner == "ast" else {}

    buckets = collections.defaultdict(list)
    for i, ex in enumerate(dev):
        buckets[eval_hardness(ex["sql"])].append(i)
    picked = []
    for d in DIFFS:
        picked += (buckets[d][:args.per_diff] if args.per_diff else buckets[d])
    picked.sort()

    print("loading encoder (Qwen LoRA + relational readout, CPU)...", flush=True)
    from engine.encoder_overlay import EncoderQuery
    from engine.primitive_head import PrimitiveReader
    from engine.compose import ComposeEngine
    enc = EncoderQuery()
    reader = PrimitiveReader(encoder=enc)
    eng = ComposeEngine(reader=reader)
    print(f"loaded. evaluating {len(picked)} examples (config={args.config})\n", flush=True)

    db_cache = {}
    def get_db(db_id):
        if db_id not in db_cache:
            capped = load_capped(os.path.join(args.dbs, db_id + ".sqlite"), cap=args.cap)
            gcon = build_mem_db(list(capped.values()))          # gold runs on the SAME capped data
            db_cache[db_id] = (capped, gcon)
        return db_cache[db_id]

    stat = collections.defaultdict(collections.Counter)      # diff -> Counter
    stage_hist = collections.Counter()
    path_hist = collections.Counter()
    path_correct = collections.Counter()
    per_example = []

    for n, i in enumerate(picked):
        ex = dev[i]; db_id = ex["db_id"]; diff = eval_hardness(ex["sql"])
        capped, gcon = get_db(db_id)
        gold_rows, gerr = exec_sql_timed(gcon, ex["query"], timeout=8.0)
        if args.config == "gold_tables":
            names = [t.lower() for t in gold_table_names(ex, tables_meta)]
            tabs = [capped[t] for t in names if t in capped] or list(capped.values())
        else:
            tabs = list(capped.values())

        r, terr = run_with_timeout(
            lambda: predict(
                enc, eng, reader, tabs, ex["question"], args.planner,
                ast_fks.get(db_id), rank_model,
            ),
            args.timeout,
        )
        if terr is not None:
            r = {"ok": False, "error": str(terr), "stage": "timeout", "path": "timeout"}
        cmp = compare(gold_rows, r.get("rows")) if r["ok"] else {}
        path_hist[r["path"]] += 1
        st = stat[diff]; st["n"] += 1
        if gerr:
            st["gold_exec_error"] += 1
        if not r["ok"]:
            st["error"] += 1
            stage_hist[r["stage"]] += 1
        else:
            st["answered"] += 1
            if cmp.get("lenient"):
                st["correct_lenient"] += 1
                path_correct[r["path"]] += 1
            if cmp.get("gold_scalar"):
                st["scalar_total"] += 1
                if cmp.get("scalar_exact"):
                    st["scalar_correct"] += 1
            if cmp.get("strict"):
                st["correct_strict"] += 1
        per_example.append({"idx": i, "db_id": db_id, "difficulty": diff, "question": ex["question"],
                            "gold": ex["query"], "gold_exec_error": gerr, **r, **cmp})
        if (n + 1) % 50 == 0:
            print(f"  {n+1}/{len(picked)}", flush=True)

    tot = collections.Counter()
    for d in DIFFS:
        tot.update(stat[d])
    N = max(tot["n"], 1)
    summary = {
        "n": tot["n"], "config": args.config, "planner": args.planner,
        "ranker_model": args.ranker_model or None,
        "answered_pct": round(100 * tot["answered"] / N, 1),
        "error_pct": round(100 * tot["error"] / N, 1),
        "correct_lenient_pct": round(100 * tot["correct_lenient"] / N, 1),
        "correct_strict_pct": round(100 * tot["correct_strict"] / N, 1),
        "scalar_gold_accuracy_pct": round(100 * tot["scalar_correct"] / max(tot["scalar_total"], 1), 1),
        "scalar_gold_n": tot["scalar_total"],
        "totals": dict(tot),
        "error_stage_histogram": dict(stage_hist),
        "path_histogram": dict(path_hist),
        "path_correct_lenient": dict(path_correct),
        "by_difficulty": {d: dict(stat[d]) for d in DIFFS},
    }
    os.makedirs(args.out, exist_ok=True)
    suf = ("_" + args.tag) if args.tag else ""
    json.dump(summary, open(os.path.join(args.out, f"full_eval{suf}.json"), "w"), indent=2)
    json.dump(per_example, open(os.path.join(args.out, f"full_eval_per_example{suf}.json"), "w"), indent=2)

    P = print
    P("\n" + "=" * 78); P("PROBE D+ — FULL OFFLINE SYSTEM (slot-filler + compose, live routing)"); P("=" * 78)
    P(f"config={args.config}   examples={tot['n']}")
    P(f"  routed: {dict(path_hist)}   (correct-lenient by path: {dict(path_correct)})")
    P(f"  answered : {tot['answered']:4d} ({summary['answered_pct']}%)   error {tot['error']} ({summary['error_pct']}%)  stages={dict(stage_hist)}")
    P(f"  CORRECT lenient (generous UB): {tot['correct_lenient']:4d} ({summary['correct_lenient_pct']}%)")
    P(f"  CORRECT strict  (harsh LB)   : {tot['correct_strict']:4d} ({summary['correct_strict_pct']}%)")
    P(f"  SCALAR-gold accuracy (clean) : {tot['scalar_correct']}/{tot['scalar_total']} ({summary['scalar_gold_accuracy_pct']}%)")
    P(f"  by difficulty:")
    for d in DIFFS:
        s = stat[d]
        P(f"     {d:8s} n={s['n']:4d}  answered={s['answered']:4d}  lenient={s['correct_lenient']:4d}  "
          f"strict={s['correct_strict']:4d}  scalar={s['scalar_correct']}/{s['scalar_total']}")
    P(f"\nwrote results/full_eval{suf}.json (+ per_example)")


if __name__ == "__main__":
    main()
