"""Label the served candidate pools of Spider TRAIN questions for arbiter fitting.

Every question runs through the production selection (`TableQuery.select_query`, via
`spider.probe.full_eval.ast_predict` in its pool_oracle mode): deterministic search, proposer
beams, pool execution. Each pooled candidate is then labeled strict/lenient by comparing its
executed rows with the train gold denotation on the same capped tables, and carries the proposer
likelihood the arbiter reads. Spider train gold is training data only; the dev split is never
read here and no gold-derived signal reaches a serving decision.

The pools come from the runtime bundle the engine loads (engine/data, or PREREASONER_DATA_DIR).
To label pools for a candidate proposer, point PREREASONER_DATA_DIR at a candidate bundle
directory instead of changing engine/data.

Output is JSONL: one `_meta` record (pool contract, bundle fingerprint, code provenance), then one
record per question with its labeled pool. Reruns resume: finished indexes are skipped.

    python -m training.rank.build_pool_labels \\
        --out training/rank/data/experiments/<id>/pools.jsonl [--dbs-filter db1,db2]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spider.probe.evalutil import build_mem_db, exec_sql_timed, load_capped, run_with_budget
from spider.probe.full_eval import _git_provenance, ast_predict
from spider.probe.hardness import eval_hardness
from spider.probe.spider_eval import compare, spider_foreign_keys


def pool_contract(enc) -> dict:
    """How the labeled pools were built: the fields a fitted arbiter must be served under."""
    from engine.sql_rank import BEAM_STEP, PROPOSAL_PENALTY

    arbiter = enc.sql_arbiter
    return {
        "search_candidates": arbiter.search_candidates,
        "proposer_beams": arbiter.proposer_beams,
        "proposer_max_new_tokens": arbiter.proposer_max_new_tokens,
        "execution_op_limit": arbiter.execution_op_limit,
        "proposal_penalty": PROPOSAL_PENALTY,
        "beam_step": BEAM_STEP,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "spider", "data"))
    ap.add_argument("--dbs", default=os.path.join(ROOT, "spider", "data", "dbs"))
    ap.add_argument("--split", default="train_spider.json",
                    help="examples file inside --data; the dev split is never read here")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0, help="stop after N new examples (0 = all)")
    ap.add_argument("--cap", type=int, default=5000, help="row cap per table")
    ap.add_argument("--dbs-filter", default="",
                    help="comma-separated db_ids to label (shards and pilot subsets)")
    ap.add_argument("--timeout", type=float, default=60.0, help="soft per-example budget")
    args = ap.parse_args()
    if args.split.startswith("dev"):
        ap.error("dev split is evaluation-only; labels come from the train split")

    with open(os.path.join(args.data, args.split), encoding="utf-8") as handle:
        examples = json.load(handle)
    with open(os.path.join(args.data, "tables.json"), encoding="utf-8") as handle:
        tables_meta = {table["db_id"]: table for table in json.load(handle)}
    fks = {db_id: spider_foreign_keys(meta) for db_id, meta in tables_meta.items()}

    print("loading the runtime bundle (encoder, SQL proposer, arbiter)...", flush=True)
    from engine.encoder_overlay import EncoderQuery
    enc = EncoderQuery()

    done = set()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                if "idx" in record:
                    done.add(record["idx"])
        print(f"resuming: {len(done)} examples already labeled", flush=True)
    out = open(args.out, "a", encoding="utf-8")
    if not done:
        out.write(json.dumps({"_meta": {
            "split": args.split, "cap": args.cap, "config": "whole_db",
            "pool": pool_contract(enc),
            "model_bundle_sha256": enc.model_bundle_sha256,
            "proposer_adapter_sha256": enc.sql_proposer.adapter_sha256,
            "proposer_device": enc.sql_proposer.device,
            **_git_provenance(ROOT),
        }}) + "\n")
    print(f"labeling {len(examples)} train examples (skipping {len(done)})", flush=True)

    db_cache: dict[str, tuple] = {}
    schema_cache: dict = {}
    stats = collections.Counter()
    new = 0
    dbs_filter = frozenset(filter(None, args.dbs_filter.split(","))) or None
    for i, example in enumerate(examples):
        if i in done:
            continue
        if args.limit and new >= args.limit:
            break
        db_id = example["db_id"]
        if dbs_filter is not None and db_id not in dbs_filter:
            continue
        db_path = os.path.join(args.dbs, db_id + ".sqlite")
        if not os.path.exists(db_path):
            stats["missing_db"] += 1
            continue
        if db_id not in db_cache:
            capped = load_capped(db_path, cap=args.cap)
            db_cache[db_id] = (capped, build_mem_db(list(capped.values())))
        capped, gold_connection = db_cache[db_id]
        gold_rows, gold_error = exec_sql_timed(gold_connection, example["query"], timeout=8.0)
        if gold_error or gold_rows is None:
            stats["gold_error"] += 1
            continue

        tabs = list(capped.values())
        result, error, seconds, over_budget = run_with_budget(
            lambda t=tabs, q=example["question"], f=fks.get(db_id): ast_predict(
                enc, t, q, f, schema_cache, selection="pool_oracle"),
            args.timeout,
        )
        new += 1
        record = {
            "idx": i, "db_id": db_id, "question": example["question"],
            "gold": example["query"], "difficulty": eval_hardness(example["sql"]),
            "over_budget": over_budget, "prediction_seconds": round(seconds, 3),
            "candidates": [],
        }
        if error is not None or not result.get("pool_execution"):
            record["error"] = str(error) if error is not None else result.get("error", "no pool")
            stats["no_pool"] += 1
        else:
            for entry in result["pool_execution"]:
                labeled = {"rank": entry["rank"], "sql": entry["sql"],
                           "score": entry["score"], "features": entry["features"],
                           "evidence": entry["evidence"]}
                if "likelihood" in entry:
                    labeled["features"] = {**labeled["features"],
                                           "proposer:scored_logprob": entry["likelihood"],
                                           "proposer:scored_tokens": float(entry["likelihood_tokens"])}
                if "rows" in entry:
                    comparison = compare(gold_rows, entry["rows"])
                    labeled["strict"] = bool(comparison.get("strict"))
                    labeled["lenient"] = bool(comparison.get("lenient"))
                else:
                    labeled["error"] = entry["error"]
                record["candidates"].append(labeled)
            stats["pool_strict_hit"] += any(c.get("strict") for c in record["candidates"])
            stats["labeled"] += 1
        out.write(json.dumps(record) + "\n")
        if new % 25 == 0:
            out.flush()
            print(f"  {new} labeled ({i + 1}/{len(examples)} scanned)  {dict(stats)}", flush=True)
    out.close()
    print(f"done: {dict(stats)}  -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
