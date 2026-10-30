"""SynSQL-2.5M (Li et al., 2025; Apache-2.0): the questions over its 16,583 synthetic SQLite databases that the
readers train on, chosen without reading any of them.

``compact`` streams the published data.json (a 9.4 GB JSON list) once into compact.jsonl: each sample's database,
question, SQL, style and complexity, and whether it leans on "external knowledge" (a note that defines its words,
such as "complicated means a complexity above 5"). ``select`` then keeps the samples a reader can learn from:

- no external knowledge, and a style that says what it asks (formal, colloquial, imperative, interrogative,
  descriptive, concise): a vague or metaphorical question needs its note, and learning it without the note would
  teach the readers to guess words' meanings;
- a database unlike every calibration, dev and test database: the share of its column names that one of those databases
  also has (and its table names) stays under ``SIMILAR``, so nothing near a held-out schema trains;
- a question no Spider question repeats;
- SQL in the shapes the readings hold: a common table expression, a window, CASE, string concatenation or a date or
  cast function is never readable (engine.readers.readings), so it is left out before the oracle runs;
- at most ``PER_DB`` samples per database, drawn by a seeded hash, so no database dominates (training.readers.gold
  then keeps the readable ones, and examples take at most a set number of those per database).

It writes questions.json (Spider's question format, with the SynSQL index and style) and tables.json (Spider's
schema format, the list-valued fields parsed). training.readers.gold reads them as the set "synsql" and
training.readers.sources adds them as the source "synsql".

``readings`` merges gold's shards of the selection and keeps, per database, at most ``--per-db`` questions whose
readings rebuild (status "ok"), drawn by the same seeded hash: the readings file training.readers.examples reads.

usage: python -m training.readers.synsql compact <SynSQL dir>
       python -m training.readers.synsql select <SynSQL dir> <out dir> --split <split dir> --test-tables <json>
           [--per-db 24] [--seed 20261025] [--spider-zip <zip>]
       python -m training.readers.synsql readings <gold shard.jsonl> [...] --out <readings_synsql.jsonl>
           [--per-db 6] [--seed 20261025]
"""
import argparse
import ast
import hashlib
import json
import os
import re
import zipfile

from training.readers.sources import SPIDER_SETS, SPIDER_ZIP

STYLES = frozenset({"Formal", "Colloquial", "Imperative", "Interrogative", "Descriptive", "Concise"})
PER_DB = 24
UNREADABLE = re.compile(r"\bWITH\b|\bOVER\s*\(|\bCASE\b|\|\||\bSTRFTIME\b|\bJULIANDAY\b|\bCAST\b|\bIIF\b|"
                        r"\bRECURSIVE\b|\bDATE\s*\(|\bSUBSTR\b", re.IGNORECASE)
SEED = 20261025
SIMILAR = 0.5
LIST_FIELDS = ("column_names", "column_names_original", "column_types", "table_names", "table_names_original",
               "foreign_keys", "primary_keys")


def stream(path, chunk=1 << 22):
    """The objects of a JSON list, one at a time, without reading the whole file."""
    decoder = json.JSONDecoder()
    with open(path, encoding="utf-8") as source:
        buffer = source.read(chunk)
        position = buffer.index("[") + 1
        while True:
            while True:
                while position < len(buffer) and buffer[position] in " \t\r\n,":
                    position += 1
                if position < len(buffer):
                    break
                more = source.read(chunk)
                if not more:
                    return
                buffer, position = buffer[position:] + more, 0
            if buffer[position] == "]":
                return
            try:
                item, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                more = source.read(chunk)
                if not more:
                    raise
                buffer, position = buffer[position:] + more, 0
                continue
            yield item
            position = end
            if position > chunk:
                buffer, position = buffer[position:], 0


def compact(directory):
    out = os.path.join(directory, "compact.jsonl")
    count = 0
    with open(out, "w", encoding="utf-8", newline="\n") as sink:
        for index, sample in enumerate(stream(os.path.join(directory, "data.json"))):
            sink.write(json.dumps({"index": index, "db_id": sample["db_id"], "question": sample["question"],
                                   "query": sample["sql"], "style": sample.get("question_style"),
                                   "complexity": sample.get("sql_complexity"),
                                   "knowledge": bool((sample.get("external_knowledge") or "").strip())}) + "\n")
            count += 1
    print(f"{count} samples -> {out}")


def parsed_schema(meta):
    """A SynSQL schema with its list-valued fields parsed (the published file stores them as Python literals)."""
    return {key: (ast.literal_eval(value) if key in LIST_FIELDS and isinstance(value, str) else value)
            for key, value in meta.items() if key != "ddls"}


def _names(meta):
    columns = {str(name).lower() for table, name in meta["column_names_original"] if table >= 0}
    tables = {str(name).lower() for name in meta["table_names_original"]}
    return columns, tables


def _overlap(a, b):
    return len(a & b) / max(1, min(len(a), len(b)))


def held_out_schemas(split_dir, test_tables, spider_zip):
    calibration = set(json.load(open(os.path.join(split_dir, "manifest.json")))["calibration_dbs"])
    with zipfile.ZipFile(spider_zip) as z:
        spider = {t["db_id"]: t for t in json.loads(z.read("spider_data/tables.json"))}
        questions = {q["question"].strip().lower() for name in SPIDER_SETS
                     for q in json.loads(z.read("spider_data/" + name))}
        dev_dbs = {q["db_id"] for q in json.loads(z.read("spider_data/dev.json"))}
    held = ([spider[db] for db in sorted(calibration | dev_dbs)] + json.load(open(test_tables, encoding="utf-8")))
    return [_names(meta) for meta in held], questions


def select(directory, out_dir, split_dir, test_tables, per_db=PER_DB, seed=SEED, spider_zip=SPIDER_ZIP):
    schemas = {t["db_id"]: parsed_schema(t) for t in json.load(open(os.path.join(directory, "tables.json"),
                                                                   encoding="utf-8"))}
    held, spider_questions = held_out_schemas(split_dir, test_tables, spider_zip)
    near = set()
    for db, meta in schemas.items():
        columns, tables = _names(meta)
        if any(_overlap(columns, c) >= SIMILAR or _overlap(tables, t) >= SIMILAR for c, t in held):
            near.add(db)
    by_db, counts = {}, {"samples": 0, "knowledge": 0, "style": 0, "near": 0, "spider": 0, "shape": 0, "kept": 0}
    for line in open(os.path.join(directory, "compact.jsonl"), encoding="utf-8"):
        sample = json.loads(line)
        counts["samples"] += 1
        if sample["knowledge"]:
            counts["knowledge"] += 1
        elif sample["style"] not in STYLES:
            counts["style"] += 1
        elif sample["db_id"] in near:
            counts["near"] += 1
        elif sample["question"].strip().lower() in spider_questions:
            counts["spider"] += 1
        elif UNREADABLE.search(sample["query"]):
            counts["shape"] += 1
        else:
            key = hashlib.sha256(f"{seed}:{sample['index']}".encode()).hexdigest()
            by_db.setdefault(sample["db_id"], []).append((key, sample))
    chosen = []
    for db in sorted(by_db):
        chosen.extend(sample for _, sample in sorted(by_db[db])[:per_db])
    chosen.sort(key=lambda s: s["index"])
    counts["kept"] = len(chosen)
    os.makedirs(out_dir, exist_ok=True)
    questions = [{"db_id": s["db_id"], "question": s["question"], "query": re.sub(r"\s+", " ", s["query"]).strip(),
                  "synsql_index": s["index"], "style": s["style"], "complexity": s["complexity"]} for s in chosen]
    used = sorted({s["db_id"] for s in chosen})
    with open(os.path.join(out_dir, "questions.json"), "w", encoding="utf-8", newline="\n") as sink:
        json.dump(questions, sink)
    with open(os.path.join(out_dir, "tables.json"), "w", encoding="utf-8", newline="\n") as sink:
        json.dump([schemas[db] for db in used], sink)
    manifest = dict(counts, databases=len(used), near_databases=len(near), per_db=per_db, seed=seed,
                    similar=SIMILAR, styles=sorted(STYLES))
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as sink:
        json.dump(manifest, sink, indent=1)
    print(json.dumps(manifest))


def capped_readings(paths, out, per_db, seed=SEED):
    by_db, counts = {}, {"records": 0, "ok": 0}
    for path in paths:
        for line in open(path, encoding="utf-8"):
            record = json.loads(line)
            counts["records"] += 1
            if record.get("status") != "ok":
                continue
            counts["ok"] += 1
            key = hashlib.sha256(f"{seed}:readings:{record['index']}".encode()).hexdigest()
            by_db.setdefault(record["db_id"], []).append((key, record))
    kept = sorted((record for db in sorted(by_db) for _, record in sorted(by_db[db])[:per_db]),
                  key=lambda r: r["index"])
    with open(out, "w", encoding="utf-8", newline="\n") as sink:
        for record in kept:
            sink.write(json.dumps(record) + "\n")
    structures = {}
    for record in kept:
        structures[record["readings"]["structure"]] = structures.get(record["readings"]["structure"], 0) + 1
    print(json.dumps(dict(counts, kept=len(kept), databases=len(by_db), per_db=per_db, structures=structures)))


def main():
    ap = argparse.ArgumentParser(description="SynSQL-2.5M questions for the readers.")
    commands = ap.add_subparsers(dest="command", required=True)
    c = commands.add_parser("compact")
    c.add_argument("directory")
    s = commands.add_parser("select")
    s.add_argument("directory")
    s.add_argument("out_dir")
    s.add_argument("--split", required=True, help="the split's folder (manifest.json: calibration_dbs)")
    s.add_argument("--test-tables", required=True, help="Spider's test schemas (tables JSON), never their questions")
    s.add_argument("--per-db", type=int, default=PER_DB)
    s.add_argument("--seed", type=int, default=SEED)
    s.add_argument("--spider-zip", default=SPIDER_ZIP)
    r = commands.add_parser("readings")
    r.add_argument("shards", nargs="+", help="training.readers.gold synsql outputs")
    r.add_argument("--out", required=True)
    r.add_argument("--per-db", type=int, default=6)
    r.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()
    if args.command == "compact":
        compact(args.directory)
    elif args.command == "readings":
        capped_readings(args.shards, args.out, args.per_db, args.seed)
    else:
        select(args.directory, args.out_dir, args.split, args.test_tables, args.per_db, args.seed, args.spider_zip)


if __name__ == "__main__":
    main()
