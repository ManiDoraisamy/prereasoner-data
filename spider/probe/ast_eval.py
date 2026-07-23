"""Deterministic SQL AST phase and optional profile-expansion evaluation.

Phase 1 and Phase 2 share the same base pool; Phase 3 adds bounded recursive candidates;
Phase 4 adds aggregate constraints, disjunctions, and relational subqueries; Phase 5 adds
arg-extrema, top-N, and set difference. An optional Phase 6 artifact reranks the Phase 5
pool. Spider-declared
foreign keys are available to every phase. Gold SQL is used only after ranking, for denotation
measurement. This isolates deterministic AST search and ranking from the encoder/compose route.

Run from the repository root:
  python spider/probe/ast_eval.py --dbs spider/data/dbs
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from .evalutil import build_mem_db, exec_sql_timed, load_capped
    from .spider_eval import (
        compare,
        is_scalar,
        recursive_gold_table_names,
        spider_foreign_keys,
    )
except ImportError:  # direct script execution
    from evalutil import build_mem_db, exec_sql_timed, load_capped
    from spider_eval import (
        compare,
        is_scalar,
        recursive_gold_table_names,
        spider_foreign_keys,
    )

from engine.sql_rank import CandidateRanker
from engine.sql_profile_expansion import ProfileSearchConfig
from engine.sql_proposal_runtime import ProposalSignalProvider
from engine.sql_search import SQLSearcher


def _score(counter, gold_rows, candidates, con, top_k):
    if is_scalar(gold_rows):
        counter["scalar_n"] += 1
    counter["candidate_total"] += len(candidates)
    counter["candidate_max"] = max(counter["candidate_max"], len(candidates))
    if not candidates:
        counter["no_candidate"] += 1
        return
    flags = []
    successful = 0
    for candidate in candidates:
        rows, error = exec_sql_timed(con, candidate.sql)
        comparison = compare(gold_rows, rows) if error is None else {}
        flags.append(comparison)
        successful += int(error is None)
    if successful == 0:
        counter["execution_failure"] += 1
        return
    counter["answered"] += 1
    first = flags[0]
    counter["lenient"] += bool(first.get("lenient"))
    counter["strict"] += bool(first.get("strict"))
    counter["oracle_lenient"] += any(flag.get("lenient") for flag in flags)
    counter["oracle_strict"] += any(flag.get("strict") for flag in flags)
    counter["topk_oracle_lenient"] += any(flag.get("lenient") for flag in flags[:top_k])
    counter["topk_oracle_strict"] += any(flag.get("strict") for flag in flags[:top_k])
    if is_scalar(gold_rows):
        counter["scalar"] += bool(first.get("scalar_exact"))


def _summary(counter, total):
    pct = lambda value, denominator=total: round(100 * value / max(denominator, 1), 1)
    return {
        "n": total,
        "answered": counter["answered"],
        "no_candidate": counter["no_candidate"],
        "execution_failure": counter["execution_failure"],
        "gold_execution_failure": counter["gold_execution_failure"],
        "strict_correct": counter["strict"],
        "lenient_correct": counter["lenient"],
        "scalar_correct": counter["scalar"],
        "topk_oracle_strict_correct": counter["topk_oracle_strict"],
        "pool_oracle_strict_correct": counter["oracle_strict"],
        "pool_oracle_lenient_correct": counter["oracle_lenient"],
        "average_candidates": round(counter["candidate_total"] / max(total, 1), 2),
        "maximum_candidates": counter["candidate_max"],
        "lenient_pct": pct(counter["lenient"]),
        "strict_pct": pct(counter["strict"]),
        "scalar_pct": pct(counter["scalar"], counter["scalar_n"]),
        "scalar_n": counter["scalar_n"],
        "topk_oracle_lenient_pct": pct(counter["topk_oracle_lenient"]),
        "topk_oracle_strict_pct": pct(counter["topk_oracle_strict"]),
        "pool_oracle_lenient_pct": pct(counter["oracle_lenient"]),
        "pool_oracle_strict_pct": pct(counter["oracle_strict"]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    parser.add_argument("--dbs", required=True)
    parser.add_argument("--config", choices=["gold_tables", "whole_db"], default="gold_tables")
    parser.add_argument("--cap", type=int, default=5000)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pool", type=int, default=100)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--ranker-model", default="",
                        help="optional frozen Phase 6 ranker JSON")
    parser.add_argument("--proposer-model", default="",
                        help="optional frozen structured proposer JSON")
    parser.add_argument("--profile-expansion", action="store_true",
                        help="explicitly generate candidates from predicted profiles")
    parser.add_argument("--profile-max-candidates", type=int, default=32)
    parser.add_argument("--profile-per-profile", type=int, default=4)
    parser.add_argument("--profile-generation-penalty", type=float, default=5.0)
    parser.add_argument("--profile-binding-quality-weight", type=float, default=2.0)
    parser.add_argument("--allow-profile-top", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    if (
        args.profile_max_candidates < 1
        or args.profile_per_profile < 1
        or args.profile_generation_penalty < 0
        or args.profile_binding_quality_weight < 0
    ):
        parser.error("profile candidate budgets must be positive and profile weights nonnegative")

    rank_model = None
    if args.ranker_model:
        from engine.sql_learned_rank import load_ranker_model
        rank_model = load_ranker_model(args.ranker_model)

    proposal_model = None
    proposal_encoder = None
    proposal_provider = None
    proposal_question_vectors = {}
    if args.proposer_model:
        from engine.encoder_overlay import EncoderQuery
        from engine.sql_proposal import SQLProposalModel

        proposal_model = SQLProposalModel.load(args.proposer_model)
        print("loading frozen encoder for profile proposals...", flush=True)
        proposal_encoder = EncoderQuery()
        proposal_provider = ProposalSignalProvider(proposal_model, proposal_encoder)

    if args.profile_expansion and proposal_model is None:
        parser.error("--profile-expansion requires --proposer-model")
    profile_config = (
        ProfileSearchConfig(
            max_candidates=args.profile_max_candidates,
            per_profile=args.profile_per_profile,
            generation_penalty=args.profile_generation_penalty,
            binding_quality_weight=args.profile_binding_quality_weight,
            preserve_baseline_top=not args.allow_profile_top,
        )
        if args.profile_expansion else None
    )
    if rank_model is not None:
        from engine.sql_learned_rank import verify_ranker_contract

        verify_ranker_contract(
            rank_model,
            proposer_model=proposal_model,
            adapter_sha256=getattr(proposal_encoder, "encoder_adapter_sha256", None),
            profile_config=profile_config,
            pool_size=args.pool,
        )

    dev = json.load(open(os.path.join(args.data, "dev.json"), encoding="utf-8"))
    if args.limit:
        dev = dev[:args.limit]
    if proposal_encoder is not None:
        vectors = proposal_encoder._encode([str(example["question"]) for example in dev])
        proposal_question_vectors = {
            index: vectors[index] for index in range(len(dev))
        }
    metas = {meta["db_id"]: meta
             for meta in json.load(open(os.path.join(args.data, "tables.json"), encoding="utf-8"))}
    db_cache = {}
    stats = {
        "phase1": collections.Counter(),
        "phase2": collections.Counter(),
        "phase3": collections.Counter(),
        "phase4": collections.Counter(),
        "phase5": collections.Counter(),
    }
    if rank_model is not None:
        stats["phase6"] = collections.Counter()
    if proposal_model is not None:
        proposal_key = "profile_expansion" if args.profile_expansion else "proposer_roles"
        stats[proposal_key] = collections.Counter()
        if rank_model is not None:
            stats["profile_ranked"] = collections.Counter()
            stats["profile_ranked_safe"] = collections.Counter()
            stats["profile_promoted"] = collections.Counter()
    started = time.time()

    for index, example in enumerate(dev, 1):
        db_id = example["db_id"]
        if db_id not in db_cache:
            db_cache[db_id] = load_capped(os.path.join(args.dbs, db_id + ".sqlite"), args.cap)
        capped = db_cache[db_id]
        if args.config == "gold_tables":
            names = [name.lower() for name in recursive_gold_table_names(example, metas)]
            tables = [capped[name] for name in names if name in capped] or list(capped.values())
        else:
            tables = list(capped.values())

        schema_fks = spider_foreign_keys(metas[db_id])
        searcher = SQLSearcher.from_tables(tables, schema_fks, max_candidates=args.pool)
        phase1 = searcher.search(
            example["question"], phase2=False, phase3=False, phase4=False, phase5=False
        )
        phase2 = CandidateRanker(searcher.schema).rank(example["question"], phase1)
        phase3 = searcher.search(
            example["question"], phase2=True, phase3=True, phase4=False, phase5=False
        )
        phase4 = searcher.search(
            example["question"], phase2=True, phase3=True, phase4=True, phase5=False
        )
        phase5 = searcher.search(
            example["question"], phase2=True, phase3=True, phase4=True, phase5=True
        )
        profile_candidates = ()
        if proposal_model is not None:
            signals = proposal_provider.signals(
                example["question"], searcher.schema,
                proposal_question_vectors[index - 1],
            )
            profile_candidates = searcher.search(
                example["question"], semantic_signals=signals,
                phase2=True, phase3=True, phase4=True, phase5=True,
                profile_config=profile_config,
            )
        phase6 = rank_model.rerank(example["question"], phase5) if rank_model else ()
        profile_ranked = ()
        profile_ranked_safe = ()
        profile_promoted = ()
        if rank_model is not None and profile_candidates:
            from engine.sql_learned_rank import rerank_with_promotion_gate

            profile_ranked = rank_model.rerank(example["question"], profile_candidates)
            fallback = profile_candidates[0]
            profile_ranked_safe = (fallback,) + tuple(
                candidate for candidate in profile_ranked if candidate.sql != fallback.sql
            )
            profile_promoted = rerank_with_promotion_gate(
                rank_model, example["question"], profile_candidates
            )

        con = build_mem_db(tables)
        gold_rows, gold_error = exec_sql_timed(con, example["query"])
        if gold_error is None:
            _score(stats["phase1"], gold_rows, phase1, con, args.top_k)
            _score(stats["phase2"], gold_rows, phase2, con, args.top_k)
            _score(stats["phase3"], gold_rows, phase3, con, args.top_k)
            _score(stats["phase4"], gold_rows, phase4, con, args.top_k)
            _score(stats["phase5"], gold_rows, phase5, con, args.top_k)
            if proposal_model is not None:
                _score(
                    stats[proposal_key], gold_rows, profile_candidates, con, args.top_k
                )
                if rank_model is not None:
                    _score(stats["profile_ranked"], gold_rows, profile_ranked, con, args.top_k)
                    _score(
                        stats["profile_ranked_safe"], gold_rows, profile_ranked_safe,
                        con, args.top_k,
                    )
                    _score(
                        stats["profile_promoted"], gold_rows, profile_promoted,
                        con, args.top_k,
                    )
            if rank_model is not None:
                _score(stats["phase6"], gold_rows, phase6, con, args.top_k)
        else:
            for counter in stats.values():
                counter["gold_execution_failure"] += 1
        con.close()
        if index % 250 == 0:
            print(f"  {index}/{len(dev)}", flush=True)

    result = {
        "config": args.config,
        "top_k": args.top_k,
        "pool": args.pool,
        "profile_expansion_enabled": args.profile_expansion,
        "profile_max_candidates": args.profile_max_candidates,
        "profile_per_profile": args.profile_per_profile,
        "profile_generation_penalty": args.profile_generation_penalty,
        "profile_binding_quality_weight": args.profile_binding_quality_weight,
        "profile_preserve_baseline_top": not args.allow_profile_top,
        "elapsed_seconds": round(time.time() - started, 2),
        "phase1": _summary(stats["phase1"], len(dev)),
        "phase2": _summary(stats["phase2"], len(dev)),
        "phase3": _summary(stats["phase3"], len(dev)),
        "phase4": _summary(stats["phase4"], len(dev)),
        "phase5": _summary(stats["phase5"], len(dev)),
    }
    if rank_model is not None:
        result["ranker_model"] = args.ranker_model
        result["phase6"] = _summary(stats["phase6"], len(dev))
    if proposal_model is not None:
        result["proposer_model"] = args.proposer_model
        result[proposal_key] = _summary(stats[proposal_key], len(dev))
        if rank_model is not None:
            result["profile_ranked"] = _summary(stats["profile_ranked"], len(dev))
            result["profile_ranked_safe"] = _summary(
                stats["profile_ranked_safe"], len(dev)
            )
            result["profile_promoted"] = _summary(
                stats["profile_promoted"], len(dev)
            )
    print(json.dumps(result, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
            handle.write("\n")


if __name__ == "__main__":
    main()
