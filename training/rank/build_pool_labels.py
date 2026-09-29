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
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from spider.probe.evalutil import build_mem_db, exec_sql_timed, load_capped, run_with_budget
from spider.probe.full_eval import _git_provenance, ast_predict
from spider.probe.hardness import eval_hardness
from spider.probe.spider_eval import compare, spider_foreign_keys
from engine.artifact_provenance import sha256_file


def pool_contract(enc) -> dict:
    """How the labeled pools were built: the fields a fitted arbiter must be served under."""
    from engine.sql_rank import BEAM_STEP, PROPOSAL_PENALTY

    arbiter = enc.sql_arbiter
    return {
        "search_candidates": arbiter.search_candidates,
        "proposer_beams": enc.sql_proposer.beams,
        "proposer_max_new_tokens": enc.sql_proposer.max_new_tokens,
        "execution_op_limit": arbiter.execution_op_limit,
        "proposal_penalty": PROPOSAL_PENALTY,
        "beam_step": BEAM_STEP,
    }


def resume_indices(path, expected_meta):
    """Validate a complete-prefix journal before appending; never mix run contracts."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return set()
    expected = {int(i): db for i, db in expected_meta["expected_examples"]}
    done = set()
    with open(path, encoding="utf-8") as handle:
        header = json.loads(next(handle))
        if header != {"_meta": expected_meta}:
            raise ValueError("pool resume contract differs; use a new output path")
        for line in handle:
            record = json.loads(line)
            idx = record["idx"]
            if idx in done or expected.get(idx) != record["db_id"]:
                raise ValueError(f"duplicate or unexpected pool identity: {idx}")
            done.add(idx)
    return done


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
    ap.add_argument("--sample-per-db", type=int, default=0,
                    help="preregistered seed-7 index-hash sample per DB (0 = every question)")
    args = ap.parse_args()
    if args.split not in {"train_spider.json", "train_others.json"}:
        ap.error("labels require an official TRAIN split filename; DEV/TEST are evaluation-only")
    if args.sample_per_db < 0:
        ap.error("sample-per-db must be nonnegative")

    with open(os.path.join(args.data, args.split), encoding="utf-8") as handle:
        examples = json.load(handle)
    with open(os.path.join(args.data, "tables.json"), encoding="utf-8") as handle:
        tables_meta = {table["db_id"]: table for table in json.load(handle)}
    fks = {db_id: spider_foreign_keys(meta) for db_id, meta in tables_meta.items()}

    print("loading the runtime bundle (encoder, SQL proposer, arbiter)...", flush=True)
    from engine.encoder_overlay import EncoderQuery
    enc = EncoderQuery()

    dbs_filter = frozenset(filter(None, args.dbs_filter.split(","))) or None
    expected = [[i, e["db_id"]] for i, e in enumerate(examples)
                if dbs_filter is None or e["db_id"] in dbs_filter]
    if args.sample_per_db:
        by_db = collections.defaultdict(list)
        for i, db in expected:
            by_db[db].append(i)
        expected = sorted([i, db] for db, indices in by_db.items() for i in sorted(
            indices, key=lambda i: hashlib.sha256(f"7:{db}:{i}".encode()).hexdigest()
        )[:args.sample_per_db])
    expected_indices = {i for i, _ in expected}
    source_files = sorted(path for directory in ("engine", "spider/probe", "training/rank")
                          for path in (Path(ROOT) / directory).rglob("*.py")
                          if "data" not in path.relative_to(ROOT).parts)
    meta = {
        "schema_version": 2, "selection_policy": "shared-ranked-intents-v1",
        "split": args.split, "split_sha256": sha256_file(Path(args.data) / args.split),
        "tables_sha256": sha256_file(Path(args.data) / "tables.json"),
        "cap": args.cap, "config": "whole_db", "timeout": args.timeout,
        "sample_per_db": args.sample_per_db, "sample_seed": 7,
        "pool": pool_contract(enc), "model_bundle_sha256": enc.model_bundle_sha256,
        "proposer_adapter_sha256": enc.sql_proposer.adapter_sha256,
        "proposer_device": enc.sql_proposer.device,
        "proposer_contract": enc.sql_proposer.contract,
        "source_hashes": {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in source_files},
        "expected_examples": expected,
        "databases": {db: sha256_file(Path(args.dbs) / (db + ".sqlite"))
                      if (Path(args.dbs) / (db + ".sqlite")).is_file() else None
                      for db in sorted({db for _, db in expected})},
        **_git_provenance(ROOT),
    }
    from engine.xiyan_sql_proposer import effective_cpu_threads
    meta["proposer_threads"] = effective_cpu_threads(enc.sql_proposer.contract)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    done = resume_indices(args.out, meta)
    has_header = os.path.exists(args.out) and os.path.getsize(args.out) > 0
    out = open(args.out, "a", encoding="utf-8")
    if not has_header:
        out.write(json.dumps({"_meta": meta}) + "\n")
        out.flush()
    print(f"labeling {len(examples)} train examples (skipping {len(done)})", flush=True)

    db_cache: dict[str, tuple] = {}
    schema_cache: dict = {}
    stats = collections.Counter()
    new = 0
    for i, example in enumerate(examples):
        if i in done or i not in expected_indices:
            continue
        if args.limit and new >= args.limit:
            break
        db_id = example["db_id"]
        if dbs_filter is not None and db_id not in dbs_filter:
            continue
        new += 1
        record = {
            "idx": i, "db_id": db_id, "question": example["question"],
            "gold": example["query"], "difficulty": eval_hardness(example["sql"]),
            "candidates": [],
        }
        db_path = os.path.join(args.dbs, db_id + ".sqlite")
        if not os.path.exists(db_path):
            stats["missing_db"] += 1
            record["error"] = "missing_db"
            out.write(json.dumps(record) + "\n")
            out.flush()
            continue
        if db_id not in db_cache:
            capped = load_capped(db_path, cap=args.cap)
            db_cache[db_id] = (capped, build_mem_db(list(capped.values())))
        capped, gold_connection = db_cache[db_id]
        gold_rows, gold_error = exec_sql_timed(gold_connection, example["query"], timeout=8.0)
        if gold_error or gold_rows is None:
            stats["gold_error"] += 1
            record["error"] = f"gold_error: {gold_error}"
            out.write(json.dumps(record) + "\n")
            out.flush()
            continue

        tabs = list(capped.values())
        result, error, seconds, over_budget = run_with_budget(
            lambda t=tabs, q=example["question"], f=fks.get(db_id): ast_predict(
                enc, t, q, f, schema_cache, selection="pool_oracle"),
            args.timeout,
        )
        record.update(over_budget=over_budget, prediction_seconds=round(seconds, 3))
        if error is not None or not result.get("pool_execution"):
            record["error"] = str(error) if error is not None else result.get("error", "no pool")
            stats["no_pool"] += 1
        else:
            for entry in result["pool_execution"]:
                labeled = {"rank": entry["rank"], "sql": entry["sql"],
                           "score": entry["score"], "features": entry["features"],
                           "evidence": entry["evidence"],
                           "proposed": entry["proposed"],
                           "executable": entry["executable"],
                           "grounded": entry["grounded"],
                           "eligible": entry["eligible"],
                           "calculation_satisfied": entry["calculation_satisfied"],
                           "money_total": entry["money_total"]}
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
            stats["pool_strict_hit"] += any(c.get("strict") and c["eligible"]
                                            for c in record["candidates"])
            stats["labeled"] += 1
        out.write(json.dumps(record) + "\n")
        out.flush()
        if new % 25 == 0:
            out.flush()
            print(f"  {new} labeled ({i + 1}/{len(examples)} scanned)  {dict(stats)}", flush=True)
    out.close()
    print(f"done: {dict(stats)}  -> {args.out}", flush=True)


if __name__ == "__main__":
    main()
