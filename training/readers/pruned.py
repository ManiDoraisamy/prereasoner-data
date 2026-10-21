"""Stage-2 rows through stage-1 pruning: for each question of a set, the row serving builds
(training.readers.outputs.serving_row over Stage1.prune, i.e. engine.readers.search's code, with the stage-1 run in
float32), written in the trainer's held-out split so a GPU run, or training.readers.outputs on CPU, writes their
outputs. A slow link then brings back outputs, not weights.

calibration: rows carry the gold readings' labels (kind "stage1"), so the trainer's metric also reads them.
test: rows carry no labels and no gold (kind "test"); only their outputs are used, scored in totals by
    training.readers.choose.

A reader trained without anchors reads rows made with --no-anchors, as serving reads them for it (reader.json).

usage: python -m training.readers.pruned calibration <stage1 run dir> <out.jsonl> --readings <readings_calibration.jsonl>
           [--no-anchors] [--spider-zip <zip>] [--db-dir <dir> ...]
       python -m training.readers.pruned test <stage1 run dir> <out.jsonl> --test-questions <test.json>
           --test-tables <test_tables.json> --test-dbs <dir> [--no-anchors]
"""
import argparse
import json
import os

from engine.readers.search import BUDGET
from engine.readers.text import Schema, fit
from training.readers import examples
from training.readers.outputs import Stage1, serving_row
from training.readers.sources import Sources, add_arguments, capped_graph


def main():
    ap = argparse.ArgumentParser(description="Stage-2 rows through serving's stage-1 pruning.")
    ap.add_argument("which", choices=["calibration", "test"])
    ap.add_argument("stage1_dir", help="a schema_items run (model.pt, tokenizer/)")
    ap.add_argument("out")
    ap.add_argument("--readings", help="calibration: the gold readings of the calibration questions")
    ap.add_argument("--test-questions", help="test: Spider's test questions (a JSON list)")
    ap.add_argument("--test-tables", help="test: Spider's test schemas (tables JSON)")
    ap.add_argument("--test-dbs", help="test: the folder of the test databases' <db_id>.sqlite files")
    ap.add_argument("--no-anchors", action="store_true", help="serialize without the question's values")
    add_arguments(ap)
    args = ap.parse_args()
    anchored = not args.no_anchors
    if args.which == "test":
        if not (args.test_questions and args.test_tables and args.test_dbs):
            ap.error("test needs --test-questions, --test-tables and --test-dbs")
        questions = json.load(open(args.test_questions, encoding="utf-8"))
        metas = {t["db_id"]: t for t in json.load(open(args.test_tables, encoding="utf-8"))}
        keyed = [("test", i, q, None) for i, q in enumerate(questions)]

        def graph_of(db):
            return capped_graph(os.path.join(args.test_dbs, db + ".sqlite"), metas[db])
    else:
        if not args.readings:
            ap.error("calibration needs --readings")
        data = Sources.from_args(args)
        readings = {(r["source"], r["index"]): r for r in map(json.loads, open(args.readings, encoding="utf-8"))
                    if "readings" in r}
        keyed = [(source, index, data.questions[source][index], record)
                 for (source, index), record in sorted(readings.items())]
        graph_of = data.graph

    reader = Stage1(args.stage1_dir, threads=4)
    schemas = {}
    with open(args.out, "w", encoding="utf-8", newline="\n") as sink:
        for source, index, q, record in keyed:
            db = q["db_id"]
            if db not in schemas:
                schemas[db] = Schema(graph_of(db))
            schema = schemas[db]
            scores = reader.schema_items(q["question"], schema)
            tables, columns_of, droppable = reader.prune(schema, scores)
            row = serving_row(reader, q["question"], schema, tables, columns_of, droppable, anchored)
            row.update(split="calibration", kind="test" if args.which == "test" else "stage1", source=source,
                       index=index, db_id=db)
            if record is not None:
                labels, values = examples.reading_labels(record["readings"])
                blocks = record["readings"]["blocks"]
                kept = [t["name"] for t in row["tables"]]
                row["tables"] = [dict(t, used=[int(t["name"] in block["tables"]) for block in blocks]
                                      + [0] * (2 - len(blocks))) for t in row["tables"]]
                # The kept tables fit again exactly as serving fitted them; their value spans locate the literals.
                _, (text, _, _, column_spans) = fit(reader.tokenizer, BUDGET, q["question"], schema, kept,
                                                    columns_of, [], anchored)
                assert text == row["text"], "refit must reproduce the serving text"
                located = []
                for b, column, value in values:
                    found = examples.find_value(value, q["question"], column_spans.get(column, (None, []))[1])
                    if found:
                        where, start, end = found
                        located.append({"block": b, "column": column, "value": value, "in": where, "start": start,
                                        "end": end})
                row.update(labels=labels, values=located, structure=record["readings"]["structure"],
                           globals=[{"distinct": int(block["distinct"]), "conj": block["conj"], "dir": block["dir"],
                                     "limit": block["limit"], "counts": examples.counts_of(block, kept)}
                                    for block in blocks])
            sink.write(json.dumps(row) + "\n")
    print(f"{len(keyed)} {args.which} rows -> {args.out}")


if __name__ == "__main__":
    main()
