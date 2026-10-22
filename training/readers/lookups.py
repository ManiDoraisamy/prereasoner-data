"""Training questions over lookup tables: a training database's categorical text column turned into codes, with a
reference table that names each code, the shape enrichment gives a sheet (a country code beside iana_country).

The readers answered "List the full country name for each customers" with the code and the name: no reading in their
pool showed the name alone (2026-10-22), since their training data holds few tables that only name another table's
codes. For every training database (never a calibration or test one) the column chosen is a text column of one table
whose 2 to 30 distinct values are words (not keys), the table read as the evaluator reads it (capped rows). Its values
become short codes; a new table maps each code to its value; a foreign key joins them. The names of the new table and
its columns vary with the database (no fixed vocabulary), and five query shapes are built on each such database:

  names     the looked-up name of every row
  labelled  a label column of the table beside the looked-up name
  counted   the number of rows for each looked-up name
  filtered  the label of the rows whose looked-up name is one value
  total     the number of rows whose looked-up name is one value

Gemini (engine/llm.py, temperature 0, seed 0) writes two questions for each query from the schema, the query and its
first rows; a question is kept only if it states the query's value verbatim. Each keeps the query's gold readings
(engine.readers.readings.readings_of), which the training examples then read like any others.

Outputs in <out dir>: dbs/<db>.sqlite, tables.json (Spider's schema format), questions.json (source "lookup") and
readings_lookup.jsonl, for training.readers.examples --lookups <out dir>.

usage: python -m training.readers.lookups <split dir> <out dir> [--bird <BIRD train dir>] [--workers 8] [--limit N]
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from engine.readers.readings import readings_of
from regress.sql_import import import_sql
from spider.probe.evalutil import load_capped
from training.readers.sources import CAP, Sources, add_arguments, capped_graph

TABLE_NAMES = ("{c}_codes", "{c}_lookup", "{c}_reference", "ref_{c}", "{c}_list")
CODE_NAMES = ("code", "{c}_code", "key", "abbreviation", "id")
NAME_NAMES = ("name", "{c}_name", "label", "description", "full_name")
SYSTEM = ("You write the questions people type to a spreadsheet assistant. Given a database schema, a query and its "
          "first result rows, write questions whose exact answer is that result.")
SCHEMA = {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
          "required": ["questions"]}


def _pick(options, key, column):
    digest = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
    return options[digest % len(options)].format(c=column.lower())


def _codes(values):
    """{value: short upper-case code}, unique, from the value's words (initials) or its first letters."""
    codes, used = {}, set()
    for value in sorted(values):
        parts = re.findall(r"[A-Za-z0-9]+", value)
        base = ("".join(p[0] for p in parts[:3]) if len(parts) > 1 else (parts[0][:3] if parts else "X")).upper()
        code, n = base, 2
        while code in used:
            code, n = f"{base}{n}", n + 1
        used.add(code)
        codes[value] = code
    return codes


def _column(meta, capped):
    """(table, column) of the first text column that names a few categories, or None."""
    names = meta["table_names_original"]
    keyed = {index for pair in meta.get("foreign_keys", []) for index in pair} | set(
        index for key in meta.get("primary_keys", []) for index in (key if isinstance(key, list) else [key]))
    for index, (table_index, column) in enumerate(meta["column_names_original"]):
        if table_index < 0 or index in keyed or re.search(r"(^|_)(id|code|key)$", column.lower()):
            continue
        table = capped.get(names[table_index].lower())
        if not table or len(table["rows"]) < 3 or column not in table["columns"]:
            continue
        position = table["columns"].index(column)
        values = [row[position] for row in table["rows"]]
        if any(value is not None and not isinstance(value, str) for value in values):
            continue
        distinct = {value for value in values if value}
        if 2 <= len(distinct) <= 30 and all(re.search(r"[A-Za-z]{3}", v) for v in distinct) \
                and sum(len(v) for v in distinct) / len(distinct) >= 4:
            return table["name"], column
    return None


def build_database(db_id, meta, path, out_dir):
    """(new db id, its schema metadata, the lookup's (table, column, lookup, code, name)), or None."""
    capped = load_capped(path, cap=CAP)
    found = _column(meta, capped)
    if found is None:
        return None
    table, column = found
    lookup = _pick(TABLE_NAMES, db_id + "t", column)
    code, name = _pick(CODE_NAMES, db_id + "c", column), _pick(NAME_NAMES, db_id + "n", column)
    if code == name:
        name = "name" if code != "name" else "label"
    if lookup.lower() in capped:
        return None
    source = capped[table.lower()]
    position = source["columns"].index(column)
    codes = _codes({row[position] for row in source["rows"] if row[position]})
    new_id = f"{db_id}__lookup"
    os.makedirs(os.path.join(out_dir, "dbs"), exist_ok=True)
    target = os.path.join(out_dir, "dbs", new_id + ".sqlite")
    if os.path.exists(target):
        os.remove(target)
    connection = sqlite3.connect(target)
    for entry in capped.values():
        rows = entry["rows"]
        if entry is source:
            rows = [row[:position] + [codes.get(row[position], row[position])] + row[position + 1:] for row in rows]
        connection.execute(f'CREATE TABLE "{entry["name"]}" ({", ".join(chr(34) + c + chr(34) for c in entry["columns"])})')
        connection.executemany(f'INSERT INTO "{entry["name"]}" VALUES ({", ".join("?" for _ in entry["columns"])})', rows)
    connection.execute(f'CREATE TABLE "{lookup}" ("{code}", "{name}")')
    connection.executemany(f'INSERT INTO "{lookup}" VALUES (?, ?)', sorted((c, v) for v, c in codes.items()))
    connection.commit()
    connection.close()
    tables = list(meta["table_names_original"]) + [lookup]
    columns = list(meta["column_names_original"]) + [[len(tables) - 1, code], [len(tables) - 1, name]]
    types = list(meta["column_types"]) + ["text", "text"]
    source_index = next(i for i, (t, c) in enumerate(columns) if t >= 0 and tables[t] == table and c == column)
    new_meta = {"db_id": new_id, "table_names_original": tables, "table_names": [t.lower() for t in tables],
                "column_names_original": columns, "column_names": [[t, c.lower()] for t, c in columns],
                "column_types": types, "primary_keys": list(meta.get("primary_keys", [])) + [len(columns) - 2],
                "foreign_keys": list(meta.get("foreign_keys", [])) + [[source_index, len(columns) - 2]]}
    return new_id, new_meta, (table, column, lookup, code, name, source)


def queries(table, column, lookup, code, name, source):
    """(shape, SQL) of the five query shapes over the new lookup."""
    join = f'FROM "{table}" JOIN "{lookup}" ON "{table}"."{column}" = "{lookup}"."{code}"'
    labels = [c for c in source["columns"] if c != column and any(isinstance(r[source["columns"].index(c)], str)
                                                                  for r in source["rows"])]
    label = next((c for c in labels if re.search(r"name|title", c, re.I)), labels[0] if labels else source["columns"][0])
    position = source["columns"].index(column)
    counts = {}
    for row in source["rows"]:
        if row[position]:
            counts[row[position]] = counts.get(row[position], 0) + 1
    common = max(sorted(counts), key=lambda v: counts[v]) if counts else None
    value = (common or "").replace("'", "''")
    shapes = [("names", f'SELECT "{lookup}"."{name}" {join}'),
              ("labelled", f'SELECT "{table}"."{label}", "{lookup}"."{name}" {join}'),
              ("counted", f'SELECT "{lookup}"."{name}", COUNT(*) {join} GROUP BY "{lookup}"."{name}"')]
    if common:
        shapes += [("filtered", f'SELECT "{table}"."{label}" {join} WHERE "{lookup}"."{name}" = \'{value}\''),
                   ("total", f'SELECT COUNT(*) {join} WHERE "{lookup}"."{name}" = \'{value}\'')]
    return shapes, common


def _schema_text(meta, connection):
    lines = []
    for index, table in enumerate(meta["table_names_original"]):
        columns = [c for t, c in meta["column_names_original"] if t == index]
        try:
            sample = connection.execute(f'SELECT * FROM "{table}" LIMIT 2').fetchall()
        except sqlite3.Error:                      # a table the capped read skips (sqlite_sequence)
            continue
        lines.append(f"- {table}({', '.join(columns)}) e.g. {sample[:2]}")
    return "\n".join(lines)


def ask(job):
    from engine import llm

    prompt = (f"Schema with sample rows:\n{job['schema']}\n\nQuery:\n{job['sql']}\n\nFirst result rows: {job['rows']}\n\n"
              "Write two different questions a person would type to get exactly this result, in everyday words; do "
              "not copy the query's table or column names unless a person would say them. Keep any quoted value "
              "word for word.")
    for _ in range(2):
        try:
            reply = json.loads(llm.generate_text(system=SYSTEM, prompt=prompt, max_output_tokens=300,
                                                 json_schema=SCHEMA, timeout_seconds=60.0, thinking="LOW"))
            return {**job, "questions": [q.strip() for q in reply.get("questions", []) if q.strip()][:2]}
        except Exception:                          # noqa: BLE001 - one retry, then the query is dropped
            continue
    return {**job, "questions": []}


def main():
    ap = argparse.ArgumentParser(description="Training questions over lookup tables built on training databases.")
    ap.add_argument("split_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--bird", help="BIRD's train directory")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, help="at most this many databases (a trial)")
    add_arguments(ap)
    args = ap.parse_args()
    calibration = set(json.load(open(os.path.join(args.split_dir, "manifest.json")))["calibration_dbs"])
    sources = Sources.from_args(args)
    if args.bird:
        sources.add_bird(args.bird)
    training = sorted({q["db_id"] for name in ("train_spider", "train_others", "bird_train")
                       for q in sources.questions.get(name, [])} - calibration)
    training = training[:args.limit] if args.limit else training
    os.makedirs(args.out_dir, exist_ok=True)
    metas, jobs = {}, []
    for db_id in training:
        try:
            built = build_database(db_id, sources.tables[db_id], sources.db_path(db_id), args.out_dir)
        except (FileNotFoundError, sqlite3.Error, KeyError, ValueError, StopIteration):
            continue
        if built is None:
            continue
        new_id, meta, (table, column, lookup, code, name, source) = built
        metas[new_id] = meta
        path = os.path.join(args.out_dir, "dbs", new_id + ".sqlite")
        graph = capped_graph(path, meta)
        connection = sqlite3.connect(path)
        schema = _schema_text(meta, connection)
        shapes, common = queries(table, column, lookup, code, name, source)
        for shape, sql in shapes:
            try:
                rows = connection.execute(sql).fetchall()
                readings = readings_of(import_sql(sql, graph))
            except Exception:                      # noqa: BLE001 - a shape this database cannot hold
                continue
            if rows:
                jobs.append({"db_id": new_id, "shape": shape, "sql": sql, "rows": rows[:3], "schema": schema,
                             "value": common if shape in ("filtered", "total") else None, "readings": readings})
        connection.close()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        answered = list(pool.map(ask, jobs))
    questions, kept = [], 0
    with open(os.path.join(args.out_dir, "readings_lookup.jsonl"), "w", encoding="utf-8", newline="\n") as sink:
        for job in answered:
            for question in job["questions"]:
                if job["value"] and job["value"].lower() not in question.lower():
                    continue
                sink.write(json.dumps({"source": "lookup", "index": len(questions), "db_id": job["db_id"],
                                       "status": "ok", "readings": job["readings"]}) + "\n")
                questions.append({"db_id": job["db_id"], "question": question, "query": job["sql"],
                                  "shape": job["shape"]})
                kept += 1
    json.dump(list(metas.values()), open(os.path.join(args.out_dir, "tables.json"), "w", encoding="utf-8"))
    json.dump(questions, open(os.path.join(args.out_dir, "questions.json"), "w", encoding="utf-8"), indent=0)
    print(f"{len(metas)} lookup databases, {len(jobs)} queries, {kept} questions -> {args.out_dir}")


if __name__ == "__main__":
    main()
