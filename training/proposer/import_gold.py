"""Measure Spider TRAIN import coverage and build execution-verified SFT targets.

An example becomes a target only when its gold SQL imports into the typed AST, validates,
renders through the engine's own renderer, executes on the capped tables, and strictly matches
the gold execution. The importer (engine/sql_import.py) is the one serving uses on proposals, so
targets are exactly the SQL the served proposer is allowed to emit. Dev is never read here.

    python -m training.proposer.import_gold --limit 1000                    # coverage sample
    python -m training.proposer.import_gold         --targets training/proposer/data/experiments/<id>/targets.jsonl     # full + targets
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

from engine.sql_ast import render_query, validate_query
from engine.sql_import import Unsupported, import_sql
from engine.sql_schema import SchemaGraph
from spider.probe.evalutil import build_mem_db, exec_sql_timed, load_capped
from spider.probe.full_eval import _git_provenance
from spider.probe.spider_eval import compare, spider_foreign_keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "spider", "data"))
    ap.add_argument("--dbs", default=os.path.join(ROOT, "spider", "data", "dbs"))
    ap.add_argument("--split", default="train_spider.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--cap", type=int, default=5000)
    ap.add_argument("--targets", default="",
                    help="write covered (schema, question, rendered SQL) SFT targets here")
    args = ap.parse_args()
    if args.split.startswith("dev"):
        ap.error("dev split is evaluation-only")

    with open(os.path.join(args.data, args.split), encoding="utf-8") as handle:
        examples = json.load(handle)
    with open(os.path.join(args.data, "tables.json"), encoding="utf-8") as handle:
        tables_meta = {table["db_id"]: table for table in json.load(handle)}
    if args.limit:
        examples = examples[:args.limit]

    db_cache: dict[str, tuple] = {}
    outcomes = collections.Counter()
    reasons = collections.Counter()
    out = None
    if args.targets:
        os.makedirs(os.path.dirname(args.targets), exist_ok=True)
        out = open(args.targets, "w", encoding="utf-8")
        out.write(json.dumps({"_meta": {"split": args.split, "cap": args.cap,
                                        **_git_provenance(ROOT)}}) + "\n")

    for i, example in enumerate(examples):
        db_id = example["db_id"]
        db_path = os.path.join(args.dbs, db_id + ".sqlite")
        if not os.path.exists(db_path):
            outcomes["missing_db"] += 1
            continue
        if db_id not in db_cache:
            capped = load_capped(db_path, cap=args.cap)
            fks = spider_foreign_keys(tables_meta[db_id])
            graph = SchemaGraph.from_tables(list(capped.values()), fks)
            db_cache[db_id] = (capped, build_mem_db(list(capped.values())), graph)
        capped, connection, graph = db_cache[db_id]
        gold_rows, gold_error = exec_sql_timed(connection, example["query"], timeout=8.0)
        if gold_error or gold_rows is None:
            outcomes["gold_error"] += 1
            continue
        try:
            query = import_sql(example["query"], graph)
            validate_query(query)
            rendered = render_query(query)
        except Unsupported as exc:
            outcomes["unsupported"] += 1
            reasons[str(exc).split("'")[0][:40]] += 1
            continue
        except (TypeError, ValueError) as exc:
            outcomes["invalid_ast"] += 1
            reasons[f"validate:{str(exc)[:34]}"] += 1
            continue
        rows, error = exec_sql_timed(connection, rendered, timeout=8.0)
        if error or rows is None:
            outcomes["render_exec_error"] += 1
            reasons[f"exec:{str(error)[:34]}"] += 1
            continue
        if compare(gold_rows, rows).get("strict"):
            outcomes["covered"] += 1
            if out is not None:
                out.write(json.dumps({"idx": i, "db_id": db_id,
                                      "question": example["question"],
                                      "sql": rendered}) + "\n")
        else:
            outcomes["denotation_mismatch"] += 1
        if (i + 1) % 500 == 0:
            print(f"  {i + 1}/{len(examples)}  {dict(outcomes)}", flush=True)
    if out is not None:
        out.close()

    total = sum(outcomes.values())
    print(f"\ncoverage: {outcomes['covered']}/{total} "
          f"({round(100 * outcomes['covered'] / max(total, 1), 1)}%)  {dict(outcomes)}")
    print("top rejection reasons:")
    for reason, count in reasons.most_common(12):
        print(f"  {reason:44s} {count}")


if __name__ == "__main__":
    main()
