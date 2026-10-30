"""Where the readers' training steps find questions, schemas and databases: the one place that knows.

- Spider: questions and schemas from the official spider_data.zip (train_spider, train_others, dev, tables.json);
  databases from the checkout's spider/data/dbs and the train_others databases the evaluator does not keep
  (spider_official/train_dbs_extra). Override with --spider-zip and --db-dir.
- BIRD's training set (``add_bird``): train.json, train_tables.json, train_databases/<db>/<db>.sqlite. Its
  databases are disjoint from Spider's.
- Spider-Syn (``add_syn``): train_spider.json only, Spider's questions reworded (SpiderSynQuestion) over
  Spider's databases.
- SynSQL-2.5M (``add_synsql``): the questions training.readers.synsql chose (questions.json, tables.json) over the
  dataset's synthetic databases (databases/<db>/<db>.sqlite). Its databases are disjoint from Spider's and BIRD's.

Every database is read as the evaluator reads it: at most 5,000 rows per table (spider.probe.evalutil.load_capped),
its foreign keys from the dataset's schema file.
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

from engine.sql_schema import SchemaGraph
from spider.probe.evalutil import build_mem_db, load_capped
from spider.probe.spider_eval import spider_foreign_keys
from training.readers.provenance import SPIDER_TRAIN_SETS

ROOT = Path(__file__).resolve().parents[2]
SPIDER_ZIP = r"C:\work\datasets\spider_official\spider_data.zip"
SPIDER_DB_DIRS = (str(ROOT / "spider" / "data" / "dbs"), r"C:\work\datasets\spider_official\train_dbs_extra")
SPIDER_SETS = tuple(f"{name}.json" for name in SPIDER_TRAIN_SETS)
TOKENIZER = r"C:\work\datasets\models\xlm-roberta-base"   # XLM-R base and large share one tokenizer
CAP = 5000


def add_arguments(parser):
    """The --spider-zip and --db-dir options every step that reads Spider takes."""
    parser.add_argument("--spider-zip", default=SPIDER_ZIP, help=f"Spider's spider_data.zip (default {SPIDER_ZIP})")
    parser.add_argument("--db-dir", action="append", dest="db_dirs", default=None,
                        help="a folder of <db_id>.sqlite files, repeatable (default: " + ", ".join(SPIDER_DB_DIRS) + ")")


def capped_graph(path, meta):
    """The schema graph of a database read with at most CAP rows per table."""
    capped = load_capped(path, cap=CAP)
    return SchemaGraph.from_tables(list(capped.values()), spider_foreign_keys(meta))


def capped_database(path, meta):
    """(in-memory connection, schema graph) of a database read with at most CAP rows per table."""
    capped = load_capped(path, cap=CAP)
    return (build_mem_db(list(capped.values())),
            SchemaGraph.from_tables(list(capped.values()), spider_foreign_keys(meta)))


class Sources:
    """Questions by source ("train_spider", "train_others", and "bird_train", "syn_train" when
    added), schema metadata by database, and the folders the databases are in."""

    def __init__(self, spider_zip=SPIDER_ZIP, db_dirs=None):
        with zipfile.ZipFile(spider_zip) as z:
            self.questions = {name.split(".")[0]: json.loads(z.read("spider_data/" + name)) for name in SPIDER_SETS}
            self.tables = {t["db_id"]: t for t in json.loads(z.read("spider_data/tables.json"))}
        self.db_dirs = list(db_dirs or SPIDER_DB_DIRS)
        self.db_files = {}

    @classmethod
    def from_args(cls, args):
        return cls(args.spider_zip, args.db_dirs)

    def add_syn(self, directory):
        """Spider-Syn: Spider's training questions reworded with synonyms (source "syn_train"); the databases are
        Spider's."""
        self.questions["syn_train"] = [dict(q, question=q["SpiderSynQuestion"]) for q in
                                       json.load(open(os.path.join(directory, "train_spider.json"), encoding="utf-8"))]

    def add_reworded(self, paths, readings_paths, out_dir):
        """Training questions reworded by training.readers.rewordings (source "reworded"), each on its original's
        database: one or more sets, concatenated in order and re-indexed from 0. Their readings, re-indexed the same
        way, are written to <out_dir>/readings_reworded.jsonl, whose path is returned."""
        rows, readings = [], []
        for path, readings_path in zip(paths, readings_paths, strict=True):
            offset = len(rows)
            rows.extend(dict(json.loads(line), index=offset + i) for i, line in enumerate(open(path, encoding="utf-8")))
            for line in open(readings_path, encoding="utf-8"):
                record = json.loads(line)
                readings.append(dict(record, index=offset + record["index"]))
        self.questions["reworded"] = rows
        os.makedirs(out_dir, exist_ok=True)
        merged = os.path.join(out_dir, "readings_reworded.jsonl")
        with open(merged, "w", encoding="utf-8", newline="\n") as sink:
            sink.writelines(json.dumps(record) + "\n" for record in readings)
        return merged

    def add_lookups(self, directory):
        """Training questions over lookup tables (training.readers.lookups, source "lookup"): their schemas and
        their databases."""
        self.tables.update({t["db_id"]: t for t in json.load(open(os.path.join(directory, "tables.json"),
                                                                  encoding="utf-8"))})
        self.questions["lookup"] = json.load(open(os.path.join(directory, "questions.json"), encoding="utf-8"))
        self.db_dirs.append(os.path.join(directory, "dbs"))

    def add_structures(self, directories, out_dir):
        """Training questions written for queries built in the structures the readers misread
        (training.readers.structures, source "structure"), each on a training database already in the sources: one
        or more sets, concatenated in order and re-indexed from 0. Their readings, re-indexed the same way, are
        written to <out_dir>/readings_structure.jsonl, whose path is returned."""
        rows, readings = [], []
        for directory in directories:
            offset = len(rows)
            rows.extend(json.load(open(os.path.join(directory, "questions.json"), encoding="utf-8")))
            for line in open(os.path.join(directory, "readings_structure.jsonl"), encoding="utf-8"):
                record = json.loads(line)
                readings.append(dict(record, index=offset + record["index"]))
        self.questions["structure"] = rows
        os.makedirs(out_dir, exist_ok=True)
        merged = os.path.join(out_dir, "readings_structure.jsonl")
        with open(merged, "w", encoding="utf-8", newline="\n") as sink:
            sink.writelines(json.dumps(record) + "\n" for record in readings)
        return merged

    def add_bird(self, directory):
        """BIRD's training set: its questions (source "bird_train"), schemas and databases."""
        tables = {t["db_id"]: t for t in json.load(open(os.path.join(directory, "train_tables.json"),
                                                        encoding="utf-8"))}
        assert not set(tables) & set(self.tables), "BIRD and Spider database names overlap"
        self.tables.update(tables)
        self.questions["bird_train"] = json.load(open(os.path.join(directory, "train.json"), encoding="utf-8"))
        self.db_dirs.extend(os.path.join(directory, "train_databases", db) for db in sorted(tables))

    def add_synsql(self, selection, databases):
        """SynSQL-2.5M's chosen questions (source "synsql") and schemas from training.readers.synsql's ``selection``
        folder; each database is ``databases``/<db>/<db>.sqlite."""
        tables = {t["db_id"]: t for t in json.load(open(os.path.join(selection, "tables.json"), encoding="utf-8"))}
        assert not set(tables) & set(self.tables), "SynSQL's database names overlap the sources'"
        self.tables.update(tables)
        self.questions["synsql"] = json.load(open(os.path.join(selection, "questions.json"), encoding="utf-8"))
        self.db_files.update({db: os.path.join(databases, db, db + ".sqlite") for db in tables})

    def db_path(self, db_id):
        if db_id in self.db_files:
            return self.db_files[db_id]
        for folder in self.db_dirs:
            path = os.path.join(folder, db_id + ".sqlite")
            if os.path.exists(path):
                return path
        raise FileNotFoundError(db_id)

    def graph(self, db_id):
        return capped_graph(self.db_path(db_id), self.tables[db_id])

    def database(self, db_id):
        return capped_database(self.db_path(db_id), self.tables[db_id])
