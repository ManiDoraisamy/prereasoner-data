"""Candidates with their features and labels, for training and calibrating the re-scorer.

For each question of a rows file with its stage-2 outputs: the k most probable distinct readings
(engine.readers.decode.candidates), each built, rendered, and run on the same capped data as gold's. Recorded per
candidate: its log probability and rank, the read-as line, whether it built, ran, and returned rows (row and column
counts, and its first row as text, "preview"), and, on training and calibration questions only, whether its rows are
gold's ("right"). The Spider test set never passes through here: test answers are chosen and scored in totals by
training.readers.choose.

A reworded question (training.readers.rewordings) is scored against its original's gold query; the written sets
(--reworded, --lookups, --structures) are the ones training.readers.examples reads, so cross-fold readers' held-out
questions of those sets get candidates too.

usage: python -m training.readers.candidates <rows.jsonl> <outputs.jsonl> <out.jsonl> [--k 16] [--floor 0.03]
           [--shard i/n] [--bird <BIRD train dir>] [--syn <Spider-Syn dir>]
           [--reworded <rewordings.jsonl>[,...] --reworded-readings <readings_reworded.jsonl>[,...]]
           [--lookups <dir>] [--structures <dir>[,...]] [--spider-zip <zip>] [--db-dir <dir> ...]
"""
import argparse
import json
import tempfile

from engine.readers import decode as decoding
from engine.readers.read_as import read_as
from engine.readers.readings import build
from engine.sql_ast import render_query
from spider.probe.evalutil import exec_sql_timed
from spider.probe.spider_eval import compare
from training.readers.sources import Sources, add_arguments

CANDIDATE_SECONDS = 2.0
PREVIEW_CHARS = 60                                 # the first result row, as text
K = 16


def candidate_records(output, row, graph, connection, gold_rows, k=K, floor=None):
    """One record per candidate of the pool serving builds (search.reading_variants), most probable first, each SQL
    once: the choice and the re-scorers were fitted on the decoded readings alone, without the variants they rank."""
    from engine.readers.search import Schema, named_values, reading_variants

    records = []
    seen = set()
    named = named_values(Schema(graph), row["question"])
    floor = decoding.FLOOR if floor is None else floor
    for rank, (score, readings) in enumerate(decoding.candidates(output, row, graph, k, floor)):
        for reading in reading_variants(readings, named, graph):
            record = {"rank": rank, "score": round(score, 4), "read_as": read_as(reading), "status": "build",
                      "rows": 0, "columns": 0, "sql": None}
            try:
                sql = render_query(build(reading, graph))
            except Exception:                      # noqa: BLE001 - a reading the builder cannot make into a query
                records.append(record)
                continue
            if sql in seen:
                continue
            seen.add(sql)
            record["sql"] = sql
            rows, error = exec_sql_timed(connection, sql, timeout=CANDIDATE_SECONDS)
            if error:
                record["status"] = "exec-error"
            else:
                record.update(status="ran", rows=len(rows), columns=len(rows[0]) if rows else 0,
                              preview=", ".join(str(v) for v in rows[0])[:PREVIEW_CHARS] if rows else "")
                if gold_rows is not None:
                    record["right"] = bool(compare(gold_rows, rows).get("strict"))
            records.append(record)
    return records


def gold_query(sources, question):
    """A question's gold SQL; a reworded question's is its original's."""
    if "origin" in question and not (question.get("query") or question.get("SQL")):
        source, index = question["origin"]
        question = sources.questions[source][index]
    return question.get("query") or question["SQL"]


def main():
    ap = argparse.ArgumentParser(description="Re-scorer candidates with execution labels.")
    ap.add_argument("rows", help="the stage-2 rows the outputs were read from")
    ap.add_argument("outputs", help="their stage-2 outputs (a run's calibration_outputs.jsonl or outputs' file)")
    ap.add_argument("out")
    ap.add_argument("--k", type=int, default=K)
    ap.add_argument("--floor", type=float, default=None, help=f"default engine.readers.decode.FLOOR ({decoding.FLOOR})")
    ap.add_argument("--shard", default="0/1", help="i/n: only outputs whose line number is i modulo n")
    ap.add_argument("--bird", help="BIRD's train directory, for bird_train questions")
    ap.add_argument("--syn", help="Spider-Syn's directory, for syn_train questions (Spider's databases)")
    ap.add_argument("--reworded", help="comma-separated rewordings.jsonl, as training.readers.examples takes them")
    ap.add_argument("--reworded-readings", help="their readings_reworded.jsonl, in the same order")
    ap.add_argument("--lookups", help="training.readers.lookups' output folder")
    ap.add_argument("--structures", help="comma-separated training.readers.structures output folders, in order")
    add_arguments(ap)
    args = ap.parse_args()
    shard = tuple(map(int, args.shard.split("/")))
    sources = Sources.from_args(args)
    if args.bird:
        sources.add_bird(args.bird)
    if args.syn:
        sources.add_syn(args.syn)
    merged = tempfile.mkdtemp(prefix="candidates-")            # the merged readings the sources write; unused here
    if args.reworded:
        sources.add_reworded(args.reworded.split(","), args.reworded_readings.split(","), merged)
    if args.lookups:
        sources.add_lookups(args.lookups)
    if args.structures:
        sources.add_structures(args.structures.split(","), merged)
    rows = {}
    for line in open(args.rows, encoding="utf-8"):
        row = json.loads(line)
        rows[(row["source"], row["index"])] = row
    databases = {}
    with open(args.out, "w", encoding="utf-8", newline="\n", buffering=1) as sink:
        for n, line in enumerate(open(args.outputs, encoding="utf-8")):
            if n % shard[1] != shard[0]:
                continue
            output = json.loads(line)
            key = (output["source"], output["index"])
            if output["source"] == "test":
                raise ValueError("test questions are chosen and scored by training.readers.choose")
            row = rows[key]
            db = output["db_id"]
            if db not in databases:
                databases[db] = sources.database(db)
            connection, graph = databases[db]
            question = sources.questions[key[0]][key[1]]
            gold_rows, _ = exec_sql_timed(connection, gold_query(sources, question), timeout=8.0)
            sink.write(json.dumps({"source": key[0], "index": key[1], "db_id": db, "question": question["question"],
                                   "candidates": candidate_records(output, row, graph, connection, gold_rows,
                                                                   args.k, args.floor)}) + "\n")


if __name__ == "__main__":
    main()
