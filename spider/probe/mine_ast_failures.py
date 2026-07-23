"""Mine Spider candidate-recall failures for the deterministic AST planner.

This probe executes every Phase 5 candidate, then compares a structural profile
of the returned pool with the parsed Spider gold tree.  Gold SQL is used only by
the offline evaluator; no profile enters planner inference or ranking.

Run from the repository root:

  python spider/probe/mine_ast_failures.py --dbs spider/data/dbs
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import time
from typing import Any, Mapping, Sequence

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from .ast_profile import CandidateAssessment, diagnose_pool, profile_query, profile_spider_sql
    from .evalutil import build_mem_db, exec_sql_timed, load_capped
    from .hardness import eval_hardness
    from .spider_eval import compare, recursive_gold_table_names, spider_foreign_keys
except ImportError:  # direct script execution
    from ast_profile import CandidateAssessment, diagnose_pool, profile_query, profile_spider_sql
    from evalutil import build_mem_db, exec_sql_timed, load_capped
    from hardness import eval_hardness
    from spider_eval import compare, recursive_gold_table_names, spider_foreign_keys

from engine.sql_search import SQLSearcher
from engine.sql_profile_expansion import ProfileSearchConfig
from engine.sql_proposal_runtime import ProposalSignalProvider


def _pct(value: int, denominator: int) -> float:
    return round(100.0 * value / max(denominator, 1), 1)


def audit_ranker_cache(path: str) -> dict[str, Any]:
    """Summarize which Spider train groups can actually supervise the ranker."""
    with open(path, encoding="utf-8") as handle:
        header = json.loads(next(handle))
        groups = [json.loads(line) for line in handle if line.strip()]

    candidate_rows = 0
    positive_rows = 0
    oracle_groups = 0
    mixed_groups = 0
    mixed_rows = 0
    empty_groups = 0
    databases = set()
    for group in groups:
        databases.add(str(group["db_id"]))
        candidates = group.get("candidates", ())
        candidate_rows += len(candidates)
        positives = sum(bool(candidate.get("correct")) for candidate in candidates)
        positive_rows += positives
        oracle_groups += int(positives > 0)
        empty_groups += int(not candidates)
        if positives and positives < len(candidates):
            mixed_groups += 1
            mixed_rows += len(candidates)
    total = len(groups)
    return {
        "path": os.path.relpath(path, ROOT).replace("\\", "/"),
        "header": header,
        "examples": total,
        "databases": len(databases),
        "candidate_rows": candidate_rows,
        "average_candidates": round(candidate_rows / max(total, 1), 2),
        "positive_rows": positive_rows,
        "oracle_positive_examples": oracle_groups,
        "oracle_positive_pct": _pct(oracle_groups, total),
        "no_positive_examples": total - oracle_groups,
        "no_positive_pct": _pct(total - oracle_groups, total),
        "mixed_examples_usable_by_ranking_loss": mixed_groups,
        "mixed_candidate_rows": mixed_rows,
        "empty_examples": empty_groups,
    }


def summarize(
    details: Sequence[Mapping[str, Any]],
    elapsed: float,
    config: str,
    pool: int,
    top_k: int,
    training_audit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    total = len(details)
    status = collections.Counter(record["diagnosis"]["status"] for record in details)
    candidate_counts = [int(record["diagnosis"]["candidate_count"]) for record in details]
    missing_features: collections.Counter[str] = collections.Counter()
    missing_roles: collections.Counter[str] = collections.Counter()
    gold_no_lenient_features: collections.Counter[str] = collections.Counter()
    nearest_missing: collections.Counter[str] = collections.Counter()
    nearest_extra: collections.Counter[str] = collections.Counter()
    nearest_delta_clusters: collections.Counter[str] = collections.Counter()
    by_difficulty: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    samples: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)

    for record in details:
        diagnosis = record["diagnosis"]
        label = diagnosis["bottleneck"] or diagnosis["status"]
        by_difficulty[str(record["difficulty"])][label] += 1
        if diagnosis["status"] == "no_match":
            for feature, count in record["gold_profile"]["sketch"].items():
                gold_no_lenient_features[feature] += int(count)
            for feature, count in diagnosis["missing_sketch_features"].items():
                missing_features[feature] += int(count)
            for role, columns in diagnosis["missing_role_columns"].items():
                missing_roles[role] += len(columns)
            nearest = diagnosis.get("nearest") or {}
            missing = nearest.get("missing_sketch_features", {})
            extra = nearest.get("extra_sketch_features", {})
            nearest_missing.update(missing)
            nearest_extra.update(extra)
            cluster = json.dumps({"missing": missing, "extra": extra}, sort_keys=True)
            nearest_delta_clusters[cluster] += 1
        if len(samples[label]) < 5:
            samples[label].append({
                "index": record["index"],
                "db_id": record["db_id"],
                "question": record["question"],
                "gold": record["gold"],
                "top_sql": record.get("top_sql"),
            })

    strict_any = sum(bool(record["diagnosis"]["strict_ranks"]) for record in details)
    lenient_any = sum(bool(record["diagnosis"]["lenient_ranks"]) for record in details)
    both = sum(
        bool(record["diagnosis"]["strict_ranks"])
        and bool(record["diagnosis"]["lenient_ranks"])
        for record in details
    )
    strict_failure_bottlenecks: collections.Counter[str] = collections.Counter()
    for record in details:
        diagnosis = record["diagnosis"]
        if diagnosis["strict_ranks"]:
            continue
        label = "lenient_only" if diagnosis["lenient_ranks"] else diagnosis["bottleneck"]
        strict_failure_bottlenecks[label or "unclassified"] += 1
    strict_failure_total = total - strict_any
    neither_total = total - (strict_any + lenient_any - both)
    coverage_fields = (
        "pool_sketch_covered",
        "pool_table_covered",
        "pool_role_columns_covered",
        "candidate_schema_covered",
        "combined_profile_covered",
    )
    strict_failure_records = [
        record for record in details if not record["diagnosis"]["strict_ranks"]
    ]
    profile_coverage = {
        field: {
            "n": sum(bool(record["diagnosis"][field]) for record in strict_failure_records),
            "pct_of_strict_failures": _pct(
                sum(bool(record["diagnosis"][field]) for record in strict_failure_records),
                strict_failure_total,
            ),
        }
        for field in coverage_fields
    }
    delta_clusters = []
    for encoded, count in nearest_delta_clusters.most_common(20):
        delta_clusters.append({"n": count, **json.loads(encoded)})
    return {
        "config": config,
        "pool": pool,
        "top_k": top_k,
        "n": total,
        "elapsed_seconds": round(elapsed, 2),
        "candidate_pool": {
            "total_candidates": sum(candidate_counts),
            "average_candidates": round(statistics.mean(candidate_counts), 2) if candidate_counts else 0.0,
            "median_candidates": statistics.median(candidate_counts) if candidate_counts else 0,
            "maximum_candidates": max(candidate_counts, default=0),
        },
        "outcomes": {
            name: {"n": count, "pct": _pct(count, total)}
            for name, count in status.items()
        },
        "metric_overlap": {
            "strict_any": strict_any,
            "lenient_any": lenient_any,
            "both": both,
            "strict_only": strict_any - both,
            "lenient_only": lenient_any - both,
            "neither": neither_total,
            "note": "strict-only cases have empty gold denotations; lenient requires nonempty gold values",
        },
        "strict_recall_failures": {
            "n": strict_failure_total,
            "pct": _pct(strict_failure_total, total),
            "bottlenecks": {
                name: {"n": count, "pct_of_strict_failures": _pct(count, strict_failure_total)}
                for name, count in strict_failure_bottlenecks.most_common()
            },
            "neither_strict_nor_lenient": neither_total,
            "profile_coverage": profile_coverage,
            "most_common_missing_sketch_features": dict(missing_features.most_common(30)),
            "nearest_candidate_missing_features": dict(nearest_missing.most_common(30)),
            "nearest_candidate_extra_features": dict(nearest_extra.most_common(30)),
            "most_common_nearest_sketch_deltas": delta_clusters,
            "missing_role_column_counts": dict(missing_roles.most_common()),
            "gold_feature_counts": dict(gold_no_lenient_features.most_common(30)),
        },
        "by_difficulty": {
            difficulty: dict(counts)
            for difficulty, counts in sorted(by_difficulty.items())
        },
        "samples": dict(samples),
        "ranker_training_audit": dict(training_audit) if training_audit else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    data = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
    results = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "results"))
    parser.add_argument("--data", default=data)
    parser.add_argument("--dbs", required=True)
    parser.add_argument("--config", choices=["gold_tables", "whole_db"], default="gold_tables")
    parser.add_argument("--cap", type=int, default=5000)
    parser.add_argument("--pool", type=int, default=180)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--proposer-model", default="")
    parser.add_argument("--profile-expansion", action="store_true")
    parser.add_argument("--profile-max-candidates", type=int, default=32)
    parser.add_argument("--profile-per-profile", type=int, default=4)
    parser.add_argument("--profile-generation-penalty", type=float, default=5.0)
    parser.add_argument("--profile-binding-quality-weight", type=float, default=2.0)
    parser.add_argument("--out", default=os.path.join(results, "ast_failure_analysis.json"))
    parser.add_argument(
        "--details",
        default=os.path.join(results, "ast_failure_analysis_per_example.json"),
    )
    parser.add_argument(
        "--ranker-cache",
        default=os.path.join(data, "ranker_train_gold_180.jsonl"),
    )
    args = parser.parse_args()
    if args.profile_expansion and not args.proposer_model:
        parser.error("--profile-expansion requires --proposer-model")
    if (
        args.profile_max_candidates < 1
        or args.profile_per_profile < 1
        or args.profile_generation_penalty < 0
        or args.profile_binding_quality_weight < 0
    ):
        parser.error("profile candidate budgets must be positive and weights nonnegative")

    with open(os.path.join(args.data, "dev.json"), encoding="utf-8") as handle:
        examples = json.load(handle)
    if args.limit:
        examples = examples[:args.limit]
    with open(os.path.join(args.data, "tables.json"), encoding="utf-8") as handle:
        metas = {meta["db_id"]: meta for meta in json.load(handle)}

    proposal_model = None
    proposal_provider = None
    question_vectors = ()
    profile_config = None
    if args.proposer_model:
        from engine.encoder_overlay import EncoderQuery
        from engine.sql_proposal import SQLProposalModel

        proposal_model = SQLProposalModel.load(args.proposer_model)
        print("loading frozen encoder for profile proposals...", flush=True)
        proposal_encoder = EncoderQuery()
        proposal_provider = ProposalSignalProvider(proposal_model, proposal_encoder)
        question_vectors = proposal_encoder._encode([
            str(example["question"]) for example in examples
        ])
    if args.profile_expansion:
        profile_config = ProfileSearchConfig(
            max_candidates=args.profile_max_candidates,
            per_profile=args.profile_per_profile,
            generation_penalty=args.profile_generation_penalty,
            binding_quality_weight=args.profile_binding_quality_weight,
            preserve_baseline_top=True,
        )

    details = []
    db_cache: dict[str, dict[str, dict[str, Any]]] = {}
    started = time.time()
    for index, example in enumerate(examples):
        db_id = str(example["db_id"])
        metadata = metas[db_id]
        if db_id not in db_cache:
            db_cache[db_id] = load_capped(os.path.join(args.dbs, db_id + ".sqlite"), args.cap)
        capped = db_cache[db_id]
        if args.config == "gold_tables":
            names = [name.lower() for name in recursive_gold_table_names(example, metas)]
            tables = [capped[name] for name in names if name in capped] or list(capped.values())
        else:
            tables = list(capped.values())

        searcher = SQLSearcher.from_tables(
            tables,
            spider_foreign_keys(metadata),
            max_candidates=args.pool,
        )
        signals = None
        if proposal_provider is not None:
            signals = proposal_provider.signals(
                str(example["question"]), searcher.schema, question_vectors[index]
            )
        candidates = searcher.search(
            str(example["question"]),
            semantic_signals=signals,
            profile_config=profile_config,
        )
        connection = build_mem_db(tables)
        gold_rows, gold_error = exec_sql_timed(connection, str(example["query"]))
        assessed = []
        if gold_error is None:
            for rank, candidate in enumerate(candidates):
                rows, error = exec_sql_timed(connection, candidate.sql)
                comparison = compare(gold_rows, rows) if error is None else {}
                assessed.append(CandidateAssessment(
                    rank=rank,
                    sql=candidate.sql,
                    profile=profile_query(candidate.query),
                    executable=error is None,
                    lenient=bool(comparison.get("lenient")),
                    strict=bool(comparison.get("strict")),
                ))
        connection.close()

        gold_profile = profile_spider_sql(example["sql"], metadata)
        diagnosis = diagnose_pool(gold_profile, assessed)
        if gold_error is not None:
            diagnosis["status"] = "gold_execution_failure"
            diagnosis["bottleneck"] = "gold_execution_failure"
        details.append({
            "index": index,
            "db_id": db_id,
            "difficulty": eval_hardness(example["sql"]),
            "question": example["question"],
            "gold": example["query"],
            "gold_profile": gold_profile.to_dict(),
            "top_sql": candidates[0].sql if candidates else None,
            "diagnosis": diagnosis,
        })
        if (index + 1) % 250 == 0:
            print(f"  {index + 1}/{len(examples)}", flush=True)

    training_audit = None
    if args.ranker_cache and os.path.exists(args.ranker_cache):
        training_audit = audit_ranker_cache(args.ranker_cache)
    result = summarize(
        details,
        time.time() - started,
        args.config,
        args.pool,
        args.top_k,
        training_audit,
    )
    result["proposer_model"] = args.proposer_model or None
    result["profile_expansion"] = bool(profile_config)
    for path, payload in ((args.out, result), (args.details, details)):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
