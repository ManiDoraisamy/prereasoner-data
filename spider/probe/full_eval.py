"""Probe D+ (headline): reproduce Prereasoner's Spider path fully offline.

The live stack routes each question (ComposedKnowledgeQuery._composed):
  * a question whose LEARNED primitive head fires a DEPTH primitive
    (EXCL/RATIO/TOPN/SHARE/TIME/HAVING/SORT/DIVIDE/RUNNING) -> the ComposeEngine view stack;
  * everything else -> the delegate, whose SQL for a self-contained (no-world) table is the deterministic
    typed-AST planner (tables.py search_ast -> engine.sql_search): a beam over a typed SQL AST that emits
    projection, value-matched WHERE, >/<, dates, GROUP BY, ORDER BY, LIMIT, argmax, FK JOINs,
    COUNT/SUM/AVG/MIN/MAX, plus recursive subqueries, aggregate constraints, disjunctions, and set/extrema
    shapes, then ranks the pool with sql_rank.

The default `--backend sql` preserves the established selected-SQL evaluation. `python` lowers the same
selected typed AST to the shared deterministic plan and executes its generated ORM/Python program on an
independent in-memory SQLite database. `auto` mirrors the bounded production preference with SQL fallback;
`verify` additionally requires strict selected-SQL/Python denotation equality. These modes do not alter the
model, candidate pool, ranking, gold query, input cap, or the existing `spider_eval.compare` correctness
contract. World resolution is irrelevant to self-contained Spider and the live clarify gate remains omitted;
a refusal would still score wrong, so that omission can only make the benchmark an upper bound.

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

try:
    from .evalutil import build_mem_db, exec_sql_timed, load_capped, run_with_budget
    from .hardness import eval_hardness
    from .spider_eval import (
        compare,
        is_scalar,
        record_integrated_result,
        recursive_gold_table_names,
        spider_foreign_keys,
    )
except ImportError:  # direct `python full_eval.py` from spider/probe remains supported
    from evalutil import build_mem_db, exec_sql_timed, load_capped, run_with_budget
    from hardness import eval_hardness
    from spider_eval import (
        compare,
        is_scalar,
        record_integrated_result,
        recursive_gold_table_names,
        spider_foreign_keys,
    )

DIFFS = ["easy", "medium", "hard", "extra"]
# Routing is NOT mirrored here — it is IMPORTED from the ONE shared router (engine.routing), the same module
# live serving uses, so the eval can never drift from production. DEPTH_PRIMS is the primitive-head EVIDENCE to
# build a compose plan; compose_owns is the AUTHORITY (a grounded world dependency). Spider tables are world-less,
# so compose_owns is always False here -> every question routes to the typed-AST planner.
from engine.artifact_provenance import json_artifact_bytes
from engine.routing import DEPTH_PRIMS, compose_owns, required_ops


class _DeterministicCandidateError(RuntimeError):
    def __init__(self, message, metadata):
        super().__init__(message)
        self.metadata = metadata


def _lower_python_candidate(candidate, tables, schema, foreign_keys):
    """Lower one selected AST and return its plan plus serving row estimate."""
    from engine.deterministic import lower_select_query
    from engine.deterministic.lower import UnsupportedDeterministicPlan
    from engine.sql_ast import SelectQuery

    if not isinstance(candidate.query, SelectQuery):
        raise UnsupportedDeterministicPlan(
            "deterministic Python requires a SELECT query"
        )
    plan = lower_select_query(
        "spider_query",
        candidate.query,
        schema,
        foreign_keys,
        postgres_row_identity=False,
    )
    estimated_rows = sum(
        len(table.get("rows") or ())
        for table in tables
        if table.get("name") in plan.views[0].tables
    )
    return plan, estimated_rows


def _execute_python_candidate(plan, tables, estimated_rows, row_limit):
    """Execute one lowered generated ORM/Python program on Spider's SQLite rows.

    Spider's independent gold SQL still runs through the existing evaluator. This helper changes
    only the selected candidate's execution backend, so SQL and Python runs share the model,
    planner, candidate ranking, capped input rows, and scalar-gold comparison contract.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from engine.deterministic import DeterministicAnalysis

    raw = build_mem_db(tables)
    engine = create_engine(
        "sqlite+pysqlite://",
        creator=lambda: raw,
        poolclass=StaticPool,
    )
    try:
        with engine.connect() as connection:
            result = DeterministicAnalysis(
                plan, conversation_schema="main"
            ).run(
                connection,
                mode="python",
                estimated_rows=estimated_rows,
                python_row_limit=row_limit,
            )
        return [list(row.values()) for row in result.rows]
    finally:
        engine.dispose()


def _git_provenance(root):
    """Record the source commit + whether the worktree is dirty, so every result traces to an exact tree
    (CLAUDE.md requires both). Best-effort: returns None/False if git is unavailable."""
    import subprocess

    def _git(*args):
        try:
            return subprocess.run(
                ["git", "-C", root, *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
        except Exception:                                    # noqa: BLE001
            return ""
    return {"source_commit": (_git("rev-parse", "HEAD") or None),
            "worktree_dirty": bool(_git("status", "--porcelain"))}


def _write_json_atomic(path, value):
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(json_artifact_bytes(value, indent=2))
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
    execution_backend="sql", python_row_limit=10_000,
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

    def execute_selected(candidate, sql_rows=None):
        metadata = {
            "execution_backend_requested": execution_backend,
            "execution_backend_actual": "sql",
            "python_lowerable": None,
            "python_sql_equal": None,
        }
        if execution_backend == "sql":
            if sql_rows is None:
                _, sql_rows = execute(candidate.sql)
            return sql_rows, metadata

        from engine.deterministic.lower import UnsupportedDeterministicPlan
        from engine.deterministic.operators import PythonRowLimitExceeded

        try:
            plan, estimated_rows = _lower_python_candidate(
                candidate, norm, sch, fks
            )
        except UnsupportedDeterministicPlan as exc:
            metadata.update(
                python_lowerable=False,
                python_fallback_reason=str(exc),
            )
            if execution_backend != "auto":
                raise _DeterministicCandidateError(str(exc), metadata) from exc
            if sql_rows is None:
                _, sql_rows = execute(candidate.sql)
            return sql_rows, metadata

        metadata.update(
            python_lowerable=True,
            python_estimated_rows=estimated_rows,
        )
        if execution_backend == "auto" and estimated_rows > python_row_limit:
            metadata["python_fallback_reason"] = (
                f"estimated rows {estimated_rows} exceed limit {python_row_limit}"
            )
            if sql_rows is None:
                _, sql_rows = execute(candidate.sql)
            return sql_rows, metadata

        try:
            python_rows = _execute_python_candidate(
                plan, norm, estimated_rows, python_row_limit
            )
        except PythonRowLimitExceeded as exc:
            metadata["python_fallback_reason"] = str(exc)
            if execution_backend != "auto":
                raise _DeterministicCandidateError(str(exc), metadata) from exc
            if sql_rows is None:
                _, sql_rows = execute(candidate.sql)
            return sql_rows, metadata
        except Exception as exc:
            metadata["python_execution_error"] = f"{type(exc).__name__}: {exc}"
            if execution_backend == "auto":
                metadata["python_fallback_reason"] = metadata[
                    "python_execution_error"
                ]
                if sql_rows is None:
                    _, sql_rows = execute(candidate.sql)
                return sql_rows, metadata
            raise _DeterministicCandidateError(
                metadata["python_execution_error"], metadata
            ) from exc

        metadata["execution_backend_actual"] = "python"
        if execution_backend in {"auto", "verify"}:
            if sql_rows is None:
                _, sql_rows = execute(candidate.sql)
            metadata["python_sql_equal"] = bool(
                compare(sql_rows, python_rows).get("strict")
            )
            if execution_backend == "verify" and not metadata["python_sql_equal"]:
                raise _DeterministicCandidateError(
                    "Python and selected SQL candidate disagree", metadata
                )
            # Observation must not change the graded answer. Serving AUTO does
            # not execute the original candidate to override successful Python;
            # doing that only here hides both tie-policy changes and real bugs.
        return python_rows, metadata

    if not candidates:
        return {"ok": False, "error": "no connected AST candidate",
                "stage": "ast_search", "path": "ast"}
    if selection == "serving_top1":
        candidate = candidates[0]
        try:
            rows, backend = execute_selected(candidate)
        except _DeterministicCandidateError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "stage": "deterministic_execution", "path": "ast",
                    **exc.metadata}
        except Exception as exc:                  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "stage": ("ast_search" if execution_backend == "sql"
                              else "deterministic_execution"), "path": "ast",
                    "execution_backend_requested": execution_backend}
        return {
            "ok": True,
            "sql": candidate.sql,
            "rows": [list(row) for row in rows],
            "path": "ast",
            **backend,
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
        try:
            selected_rows, backend = execute_selected(candidate, rows)
        except _DeterministicCandidateError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "stage": "deterministic_execution", "path": "ast",
                    **exc.metadata}
        except Exception as exc:                  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "stage": "deterministic_execution", "path": "ast",
                    "execution_backend_requested": execution_backend}
        return {"ok": True, "sql": candidate.sql, "rows": [list(row) for row in selected_rows], "path": "ast",
                **backend,
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
            "plan": res.get("plan"), "primitives": res.get("primitives"),
            "world_dependency": res.get("world_dependency")}   # None on Spider (world=None) -> router picks AST


def predict(enc, eng, reader, tabs, question, schema_fks=None,
            ast_schema_cache=None, selection="serving_top1", max_candidates=25,
            use_signals=True, use_compose=True, execution_backend="sql",
            python_row_limit=10_000):
    """Route exactly like live serving, via the SHARED router (engine.routing): primitive-head depth cues are
    EVIDENCE to build a compose plan; the AUTHORITY to stand on it is a grounded world dependency (compose_owns).
    Spider tables are world-less, so compose_owns is always False and every question routes to the typed-AST
    planner. Any unrecovered exception is caught and attributed to a stage.

    use_compose / use_signals gate compose routing and encoder signals (ablation only; serving is always
    True/True). use_compose=False isolates the pure typed-AST planner."""
    if use_compose:
        try:
            depth = bool(reader.present(question) & DEPTH_PRIMS)
        except Exception:                        # noqa: BLE001
            depth = False
        if depth:
            try:
                r = compose_predict(eng, tabs, question)
                # AUTHORITY: a NECESSARY grounded world dependency (world_dependency is None on Spider -> AST).
                if compose_owns(r.get("plan"), r.get("world_dependency"), r.get("rows"),
                                required_ops(question)):
                    return r
            except Exception:  # noqa: BLE001, S110 — live serve() delegates on engine error
                pass
    try:
        return ast_predict(
            enc, tabs, question, schema_fks, ast_schema_cache,
            selection, max_candidates, use_signals,
            execution_backend, python_row_limit,
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
    ap.add_argument(
        "--backend",
        choices=["sql", "python", "auto", "verify"],
        default="sql",
        help=(
            "execute the selected AST with legacy SQL, generated Python, "
            "Python-preferred auto fallback, or Python/SQL denotation verification"
        ),
    )
    ap.add_argument(
        "--python-row-limit",
        type=int,
        default=10_000,
        help="maximum estimated and materialized rows for Python execution",
    )
    ap.add_argument(
        "--scalar-only",
        action="store_true",
        help="evaluate only examples whose independently executed gold result is scalar",
    )
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
    if (args.checkpoint_every < 0 or args.max_new < 0
            or args.max_candidates < 1 or args.python_row_limit < 0):
        ap.error("checkpoint cadence, max-new, and row limits must be nonnegative")

    with open(os.path.join(args.data, "dev.json"), encoding="utf-8") as handle:
        dev = json.load(handle)
    with open(os.path.join(args.data, "tables.json"), encoding="utf-8") as handle:
        tables_meta = {table["db_id"]: table for table in json.load(handle)}
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
        "backend": args.backend,
        "python_row_limit": args.python_row_limit,
        "scalar_only": args.scalar_only,
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
    engine_code = ("routing.py", "tables.py", "sql_search.py", "sql_rank.py", "sql_ast.py", "sql_candidate.py",
                   "sql_schema.py", "sql_expansion.py", "sql_constraints.py", "sql_extrema.py",
                   "sql_recursive.py", "sql_profile.py", "sql_profile_expansion.py",
                   "knowledge_compose.py", "primitive_head.py", "compose.py", "encoder_overlay.py",
                   "calculations/core.py", "calculations/registry.py",
                   "calculations/search.py", "calculations/specifications.py")
    deterministic_code = (
        "deterministic/context.py",
        "deterministic/lower.py",
        "deterministic/operators.py",
        "deterministic/plan.py",
        "deterministic/runtime.py",
        "deterministic/service.py",
        "deterministic/emitter/py/__init__.py",
        "deterministic/emitter/sql/__init__.py",
    )
    checkpoint_contract["artifacts"] = {
        **fingerprint_paths({
            "dev": os.path.join(args.data, "dev.json"),
            "tables": os.path.join(args.data, "tables.json"),
            "encoder": DATA_DIR / "encoder.pt",
            "encoder_meta": DATA_DIR / "encoder_meta.pt",
            "eval_harness": os.path.join(ROOT, "spider", "probe", "full_eval.py"),
            **{f"engine/{name}": os.path.join(ROOT, "engine", name) for name in engine_code},
            **{
                f"engine/{name}": os.path.join(ROOT, "engine", *name.split("/"))
                for name in deterministic_code
            },
        }),
        "encoder_adapter": sha256_tree(DATA_DIR / "qwen_lora"),
        **_git_provenance(ROOT),   # source_commit + worktree_dirty: a run traces to an exact tree; a dirty
    }                              # tree (or a different commit) invalidates a --resume checkpoint.
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
    from engine.compose import ComposeEngine
    from engine.encoder_overlay import EncoderQuery
    from engine.primitive_head import PrimitiveReader
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
        if args.scalar_only and not is_scalar(gold_rows):
            continue
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
            selected_fks = ast_fks.get(db_id)

            def predict_current(
                current_tabs=tabs,
                current_question=ex["question"],
                current_fks=selected_fks,
            ):
                return predict(
                    enc, eng, reader, current_tabs, current_question,
                    current_fks, ast_schema_cache,
                    args.selection, args.max_candidates,
                    not args.no_signals, not args.no_compose,
                    args.backend, args.python_row_limit,
                )

            r, terr, prediction_seconds, over_budget = run_with_budget(
                predict_current,
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
        if args.backend != "sql":
            st["python_candidate_total"] += 1
            st["python_lowerable"] += r.get("python_lowerable") is True
            st["python_executed"] += r.get("execution_backend_actual") == "python"
            st["python_sql_compared"] += r.get("python_sql_equal") is not None
            st["python_sql_verified"] += r.get("python_sql_equal") is True
            if is_scalar(gold_rows):
                st["python_scalar_total"] += 1
                if r.get("python_lowerable") is True:
                    st["python_scalar_lowerable"] += 1
                if r.get("execution_backend_actual") == "python":
                    st["python_scalar_executed"] += 1
                    if cmp.get("scalar_exact"):
                        st["python_scalar_correct"] += 1
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
        "backend": args.backend,
        "python_row_limit": args.python_row_limit,
        "scalar_only": args.scalar_only,
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
    if args.backend != "sql":
        summary["python"] = {
            "candidate_n": tot["python_candidate_total"],
            "lowerable_n": tot["python_lowerable"],
            "lowering_coverage_pct": round(
                100 * tot["python_lowerable"]
                / max(tot["python_candidate_total"], 1),
                1,
            ),
            "executed_n": tot["python_executed"],
            "sql_compared_n": tot["python_sql_compared"],
            "sql_verified_n": tot["python_sql_verified"],
            "sql_mismatch_n": (
                tot["python_sql_compared"] - tot["python_sql_verified"]
            ),
            "scalar_gold_n": tot["python_scalar_total"],
            "scalar_lowerable_n": tot["python_scalar_lowerable"],
            "scalar_executed_n": tot["python_scalar_executed"],
            "scalar_correct_n": tot["python_scalar_correct"],
            "scalar_accuracy_on_executed_pct": round(
                100 * tot["python_scalar_correct"]
                / max(tot["python_scalar_executed"], 1),
                1,
            ),
            "scalar_accuracy_on_all_gold_pct": round(
                100 * tot["python_scalar_correct"]
                / max(tot["python_scalar_total"], 1),
                1,
            ),
        }
    _write_json_atomic(
        checkpoint_path,
        {"contract": checkpoint_contract, "records": checkpoint_records()},
    )
    suf = suffix
    _write_json_atomic(
        os.path.join(args.out, f"full_eval{suf}.json"), summary
    )
    _write_json_atomic(
        os.path.join(args.out, f"full_eval_per_example{suf}.json"), per_example
    )

    P = print
    P("\n" + "=" * 78); P("PROBE D+ — FULL OFFLINE SYSTEM (typed-AST planner + compose, live routing)"); P("=" * 78)
    P(f"config={args.config}   backend={args.backend}   examples={tot['n']}")
    P(f"  routed: {dict(path_hist)}   (correct-lenient by path: {dict(path_correct)})")
    P(f"  answered : {tot['answered']:4d} ({summary['answered_pct']}%)   error {tot['error']} ({summary['error_pct']}%)  stages={dict(stage_hist)}")
    P(f"  CORRECT lenient (generous UB): {tot['correct_lenient']:4d} ({summary['correct_lenient_pct']}%)")
    P(f"  CORRECT strict  (harsh LB)   : {tot['correct_strict']:4d} ({summary['correct_strict_pct']}%)")
    P(f"  SCALAR-gold accuracy (clean) : {tot['scalar_correct']}/{tot['scalar_total']} ({summary['scalar_gold_accuracy_pct']}%)")
    if args.backend != "sql":
        py = summary["python"]
        P(
            "  PYTHON lowering/scalar gold   : "
            f"coverage={py['lowerable_n']}/{py['candidate_n']} "
            f"({py['lowering_coverage_pct']}%)  "
            f"scalar={py['scalar_correct_n']}/{py['scalar_executed_n']} "
            f"({py['scalar_accuracy_on_executed_pct']}% executed; "
            f"{py['scalar_accuracy_on_all_gold_pct']}% all gold)"
        )
    P("  by difficulty:")
    for d in DIFFS:
        s = stat[d]
        P(f"     {d:8s} n={s['n']:4d}  answered={s['answered']:4d}  lenient={s['correct_lenient']:4d}  "
          f"strict={s['correct_strict']:4d}  scalar={s['scalar_correct']}/{s['scalar_total']}")
    P(f"\nwrote results/full_eval{suf}.json (+ per_example)")


if __name__ == "__main__":
    main()
