"""Gold readings: each question's gold SQL -> typed AST (regress.sql_import.import_sql) -> readings
(engine.readers.readings.readings_of), written as the readings files training.readers.examples reads.

The perfect-readings oracle is the check: gold's rows are compared with the rows of the AST as imported, and with
the rows of the query the builder (engine.readers.readings.build) makes back from the readings, on the same capped
data. The printed totals say how much the readings form and the foreign-key joins lose before any model reads
anything. Every question gets one record: {"source", "index", "db_id", "status"} plus "readings" when its SQL is
readable. Status "ok" means the rebuilt query returns gold's rows. "differs" means the AST does and the rebuild does
not. "gold-ast-differs" means the AST as imported does not. "build" means the builder could not make the readings
into a query. "not-readable:<why>" and "import" mean there are no readings. "database" means the capped loader
could not read the question's database (a SynSQL value SQLite cannot hold), so the question was not read.

Sets: calibration and train split Spider's train_spider, train_others and dev questions by the split manifest's
calibration_dbs (training.readers.splits); bird reads BIRD's training set (train.json, train_tables.json,
train_databases/), whose databases are disjoint from Spider's, so every BIRD question is training data; synsql reads
the SynSQL-2.5M questions training.readers.synsql chose, over databases disjoint from both (training data too). Their
databases hold a row or two per table, so the oracle's row check there mostly confirms that a query runs.

usage: python -m training.readers.gold <split dir> [calibration|train|bird|synsql] [--bird <BIRD train dir>]
           [--synsql <selection dir> --synsql-databases <dir>] [--shard i/n] [--resume]
           [--out readings.jsonl] [--spider-zip <zip>] [--db-dir <dir> ...]
"""
import argparse
import collections
import json
import os
import zlib

from engine.readers.readings import NotReadable, build, readings_of
from engine.sql_ast import render_query
from regress.sql_import import import_sql
from spider.probe.evalutil import exec_sql_timed
from spider.probe.spider_eval import compare
from training.readers.sources import Sources, add_arguments, capped_database

SECONDS = 8.0


def gold_sql(q):
    return q.get("query") or q["SQL"]


def read_gold(source, index, q, connection, graph, counts, reasons, structures):
    """The record of one question, counting what the oracle found in ``counts``, ``reasons`` and ``structures``."""
    gold_rows, _ = exec_sql_timed(connection, gold_sql(q), timeout=SECONDS)
    counts["questions"] += 1
    record = {"source": source, "index": index, "db_id": q["db_id"]}
    try:
        query = import_sql(gold_sql(q), graph)
    except Exception as exc:                       # noqa: BLE001 - the grammar cannot hold it
        reasons["import:" + str(exc)[:40]] += 1
        record["status"] = "import"
        return record
    counts["imported"] += 1
    try:
        rows, err = exec_sql_timed(connection, render_query(query), timeout=SECONDS)
    except Exception as exc:                       # noqa: BLE001 - the AST validator refuses the gold shape
        rows, err = None, exc
    ast_ok = err is None and bool(compare(gold_rows, rows).get("strict"))
    counts["ast_rows"] += ast_ok
    try:
        readings = readings_of(query)
    except NotReadable as exc:
        reasons["readings:" + str(exc)] += 1
        record["status"] = "not-readable:" + str(exc)
        return record
    counts["readable"] += 1
    structures[readings["structure"]] += 1
    record["readings"] = readings
    try:
        sql = render_query(build(readings, graph))
    except Exception as exc:                       # noqa: BLE001 - classify what the builder cannot make
        reasons["build:" + type(exc).__name__ + ":" + str(exc)[:40]] += 1
        record["status"] = "build"
        return record
    rows, err = exec_sql_timed(connection, sql, timeout=SECONDS)
    ok = err is None and bool(compare(gold_rows, rows).get("strict"))
    counts["rebuilt_rows"] += ok
    counts["rebuilt_rows_where_ast_ok"] += ok and ast_ok
    if ast_ok and not ok:
        reasons["rebuilt-differs" + (":render-error" if err else "")] += 1
    record["status"] = "ok" if ok else ("differs" if ast_ok else "gold-ast-differs")
    return record


def question_set(which, split_dir, args):
    """([(source, index, question)], {db_id: (connection, graph)} loader) for a set."""
    if which == "bird":
        if not args.bird:
            raise SystemExit("bird needs --bird <BIRD train dir>")
        tables = {t["db_id"]: t for t in json.load(open(os.path.join(args.bird, "train_tables.json"),
                                                        encoding="utf-8"))}
        questions = [("bird_train", index, q)
                     for index, q in enumerate(json.load(open(os.path.join(args.bird, "train.json"), encoding="utf-8")))]

        def database(db):
            return capped_database(os.path.join(args.bird, "train_databases", db, db + ".sqlite"), tables[db])
        return questions, database
    sources = Sources.from_args(args)
    if which == "synsql":
        if not (args.synsql and args.synsql_databases):
            raise SystemExit("synsql needs --synsql <selection dir> --synsql-databases <dir>")
        sources.add_synsql(args.synsql, args.synsql_databases)
        return [("synsql", index, q) for index, q in enumerate(sources.questions["synsql"])], sources.database
    calibration = set(json.load(open(os.path.join(split_dir, "manifest.json")))["calibration_dbs"])
    questions = [(name, index, q) for name in ("train_spider", "train_others", "dev")
                 for index, q in enumerate(sources.questions[name])
                 if (q["db_id"] in calibration) == (which == "calibration")]
    return questions, sources.database


def main():
    ap = argparse.ArgumentParser(description="Gold readings of a question set, checked by the perfect-readings oracle.")
    ap.add_argument("split_dir", help="the folder of the split's manifest.json (training.readers.splits)")
    ap.add_argument("which", nargs="?", default="calibration", choices=["calibration", "train", "bird", "synsql"])
    ap.add_argument("--bird", help="BIRD's train directory (for bird)")
    ap.add_argument("--synsql", help="training.readers.synsql's selection folder (for synsql)")
    ap.add_argument("--synsql-databases", help="SynSQL-2.5M's databases folder (for synsql)")
    ap.add_argument("--shard", default="0/1", help="i/n: only the questions of databases whose CRC-32 is i modulo n")
    ap.add_argument("--out", help="readings.jsonl to write; without it only the totals print")
    ap.add_argument("--resume", action="store_true", help="keep --out's records and read only the questions it lacks")
    add_arguments(ap)
    args = ap.parse_args()
    questions, database = question_set(args.which, args.split_dir, args)
    part, parts = map(int, args.shard.split("/"))
    questions = [item for item in questions if zlib.crc32(item[2]["db_id"].encode()) % parts == part]
    databases = {}
    counts, reasons, structures = collections.Counter(), collections.Counter(), collections.Counter()
    done = set()
    if args.resume and args.out and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as kept:
            done = {(r["source"], r["index"]) for r in map(json.loads, kept)}
    sink = open(args.out, "a" if done else "w", encoding="utf-8", newline="\n") if args.out else None
    for source, index, q in questions:
        if (source, index) in done:
            continue
        db = q["db_id"]
        if db not in databases:
            try:
                databases[db] = database(db)
            except (OverflowError, ValueError) as exc:    # a value the capped loader's SQLite cannot hold
                reasons["database:" + type(exc).__name__] += 1
                databases[db] = None
        if databases[db] is None:
            counts["questions"] += 1
            record = {"source": source, "index": index, "db_id": db, "status": "database"}
        else:
            connection, graph = databases[db]
            record = read_gold(source, index, q, connection, graph, counts, reasons, structures)
        if sink:
            sink.write(json.dumps(record) + "\n")
    if sink:
        sink.close()
    n = counts["questions"]
    print(f"{args.which}: {n} questions")
    for key in ("imported", "ast_rows", "readable", "rebuilt_rows", "rebuilt_rows_where_ast_ok"):
        print(f"  {key:28s} {counts[key]:5d}  {counts[key] / max(1, n):.1%}")
    print("  structures:", dict(structures))
    for reason, count in reasons.most_common(15):
        print(f"  {count:5d} {reason}")


if __name__ == "__main__":
    main()
