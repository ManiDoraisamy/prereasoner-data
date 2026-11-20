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
    parser.add_argument("--source-databases", help="Hashed manifest of explicitly recovered source database versions")
    parser.add_argument("--column-dictionaries", help="Verified source-column dictionary manifest; no per-question hints")


def unit_graph_pairs(rows):
    """Recover adjacent question/program annotations from the original Qwen corpus.

    SQL is stored as typed token units in a companion role record, not a query
    string field. Preserve it as tokens: identifiers/strings may be unquoted and
    filter literal types are absent. Pairing/link validation does not certify SQL
    execution or the question's meaning, or admit a new source to gradient training.
    """
    previous = None
    for line, row in enumerate(rows, 1):
        if row.get("task") == "intent":
            if previous is not None:
                raise ValueError("unpaired intent record in Qwen graph corpus")
            previous = (line, row)
            continue
        if row.get("task") != "role" or previous is None:
            raise ValueError("Qwen graph roles must immediately follow their intent record")
        first_line, intent = previous
        metadata = {k: v for k, v in intent.items() if k not in ("task", "units")}
        if metadata != {k: v for k, v in row.items() if k not in ("task", "units")}:
            raise ValueError("Qwen graph companion metadata differs")
        schema = [u for u in intent["units"] if u.get("group") == "schema"]
        if not schema or schema != [u for u in row["units"] if u.get("group") == "schema"]:
            raise ValueError("Qwen graph companions have different or empty schemas")
        lookup = {}
        for unit in schema:
            key = (unit["table"], unit.get("colname", unit["text"])) if unit.get("table") else unit["col"]
            if key in lookup:
                raise ValueError("duplicate Qwen schema pointer")
            lookup[key] = unit
        tokens = [u for u in row["units"] if u.get("group") == "sql"]
        question = [u for u in intent["units"] if u.get("group") == "q"]
        if not tokens or not question or any(not isinstance(u.get("text"), str) or not u["text"]
                                             for u in [*question, *tokens]):
            raise ValueError("Qwen graph pair needs nonempty question and program tokens")
        offsets, cursor, links = [], 0, []
        for i, unit in enumerate(question):
            offsets.append([cursor, cursor + len(unit["text"])])
            cursor += len(unit["text"]) + 1
            link = unit.get("link")
            if link is None:
                continue
            key = tuple(link) if isinstance(link, list) else link
            if key not in lookup:
                raise ValueError("Qwen question link has no schema column")
            column = lookup[key]
            occurrences = []
            for j, token in enumerate(tokens):
                target = ((token.get("table"), token.get("colname")) if isinstance(key, tuple)
                          else token.get("col"))
                if target != key or token["kind"] not in ("field", "literal"):
                    continue
                # Source literal identity needs exact spelling. A field link
                # supplies column alignment even under a different noun phrase.
                if token["kind"] == "literal" and token["text"] != unit["text"]:
                    continue
                occurrences.append({"sql_token": j, "kind": token["kind"], "clause": token["clause"]})
            links.append({"question_token": i, "span": offsets[-1], "source_pointer": link,
                          "column": {"table": column.get("table"), "name": column.get("colname", column["text"]),
                                     "index": column["col"]},
                          "sql_occurrences": occurrences,
                          "literal_type": "unknown"})
        yield {"source_lines": [first_line, line], "source_metadata": metadata, "schema_units": schema,
               "question": " ".join(u["text"] for u in question), "question_units": question,
               "question_spans": offsets, "sql_units": tokens, "alignments": links,
               "admitted_for_training": False,
               "scope": "Source-token recovery and physical alignment only; SQL literal types and execution unverified."}
        previous = None
    if previous is not None:
        raise ValueError("Qwen graph corpus ends with an unpaired intent record")


def match_unit_source(pair, original):
    """Verify the original quoted SQL against every recovered question/SQL unit.

    The graph builder stripped quoting and literal types. Its source record can
    restore them only after metadata, question annotations, token order and
    physical column pointers agree. This is not English-meaning certification.
    """
    from sqlglot.expressions import Identifier

    metadata = pair["source_metadata"]
    if any(original.get(k) != v for k, v in metadata.items()):
        raise ValueError("Qwen original source metadata differs")
    if original.get("question") != pair["question"]:
        raise ValueError("Qwen original question differs")
    questions = original.get("q_units", [])
    tokens = original.get("sql_units", [])
    if len(questions) != len(pair["question_units"]) or len(tokens) != len(pair["sql_units"]):
        raise ValueError("Qwen original token count differs")
    for old, new in zip(questions, pair["question_units"], strict=True):
        if (old.get("text") != new["text"] or old.get("link") != new.get("link") or
                ["intent_" + x for x in old.get("intent", [])] != new.get("fired", [])):
            raise ValueError("Qwen original question annotation differs")
    qualified = all(u.get("table") for u in pair["schema_units"])
    pointers = {(u.get("table"), u.get("colname")): u["col"] for u in pair["schema_units"]}
    for old, new in zip(tokens, pair["sql_units"], strict=True):
        kind = old.get("srole")
        text = str(old["raw"]) if kind in ("field", "table", "literal") and old.get("raw") is not None else old["text"]
        column = pointers.get((old.get("table"), old.get("col")), -1) if qualified else old.get("col")
        column = -1 if column is None else column
        if (kind != new["kind"] or old.get("clause") != new["clause"] or text != new["text"] or
                column != new.get("col", -1) or qualified and
                (old.get("table") != new.get("table") or old.get("col") != new.get("colname"))):
            raise ValueError("Qwen original SQL annotation differs")
        if kind in ("field", "table") and old["text"] != Identifier(this=text, quoted=True).sql(dialect="sqlite"):
            raise ValueError("Qwen original quoted identifier differs")
    if original.get("sql") != " ".join(u["text"] for u in tokens):
        raise ValueError("Qwen original SQL differs from its token stream")
    return tokens


def capped_graph(path, meta):
    """The schema graph of a database read with at most CAP rows per table."""
    capped = load_capped(path, cap=CAP)
    return _capped_graph(path, meta, capped)


def _capped_graph(path, meta, capped):
    """Keep verified numeric SQL declarations when cell inference is insufficient.

    Empty/all-NULL columns cannot be typed from observations. Native SQL numbers
    written in scientific notation must also not become text. Nonempty mixed
    columns retain the existing conservative inference; this never coerces an
    invalid text cell into a numeric value or treats a missing fact as evidence.
    """
    from dataclasses import replace
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    import math
    from numbers import Real
    from engine.sql_ast import SQLType
    from engine.numeric import observed_numeric_affinity

    graph = SchemaGraph.from_tables(list(capped.values()), spider_foreign_keys(meta))
    def native_numeric(column):
        filled = [v for v in column.values if v is not None and str(v).strip()]
        return (bool(filled) and all(isinstance(v, Real) and not isinstance(v, bool) and math.isfinite(v)
                                    for v in filled) and observed_numeric_affinity(filled) != "TEXT")
    declarations = {}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        for table, columns in graph.by_table.items():
            if not any((col.ref.type is SQLType.TEXT and native_numeric(col)) or
                       not any(value is not None and str(value).strip() for value in col.values)
                       for col in columns):
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            for row in connection.execute("PRAGMA table_info(" + quoted + ")"):
                declared = str(row[2]).upper()
                dtype = (SQLType.INTEGER if "INT" in declared else SQLType.REAL
                         if any(token in declared for token in ("REAL", "FLOA", "DOUB", "NUM", "DEC")) else None)
                if dtype is not None:
                    declarations[(table, row[1])] = dtype
    refs = {}
    columns = []
    for column in graph.columns:
        key = (column.ref.table, column.ref.name)
        empty = not any(value is not None and str(value).strip() for value in column.values)
        ref = (replace(column.ref, type=declarations[key])
               if key in declarations and (empty or column.ref.type is SQLType.TEXT and native_numeric(column))
               else column.ref)
        refs[key] = ref
        columns.append(replace(column, ref=ref))
    def mapped(ref):
        return refs[(ref.table, ref.name)]
    foreign = [replace(fk, from_column=mapped(fk.from_column), to_column=mapped(fk.to_column),
                       additional_columns=tuple((mapped(a), mapped(b)) for a, b in fk.additional_columns))
               for fk in graph.foreign_keys]
    return SchemaGraph(columns, foreign)


def capped_database(path, meta):
    """(in-memory connection, schema graph) of a database read with at most CAP rows per table."""
    capped = load_capped(path, cap=CAP)
    return (build_mem_db(list(capped.values())),
            _capped_graph(path, meta, capped))


class Sources:
    """Questions by source ("train_spider", "train_others", and "bird_train", "syn_train" when
    added), schema metadata by database, and the folders the databases are in."""

    def __init__(self, spider_zip=SPIDER_ZIP, db_dirs=None):
        with zipfile.ZipFile(spider_zip) as z:
            self.questions = {name.split(".")[0]: json.loads(z.read("spider_data/" + name)) for name in SPIDER_SETS}
            self.tables = {t["db_id"]: t for t in json.loads(z.read("spider_data/tables.json"))}
        self.db_dirs = list(db_dirs or SPIDER_DB_DIRS)
        self.db_files = {}
        self.column_annotations = {}

    @classmethod
    def from_args(cls, args):
        sources = cls(args.spider_zip, args.db_dirs)
        if getattr(args, "source_databases", None):
            sources.add_database_versions(args.source_databases)
        if getattr(args, "column_dictionaries", None):
            sources.add_column_dictionaries(args.column_dictionaries)
        return sources

    def add_column_dictionaries(self, manifest):
        """Attach publisher column facts only after their original hashes match.

        The schema may be registered later by add_bird; graph() binds the facts
        to exact physical identities. File paths/provenance never enter input.
        """
        import hashlib

        selected, verified = {}, {}
        fields = {"column_name", "column_description", "data_format", "value_description"}
        for record in json.loads(Path(manifest).read_text(encoding="utf-8")):
            path = record["source_path"]
            if path not in verified:
                with Path(path).open("rb") as stream:
                    verified[path] = hashlib.file_digest(stream, "sha256").hexdigest()
            if verified[path] != record["source_sha256"]:
                raise ValueError("source column dictionary identity changed: " + path)
            annotations = record["annotations"]
            if (not isinstance(annotations, dict) or set(annotations) - fields
                    or any(not isinstance(value, str) for value in annotations.values())):
                raise ValueError("unsupported source column dictionary field")
            key = (record["db_id"], record["table"], record["column"])
            if key in selected:
                raise ValueError("duplicate source column dictionary binding")
            selected[key] = tuple(sorted(annotations.items()))
        self.column_annotations = selected

    def add_database_versions(self, manifest):
        """Use an explicit source revision, never disguise it as an index copy.

        Both the recovered database and the immutable upstream input are pinned.
        Validate the complete manifest before changing any selected source path.
        """
        import hashlib

        verified = {}
        def verify(path, expected):
            path = Path(path).resolve()
            if path not in verified:
                with path.open("rb") as stream:
                    verified[path] = hashlib.file_digest(stream, "sha256").hexdigest()
            actual = verified[path]
            if actual != expected:
                raise ValueError("recovered source database identity changed: " + str(path))

        records = json.loads(Path(manifest).read_text(encoding="utf-8"))
        selected = {}
        for db, record in records.items():
            if db not in self.tables:
                raise ValueError("recovered database has no registered schema: " + db)
            verify(record["source_path"], record["source_sha256"])
            verify(record["path"], record["sha256"])
            selected[db] = record["path"]
        self.db_files.update(selected)

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

    def primary_keys(self, db_id):
        """Declared, non-null source keys for offline SQL grouping normalization.

        SQLite permits NULL in some declared primary keys. Such a key cannot
        prove that a group determines the rest of a source row, so exclude it.
        These facts never depend on a training question or its target.
        """
        import sqlite3
        from contextlib import closing

        keys = {}
        def quote(value):
            return '"' + value.replace('"', '""') + '"'
        with closing(sqlite3.connect(Path(self.db_path(db_id)).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            for table in self.tables[db_id]["table_names_original"]:
                fields = connection.execute("PRAGMA table_info(" + quote(table) + ")").fetchall()
                primary = tuple(name for _, name in sorted((field[5], field[1]) for field in fields if field[5]))
                if primary and connection.execute("SELECT 1 FROM " + quote(table) + " WHERE " +
                        " OR ".join(quote(name) + " IS NULL" for name in primary) + " LIMIT 1").fetchone() is None:
                    keys[table] = primary
        return keys

    def graph(self, db_id):
        from dataclasses import replace

        graph = capped_graph(self.db_path(db_id), self.tables[db_id])
        annotations = getattr(self, "column_annotations", {})
        supplied = {key[1:] for key in annotations if key[0] == db_id}
        if supplied - set(graph.column_map):
            raise ValueError("source dictionary refers to a missing physical column")
        if not supplied:
            return graph
        return SchemaGraph((replace(column, annotations=annotations.get(
            (db_id, column.ref.table, column.ref.name), ())) for column in graph.columns),
            graph.foreign_keys, value_index=graph.value_index)

    def question_graph(self, db_id, turns, *, seconds=10, max_columns=64, graph=None):
        """Add bounded full-source rows matching names in the input.

        This retrieves from input text/schema only; no target SQL or literal is
        accepted. Optional reads commit all their rows or none. A resource limit
        returns the original bank with an unavailable receipt; the grammar still
        rejects any target whose required value is missing from that bank.
        Serving's graph already indexes its actual supplied table cells.
        """
        from contextlib import closing
        from dataclasses import replace
        from pathlib import Path
        import re
        import sqlite3
        import time
        from engine.sql_ast import SQLType
        from collections import Counter
        from decimal import Decimal
        from engine.numeric import canonical_decimal

        graph = graph if graph is not None else self.graph(db_id)
        text = "\n".join(turn["content"] for turn in turns)
        phrases = re.findall(r'"([^"]{3,512})"', text)
        phrases += re.findall(r"(?<!\w)'([^\n]{3,512}?)'(?!\w)", text)
        phrases += re.findall(r"‘([^’\n]{3,512})’|“([^”\n]{3,512})”", text)
        phrases = [phrase.strip() if isinstance(phrase, str) else next(part for part in phrase if part)
                   for phrase in phrases]
        capital = r"[A-Z][\w'’.-]*"
        particle = r"(?:of|the|and|de|la|van|von)"
        proper_names = re.findall(
            r"\b(?:\d+\s+)?" + capital + r"(?:\s+(?:" + particle + r"\s+)?" + capital + r")+", text)
        phrases.extend(sorted(set(proper_names), key=lambda value: (-len(value), value))[:4])
        words = set(re.findall(r"[^\W_]+", text.casefold()))
        # Related low-cardinality answers/codes can occur beyond the row cap.
        # The relationship is an input-derived proposal, not a new planner FK.
        profiles = []
        small = {table for table, columns in graph.by_table.items()
                 if 0 < max((len(column.values) for column in columns), default=0) <= 128
                 and any(column.ref.type is SQLType.TEXT for column in columns)}
        if small:
            seen_links = set()
            for link in (*graph.foreign_keys, *graph.discovered_foreign_keys):
                child, parent = link.from_column, link.to_column
                identity = (child.table, child.name, parent.table, parent.name)
                if link not in graph.foreign_keys and child.name.casefold() != parent.name.casefold():
                    continue
                if parent.table not in small or link.additional_columns or link.confidence < 1 or identity in seen_links:
                    continue
                seen_links.add(identity)
                columns = graph.by_table[parent.table]
                labels = [frozenset(word for column in columns if column.ref.type is SQLType.TEXT
                           for word in re.findall(r"[^\W_]+", str(column.values[row]).casefold())
                           if len(word) >= 4 or word.isdigit())
                          for row in range(min(len(column.values) for column in columns))]
                frequencies = Counter(word for tokens in labels for word in tokens)
                keys = graph.column_map[(parent.table, parent.name)].values
                ranked = sorted(((-sum(1 / frequencies[word] for word in tokens & words), row)
                                 for row, tokens in enumerate(labels) if tokens & words))
                keys = [keys[row] for score, row in ranked if score <= -0.2 and keys[row] is not None][:8]
                if not keys:
                    continue
                candidates = []
                for column in graph.by_table[child.table]:
                    strings = {value for value in column.values if isinstance(value, str)}
                    if (column.ref.type is SQLType.TEXT and 1 < len(strings) <= 512
                            and max(map(len, strings), default=0) <= 256):
                        candidates.append((len(strings), column.ref.name))
                if candidates:
                    profiles.append((child, parent, min(candidates)[1], keys))
                if len(profiles) >= 2:
                    break
        singular = {word[:-1] if word.endswith("s") else word for word in words}
        choices = []
        labels = set()
        for column in graph.columns:
            if column.ref.type is not SQLType.TEXT:
                continue
            table, name = column.ref.table, column.ref.name
            table_words = set(re.findall(r"[^\W_]+", table.casefold()))
            name_words = set(re.findall(r"[^\W_]+", name.casefold()))
            labels.update(word for word in name_words & words if len(word) >= 6)
            choices.append((not bool(name_words & (words | singular)), not bool(table_words & (words | singular)), table, name))
        phrases.extend(sorted(labels, key=lambda value: (-len(value), value))[:4])
        if not phrases and not profiles:
            return graph, []
        # Avoid a database sweep when every requested phrase is already visible.
        # This decision inspects source values and input phrases, never a target.
        visible = [value.casefold() for column in graph.columns for value in column.values if isinstance(value, str)]
        def seen(phrase):
            terms = re.findall(r"[^\W_]+", phrase.casefold())
            return any(all(term in value for term in terms) for value in visible)
        if not profiles and all(seen(phrase) for phrase in phrases):
            return graph, []
        selected = sorted(choices)[:max_columns]
        retrieved, evidence = {}, []
        deadline = time.monotonic() + seconds
        def quote(value):
            return '"' + value.replace('"', '""') + '"'
        with closing(sqlite3.connect(Path(self.db_path(db_id)).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            for child, parent, name, keys in profiles:
                if time.monotonic() > deadline:
                    break
                columns = graph.by_table[child.table]
                physical = {column.ref.name.casefold() for column in columns}
                rowid = next((name for name in ('_rowid_', 'rowid', 'oid') if name not in physical), None)
                definition = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (child.table,)).fetchone()
                if not rowid or not definition or 'WITHOUT ROWID' in definition[0].upper():
                    continue
                field, context = quote(name), quote(child.name)
                sql = ("SELECT * FROM " + quote(child.table) + " WHERE " + quote(rowid) + " IN (SELECT MIN(" +
                       quote(rowid) + ") FROM " + quote(child.table) + " WHERE " + context + " IN (" +
                       ','.join('?' for _ in keys) + ") AND " + field + " IS NOT NULL GROUP BY " + context + ',' +
                       field + ") ORDER BY " + context + ',' + field)
                try:
                    per_context = Counter(); sampled = []
                    position = next(i for i, column in enumerate(columns) if column.ref.name == child.name)
                    bound_keys = [canonical_decimal(key) if isinstance(key, Decimal) else key for key in keys]
                    for row in connection.execute(sql, bound_keys):
                        if time.monotonic() > deadline:
                            break
                        key = row[position]
                        if per_context[key] < 8 and sum(map(len, retrieved.values())) < 128:
                            sampled.append(row);per_context[key] += 1
                    retrieved.setdefault(child.table, []).extend(sampled)
                    evidence.append({"table": child.table, "column": name, "reference_table": parent.table,
                                     "reference_column": parent.name, "context_column": child.name,
                                     "context_values": bound_keys, "retrieved_rows": len(sampled),
                                     "scope": "observed categorical rows by input-matched reference"})
                except sqlite3.OperationalError as exc:
                    if (getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_INTERRUPT
                            or time.monotonic() <= deadline):
                        raise
                    evidence.append({"state": "lookup_failed", "reason": str(exc)})
            for phrase in dict.fromkeys(phrases):
                terms = list(dict.fromkeys(word for word in re.findall(r"[^\W_]+", phrase) if len(word) >= 3))[:20]
                if not terms:
                    continue
                patterns = ["%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for word in terms]
                for _, _, table, name in selected:
                    if time.monotonic() > deadline:
                        break
                    field = quote(name)
                    sql = "SELECT DISTINCT " + field + " FROM " + quote(table) + " WHERE " + " AND ".join(
                        field + " LIKE ? ESCAPE '\\'" for _ in patterns) + " ORDER BY " + field + " LIMIT 8"
                    try:
                        values = [r[0] for r in connection.execute(sql, patterns)]
                        if not values:
                            continue
                        rows = []
                        order = ','.join(quote(column.ref.name) for column in graph.by_table[table])
                        for value in values:
                            rows.extend(connection.execute("SELECT * FROM " + quote(table) + " WHERE " + field +
                                                           "=? ORDER BY " + order + " LIMIT 8", (value,)).fetchall())
                        retrieved.setdefault(table, []).extend(rows)
                        evidence.append({"table": table, "column": name, "phrase": phrase, "values": values, "retrieved_rows": len(rows)})
                    except sqlite3.OperationalError as exc:
                        if (getattr(exc, 'sqlite_errorcode', None) != sqlite3.SQLITE_INTERRUPT
                                or time.monotonic() <= deadline):
                            raise
                        evidence.append({"table": table, "column": name, "state": "lookup_failed", "reason": str(exc)})
                        if time.monotonic() > deadline:
                            break
        if time.monotonic() > deadline or any(item.get('state') == 'lookup_failed' for item in evidence):
            return graph, [{"state": "unavailable", "scope": "optional_full_source_rows",
                            "reason": "input-only source retrieval did not complete within its resource limit",
                            "budget_seconds": seconds, "base_bank_unchanged": True,
                            "discarded_source_rows": sum(map(len, retrieved.values()))}]
        columns = []
        from engine.sql_schema import _coerce_value
        for column in graph.columns:
            extra = retrieved.get(column.ref.table, [])
            position = list(c.ref.name for c in graph.by_table[column.ref.table]).index(column.ref.name)
            values = tuple(_coerce_value(row[position], column.ref.type) for row in extra)
            columns.append(replace(column, values=column.values + values))
        explicit = list(graph.source_rows)
        for table, extra in retrieved.items():
            start = len(graph.by_table[table][0].values)
            explicit.extend((table, start + i) for i in range(len(extra)))
        return SchemaGraph(columns, graph.foreign_keys, source_rows=explicit), evidence

    def database(self, db_id):
        return capped_database(self.db_path(db_id), self.tables[db_id])
