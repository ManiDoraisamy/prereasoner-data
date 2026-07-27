"""Probe D+ (headline): reproduce the PreReasoner system's text-to-SQL on Spider, fully OFFLINE.

The live stack routes each question (ComposedKnowledgeQuery._composed):
  * a question whose LEARNED primitive head fires a DEPTH primitive
    (EXCL/RATIO/TOPN/SHARE/TIME/HAVING/SORT/DIVIDE/RUNNING) -> the ComposeEngine view stack;
  * everything else -> the delegate, whose SQL for a self-contained (no-world) table is the deterministic
    typed-AST planner (tables.py search_ast -> engine.sql_search): a beam over a typed SQL AST that emits
    projection, value-matched WHERE, >/<, dates, GROUP BY, ORDER BY, LIMIT, argmax, FK JOINs,
    COUNT/SUM/AVG/MIN/MAX, plus recursive subqueries, aggregate constraints, disjunctions, and set/extrema
    shapes, then ranks the pool with sql_rank.

Both routes assemble a SQL STRING that is executor-agnostic; the live system runs it on Postgres,
the offline/test path (`TableQuery.execute` / `ComposeEngine.run`) runs the SAME SQL on in-memory SQLite.
For self-contained Spider DBs (world=None) the two are equivalent, so this is a FAITHFUL reproduction of the
system's SQL, minus (all honestly noted): world-knowledge resolution (irrelevant to Spider), and the clarify
gate (which would turn some wrong answers into refusals — refusals still score wrong on Spider, so omitting it
makes this an UPPER bound on the live number, not a lower one).

Serving-faithful selection is --selection serving_top1 (exact live top-1); --selection execution_checks adds
bounded deterministic execution reranking. Denotation is compared on the REAL gold rows; we report the clean
SCALAR-gold accuracy (unambiguous), lenient containment (generous UB), and strict row-set equality (harsh LB)
so the true number is bracketed.
"""
from __future__ import annotations
import argparse
import collections
import json
import os
import sys
import time
import warnings

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

warnings.filterwarnings("ignore")

from hardness import eval_hardness
from evalutil import load_capped, build_mem_db, exec_sql_timed, run_with_budget
from spider_eval import (
    compare,
    record_integrated_result,
    recursive_gold_table_names,
    spider_foreign_keys,
)

DIFFS = ["easy", "medium", "hard", "extra"]
# Mirrors the LIVE routing gate — keep in sync with engine.knowledge_compose.ComposedKnowledgeQuery.DEPTH_PRIMS.
# The Spider-only trim of TOPN/SORT/TIME was REVERTED: it lifted the benchmark but broke live composite view
# stacks (test_geo). This mirror tracks the live gate, so the Spider number here reflects real behavior.
DEPTH_PRIMS = frozenset({"EXCL", "RATIO", "TOPN", "SHARE", "TIME", "HAVING", "SORT", "DIVIDE", "RUNNING", "GROUP"})


def _write_json_atomic(path, value):
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
    for attempt in range(10):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))


def ast_predict(
    enc, tabs, question, schema_fks=None, schema_cache=None,
    selection="serving_top1", max_candidates=25, use_signals=True,
):
    """Run AST search using either exact serving top-1 or bounded execution checks.

    use_signals gates the encoder semantic signals (ablation only; serving is always True)."""
    cache_key = tuple(id(table) for table in tabs)
    cached = schema_cache.get(cache_key) if schema_cache is not None else None
    if cached is None:
        norm, discovered_fks = enc.ingest(tabs)
        fks = schema_fks if schema_fks is not None else discovered_fks
        sch, _, tmap = enc.schema(norm, fks)
        cached = (norm, fks, sch, tmap)
        if schema_cache is not None:
            schema_cache[cache_key] = cached
    norm, fks, sch, tmap = cached
    candidates = enc.search_ast(question, sch, norm, fks, max_candidates=max_candidates,
                                use_semantic_signals=use_signals)
    from engine.sql_rank import execute_and_rerank
    from engine.sql_schema import SchemaGraph

    def execute(sql):
        ok, why = enc.guard(sql)
        if not ok:
            raise RuntimeError(f"guard: {why}")
        return enc.execute(tmap, sch, sql)

    if not candidates:
        return {"ok": False, "error": "no connected AST candidate",
                "stage": "ast_search", "path": "ast"}
    if selection == "serving_top1":
        candidate = candidates[0]
        try:
            _, rows = execute(candidate.sql)
        except Exception as exc:                  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "stage": "ast_search", "path": "ast"}
        return {
            "ok": True,
            "sql": candidate.sql,
            "rows": [list(row) for row in rows],
            "path": "ast",
            "plan": list(candidate.evidence),
            "candidate_count": len(candidates),
            "executed_candidate_count": 1,
            "selected_candidate_rank": 0,
            "candidate_score": round(candidate.score, 4),
            "rank_features": dict(candidate.features),
        }

    graph = SchemaGraph.from_planner(sch, fks)
    executions = execute_and_rerank(
        question, candidates, graph, execute, max_candidates=5, preserve_top=True
    )
    errors = [execution.error for execution in executions if execution.error]
    for selected_rank, execution in enumerate(executions):
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
                "selected_candidate_rank": selected_rank,
                "rank_features": dict(candidate.features)}
    detail = errors[0] if errors else "no connected AST candidate"
    return {"ok": False, "error": detail, "stage": "ast_search", "path": "ast"}


def compose_predict(eng, tabs, question):
    res = eng.run(tabs, question, world=None)
    ans = res.get("answer")
    return {"ok": True, "sql": (res["views"][-1]["sql"] if res.get("views") else None),
            "rows": ans["rows"] if ans else None, "path": "compose",
            "plan": res.get("plan"), "primitives": res.get("primitives")}


# the DEPTH composition views — mirrors engine.knowledge_compose.ComposedKnowledgeQuery, SPLIT by whether the
# delegate AST planner can also express them. ENGINE_ONLY (yoy/running/share/divide/having) the planner cannot do
# -> always stand on compose. SLOT_OVERLAP (topn/sort/time_filter) the planner ALSO does WITH projection+WHERE ->
# stand on compose only when a WORLD join is in the plan (world_join/world_filter). On Spider world=None, so a
# world join NEVER appears -> SLOT_OVERLAP always falls to the AST planner (recovering the projection/sort losses),
# while the live world composites (world join present) still stand. Keep in sync with knowledge_compose.py.
_ENGINE_ONLY_VIEWS = {"yoy", "running", "share", "divide", "having"}
_SLOT_OVERLAP_VIEWS = {"topn", "sort", "time_filter"}


def predict(enc, eng, reader, tabs, question, schema_fks=None,
            ast_schema_cache=None, selection="serving_top1", max_candidates=25,
            use_signals=True, use_compose=True):
    """Route like the live system (gate -> compose -> stand-or-fall-back -> delegate/AST planner). Any
    unrecovered exception is caught and attributed to a stage.

    use_compose / use_signals gate the compose routing and encoder signals respectively (ablation only;
    serving is always True/True). use_compose=False isolates the pure typed-AST planner."""
    if use_compose:
        try:
            depth = bool(reader.present(question) & DEPTH_PRIMS)
        except Exception:                        # noqa: BLE001
            depth = False
        if depth:
            try:
                r = compose_predict(eng, tabs, question)
                plan = r.get("plan") or []
                world = any(op in ("world_join", "world_filter") for op in plan)   # never true on Spider (world=None)
                if (any(op in _ENGINE_ONLY_VIEWS for op in plan)
                        or (world and any(op in _SLOT_OVERLAP_VIEWS for op in plan))):
                    return r                      # genuine composition (or a world composite) — stand on it
            except Exception:                     # noqa: BLE001 — live serve() delegates on engine error
                pass
    try:
        return ast_predict(
            enc, tabs, question, schema_fks, ast_schema_cache,
            selection, max_candidates, use_signals,
        )
    except Exception as e:                        # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        stage = ("join_build" if ("ambiguous" in str(e) or ("join" in str(e).lower()))
                 else "assemble_exec")
        return {"ok": False, "error": msg, "stage": stage, "path": "ast"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--dbs", required=True)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "results"))
    ap.add_argument("--per-diff", type=int, default=0)
    ap.add_argument("--config", default="gold_tables", choices=["gold_tables", "whole_db"])
    ap.add_argument("--selection", choices=["serving_top1", "execution_checks"],
                    default="serving_top1",
                    help="serving_top1 matches live AST selection exactly")
    ap.add_argument("--max-candidates", type=int, default=25,
                    help="AST candidate pool returned to selection/ranking")
    # --- ablation knobs (NOT serving; serving is always compose+signals). Attribute where accuracy comes from. ---
    ap.add_argument("--no-compose", action="store_true",
                    help="ablation: isolate the pure typed-AST planner (skip DEPTH compose routing)")
    ap.add_argument("--no-signals", action="store_true",
                    help="ablation: run the AST search WITHOUT encoder semantic signals")
    ap.add_argument("--cap", type=int, default=5000, help="row cap per table (bounds exec; only wta_1 is capped)")
    ap.add_argument("--timeout", type=float, default=12.0,
                    help="soft prediction latency budget; evaluation is never abandoned")
    ap.add_argument("--tag", default="")
    ap.add_argument("--checkpoint-every", type=int, default=25)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--retry-timeouts", action="store_true")
    ap.add_argument("--max-new", type=int, default=0,
                    help="checkpoint and exit cleanly after this many new predictions")
    args = ap.parse_args()
    if args.checkpoint_every < 0 or args.max_new < 0 or args.max_candidates < 1:
        ap.error("checkpoint cadence and max-new must be nonnegative")

    dev = json.load(open(os.path.join(args.data, "dev.json"), encoding="utf-8"))
    tables_meta = {t["db_id"]: t for t in json.load(open(os.path.join(args.data, "tables.json"), encoding="utf-8"))}
    ast_fks = {db_id: spider_foreign_keys(meta) for db_id, meta in tables_meta.items()}

    buckets = collections.defaultdict(list)
    for i, ex in enumerate(dev):
        buckets[eval_hardness(ex["sql"])].append(i)
    picked = []
    for d in DIFFS:
        picked += (buckets[d][:args.per_diff] if args.per_diff else buckets[d])
    picked.sort()

    os.makedirs(args.out, exist_ok=True)
    suffix = ("_" + args.tag) if args.tag else ""
    checkpoint_path = os.path.join(
        args.out, f"full_eval_per_example{suffix}.checkpoint.json"
    )
    checkpoint_contract = {
        "picked": picked,
        "config": args.config,
        "selection": args.selection,
        "max_candidates": args.max_candidates,
        "compose": not args.no_compose,
        "signals": not args.no_signals,
        "cap": args.cap,
        "timeout": args.timeout,
    }
    from engine.artifact_provenance import fingerprint_paths, sha256_tree
    from engine.config import DATA_DIR

    # Fingerprint the FULL serving path, not just the planner core — a routing or semantic-signal change
    # (tables.py / knowledge_compose.py / primitive_head.py / compose.py / encoder_overlay.py) or an edit to
    # this harness must invalidate a --resume checkpoint, or stale predictions get silently reused.
    engine_code = ("tables.py", "sql_search.py", "sql_rank.py", "sql_ast.py", "sql_candidate.py",
                   "sql_schema.py", "sql_expansion.py", "sql_constraints.py", "sql_extrema.py",
                   "sql_recursive.py", "sql_profile.py", "sql_profile_expansion.py",
                   "knowledge_compose.py", "primitive_head.py", "compose.py", "encoder_overlay.py")
    checkpoint_contract["artifacts"] = {
        **fingerprint_paths({
            "dev": os.path.join(args.data, "dev.json"),
            "tables": os.path.join(args.data, "tables.json"),
            "encoder": DATA_DIR / "encoder.pt",
            "encoder_meta": DATA_DIR / "encoder_meta.pt",
            "eval_harness": os.path.join(ROOT, "spider", "probe", "full_eval.py"),
            **{f"engine/{name}": os.path.join(ROOT, "engine", name) for name in engine_code},
        }),
        "encoder_adapter": sha256_tree(DATA_DIR / "qwen_lora"),
    }
    completed = {}
    if args.resume and os.path.exists(checkpoint_path):
        with open(checkpoint_path, encoding="utf-8") as handle:
            checkpoint = json.load(handle)
        if checkpoint.get("contract") != checkpoint_contract:
            ap.error("checkpoint does not match this evaluation contract")
        completed = {
            int(record["idx"]): record
            for record in checkpoint["records"]
            if not (args.retry_timeouts and record.get("stage") == "timeout")
        }
        print(f"resuming from {len(completed)} checkpointed examples", flush=True)

    print("loading encoder (Qwen LoRA + relational readout, CPU)...", flush=True)
    from engine.encoder_overlay import EncoderQuery
    from engine.primitive_head import PrimitiveReader
    from engine.compose import ComposeEngine
    enc = EncoderQuery()
    reader = PrimitiveReader(encoder=enc)
    eng = ComposeEngine(reader=reader)
    print(f"loaded. evaluating {len(picked)} examples (config={args.config})\n", flush=True)

    db_cache = {}
    ast_schema_cache = {}
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
    new_predictions = 0

    def checkpoint_records():
        merged = dict(completed)
        merged.update((int(record["idx"]), record) for record in per_example)
        return [merged[index] for index in sorted(merged)]

    for n, i in enumerate(picked):
        ex = dev[i]; db_id = ex["db_id"]; diff = eval_hardness(ex["sql"])
        capped, gcon = get_db(db_id)
        gold_rows, gerr = exec_sql_timed(gcon, ex["query"], timeout=8.0)
        if args.config == "gold_tables":
            names = [t.lower() for t in recursive_gold_table_names(ex, tables_meta)]
            tabs = [capped[t] for t in names if t in capped] or list(capped.values())
        else:
            tabs = list(capped.values())

        saved = completed.get(i)
        if saved is not None:
            r = saved
        else:
            new_predictions += 1
            r, terr, prediction_seconds, over_budget = run_with_budget(
                lambda: predict(
                    enc, eng, reader, tabs, ex["question"],
                    ast_fks.get(db_id), ast_schema_cache,
                    args.selection, args.max_candidates,
                    not args.no_signals, not args.no_compose,
                ),
                args.timeout,
            )
            if terr is not None:
                r = {"ok": False, "error": str(terr), "stage": "prediction_error",
                     "path": "ast"}
            r["prediction_seconds"] = round(prediction_seconds, 6)
            r["over_budget"] = over_budget
        cmp = compare(gold_rows, r.get("rows")) if r["ok"] else {}
        path_hist[r["path"]] += 1
        st = stat[diff]; st["n"] += 1
        if gerr:
            st["gold_exec_error"] += 1
        record_integrated_result(st, gold_rows, cmp, bool(r["ok"]))
        st["over_budget"] += bool(r.get("over_budget"))
        if not r["ok"]:
            stage_hist[r["stage"]] += 1
        else:
            if cmp.get("lenient"):
                path_correct[r["path"]] += 1
        record = (
            saved if saved is not None
            else {"idx": i, "db_id": db_id, "difficulty": diff, "question": ex["question"],
                  "gold": ex["query"], "gold_exec_error": gerr, **r, **cmp}
        )
        per_example.append(record)
        if args.checkpoint_every and (n + 1) % args.checkpoint_every == 0:
            _write_json_atomic(
                checkpoint_path,
                {"contract": checkpoint_contract, "records": checkpoint_records()},
            )
        if (n + 1) % 50 == 0:
            print(f"  {n+1}/{len(picked)}", flush=True)
        if args.max_new and new_predictions >= args.max_new:
            _write_json_atomic(
                checkpoint_path,
                {"contract": checkpoint_contract, "records": checkpoint_records()},
            )
            print(
                f"checkpointed {len(checkpoint_records())}/{len(picked)} examples; "
                f"clean segment exit after {new_predictions} new predictions",
                flush=True,
            )
            return

    tot = collections.Counter()
    for d in DIFFS:
        tot.update(stat[d])
    N = max(tot["n"], 1)
    summary = {
        "n": tot["n"], "config": args.config,
        "selection": args.selection,
        "compose": not args.no_compose,
        "signals": not args.no_signals,
        "max_candidates": args.max_candidates,
        "artifacts": checkpoint_contract["artifacts"],
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
    _write_json_atomic(
        checkpoint_path,
        {"contract": checkpoint_contract, "records": checkpoint_records()},
    )
    suf = suffix
    with open(os.path.join(args.out, f"full_eval{suf}.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
        handle.write("\n")
    with open(
        os.path.join(args.out, f"full_eval_per_example{suf}.json"), "w", encoding="utf-8"
    ) as handle:
        json.dump(per_example, handle, indent=2)
        handle.write("\n")

    P = print
    P("\n" + "=" * 78); P("PROBE D+ — FULL OFFLINE SYSTEM (typed-AST planner + compose, live routing)"); P("=" * 78)
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
