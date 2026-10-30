"""The database split, and schema-item labels read from Spider's own parse.

Databases are split, never questions:

  test         the 40 official Spider test databases: never labelled here, never inspected
  calibration  20 of the 140 train_spider databases, drawn with seed 20261019: checkpoint selection, thresholds,
               refusal, ablations
  dev          the 20 official dev databases: never trained on, so a score on them says something about databases
               the readers have not seen. The readers shipped through 2026-10 trained on them, so 803/1,034 on dev is
               not such a score; 71.3% on the 40 test databases is (DECISIONS.md, 2026-10-30).
  train        the other 120 and the 6 train_others databases

Each Spider question is labelled with the tables and columns its gold SQL uses and the role each column plays there
(select, filter, group, having, order, join), read from Spider's own parse. No question word is string-matched to a
column: the learner must find the link itself.

Writes <out dir>/manifest.json (the seed, the calibration and test databases, question and database counts, the
source zip's and the labels' SHA-256), labels.jsonl (one record per question) and schemas.json (the declared schema
of every split database). The other steps read manifest.json's calibration_dbs (training.readers.gold,
training.readers.labels_syn).

usage: python -m training.readers.splits <out dir> [--spider-zip <spider_data.zip>]
"""
import argparse
import collections
import hashlib
import json
import os
import random
import zipfile

from training.readers.sources import SPIDER_ZIP

SEED = 20261019
CALIBRATION_DBS = 20
ROLE = {"select": "select", "where": "filter", "groupBy": "group", "having": "having", "orderBy": "order"}


def roles_of(sql):
    """{column id: set of roles} over a Spider parse, nested queries included."""
    found = collections.defaultdict(set)

    def cols(node, role):
        if isinstance(node, dict):
            visit(node)
        elif isinstance(node, list):
            if len(node) == 3 and type(node[0]) is int and type(node[1]) is int and isinstance(node[2], bool):
                if node[1] != 0:
                    found[node[1]].add(role)
                return
            for item in node:
                cols(item, role)

    def visit(query):
        for key, role in ROLE.items():
            cols(query.get(key), role)
        source = query.get("from") or {}
        cols(source.get("conds"), "join")
        for unit in source.get("table_units") or []:
            if unit and unit[0] == "sql":
                visit(unit[1])
        for key in ("intersect", "union", "except"):
            if query.get(key):
                visit(query[key])
    visit(sql)
    return found


def tables_of(sql):
    """The table ids a Spider parse reads, nested queries included."""
    found = set()

    def visit(query):
        for unit in (query.get("from") or {}).get("table_units") or []:
            if unit[0] == "table_unit":
                found.add(unit[1])
            elif unit[0] == "sql":
                visit(unit[1])
        for key in ("intersect", "union", "except"):
            if query.get(key):
                visit(query[key])

        def nested(node):
            if isinstance(node, dict) and "select" in node:
                visit(node)
            elif isinstance(node, dict):
                for value in node.values():
                    nested(value)
            elif isinstance(node, list):
                for item in node:
                    nested(item)
        for key in ("where", "having"):
            nested(query.get(key))
    visit(sql)
    return found


def main():
    ap = argparse.ArgumentParser(description="The database split manifest and schema-item labels.")
    ap.add_argument("out_dir")
    ap.add_argument("--spider-zip", default=SPIDER_ZIP)
    args = ap.parse_args()
    with zipfile.ZipFile(args.spider_zip) as z:
        def load(name):
            return json.loads(z.read("spider_data/" + name))
        train_spider, train_others, dev, test = (load("train_spider.json"), load("train_others.json"),
                                                 load("dev.json"), load("test.json"))
        tables = {t["db_id"]: t for t in load("tables.json")}
    test_dbs = {q["db_id"] for q in test}
    dev_dbs = {q["db_id"] for q in dev}
    main_dbs = sorted({q["db_id"] for q in train_spider})
    assert not (test_dbs | dev_dbs) & (set(main_dbs) | {q["db_id"] for q in train_others})
    calibration = sorted(random.Random(SEED).sample(main_dbs, CALIBRATION_DBS))
    split_of = {db: ("calibration" if db in calibration else "train") for db in tables
                if db not in test_dbs and db not in dev_dbs}

    os.makedirs(args.out_dir, exist_ok=True)
    labels_path = os.path.join(args.out_dir, "labels.jsonl")
    counts = collections.Counter()
    with open(labels_path, "w", encoding="utf-8", newline="\n") as sink:
        for source, rows in (("train_spider", train_spider), ("train_others", train_others)):
            for index, q in enumerate(rows):
                db = tables[q["db_id"]]
                names, table_names = db["column_names_original"], db["table_names_original"]
                roles = roles_of(q["sql"])
                used_tables = tables_of(q["sql"]) | {names[c][0] for c in roles}
                record = {
                    "source": source, "index": index, "db_id": q["db_id"], "split": split_of[q["db_id"]],
                    "question": q["question"],
                    "tables": sorted(table_names[t] for t in used_tables if t >= 0),
                    "columns": sorted([table_names[names[c][0]], names[c][1], sorted(r)] for c, r in roles.items()),
                }
                sink.write(json.dumps(record) + "\n")
                counts[record["split"]] += 1
    schemas = {db_id: {"tables": t["table_names_original"],
                       "columns": [[t["table_names_original"][ti] if ti >= 0 else None, name, kind]
                                   for (ti, name), kind in zip(t["column_names_original"], t["column_types"])],
                       "primary_keys": t["primary_keys"], "foreign_keys": t["foreign_keys"]}
               for db_id, t in tables.items() if db_id in split_of}
    with open(os.path.join(args.out_dir, "schemas.json"), "w", encoding="utf-8", newline="\n") as sink:
        json.dump(schemas, sink)
    with open(args.spider_zip, "rb") as source, open(labels_path, "rb") as labels:
        zip_sha256, labels_sha256 = hashlib.sha256(source.read()).hexdigest(), hashlib.sha256(labels.read()).hexdigest()
    manifest = {"seed": SEED, "calibration_dbs": calibration, "test_dbs": sorted(test_dbs), "dev_dbs": sorted(dev_dbs),
                "questions": dict(counts), "dbs": collections.Counter(split_of.values()),
                "source_zip_sha256": zip_sha256, "labels_sha256": labels_sha256}
    with open(os.path.join(args.out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as sink:
        json.dump(manifest, sink, indent=1)
    print(json.dumps({k: v for k, v in manifest.items() if k != "test_dbs"}, indent=1))


if __name__ == "__main__":
    main()
