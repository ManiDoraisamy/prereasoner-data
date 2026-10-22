"""Training questions written for queries built on the training databases, in three structures the readers misread:

  unmatched  the rows of a table that no row of a table referencing it matches, at most once filtered on a value of
             that table or of a table it references:
             SELECT p.label FROM p WHERE p.key NOT IN (SELECT c.ref FROM c [JOIN o ...] [WHERE x.col = 'value'])
             The readers read "List the product names that no customer from Paris has bought" without the negation
             (2026-10-22): its pool held no reading that excludes.
  ranked     the groups of a table ranked by the total (SUM) or the average (AVG) of a number of the table that
             references it, or of its own: SELECT g FROM c JOIN p ... GROUP BY g ORDER BY SUM(c.m) ASC|DESC LIMIT k.
             The readers read "top 2 product categories by total revenue" as a ranking by the number of rows. A
             query is kept only if its first k groups differ from the first k by row count, and no tie crosses the
             cut, so the right reading is the only one that answers.
  aggregate  the lowest, highest, average or total of a number of one table (MIN, MAX, AVG and SUM equally often,
             by hash), or its number of rows, at most once filtered on a category of that table:
             SELECT MAX(t.m) FROM t [WHERE t.c = 'value']. The readers read "maximum notice_days" as a total and,
             before the opposite rewordings, "minimum amount" as the highest (2026-10-22). A lowest or highest is
             kept only when the rows it reads hold more than one value.

The tables and columns are chosen from each database's foreign keys and its values: a label is a mostly distinct
text column; a category a text column of 2 to 40 repeated values; a number a numeric column of more than two values
whose name does not end in a key, year, code, phone or coordinate word (NOT_A_MEASURE), and a share (SHARE: a
percentage, rate or ratio) is averaged, never totalled. Every choice that has options is made by a hash of the
database and the query, so a rerun builds the same queries. Each database is read as the evaluator reads it (capped
rows), and every query is run on it: one that fails, is empty, or returns every row is dropped.

Gemini (engine/llm.py, the product's one client) writes two questions for each query from the schema, the query and
its first rows: one a full question, the other the few words a person types into a search box ("highest price of
toys"), which the training questions (full sentences) rarely hold. A question is kept only if it states the query's
filter value verbatim and, for a ranking of more than one group, the number of groups. Each keeps the query's gold
readings (engine.readers.readings.readings_of).
Only training databases are used: never the split's calibration or test databases.

Outputs in <out dir>: questions.json (source "structure") and readings_structure.jsonl, for
training.readers.examples --structures <out dir>.

usage: python -m training.readers.structures <split dir> <out dir> [--bird <BIRD train dir>] [--workers 8]
           [--limit N]
"""
import argparse
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from engine.readers import numerals
from engine.readers.readings import readings_of
from engine.sql_schema import SchemaGraph
from regress.sql_import import import_sql
from spider.probe.evalutil import build_mem_db, load_capped
from spider.probe.spider_eval import spider_foreign_keys
from training.readers.sources import CAP, Sources, add_arguments

KEY_NAME = re.compile(r"(^|_)(id|code|key)$", re.I)
NOT_A_MEASURE = re.compile(
    r"(^|_)(id|code|key|year|zip|phone|postcode|lat|lon|long|latitude|longitude|number|no|num)$", re.I)
SHARE = re.compile(r"percent|rate|ratio|pct", re.I)
LIMITS = (1, 2, 3, 5)
MAX_PAIRS = 12                                     # (group, number) pairings tried per joined table
BUDGET = {"unmatched": 3, "unmatched_filtered": 5, "ranked": 6, "aggregate": 4}     # queries kept per database
FUNCTIONS = ("MIN", "MAX", "AVG", "SUM")
WORDS = {"MIN": "lowest", "MAX": "highest", "AVG": "average", "SUM": "total", "COUNT": "number of rows"}
SYSTEM = ("You write the questions people type to a spreadsheet assistant. Given a database schema, a query and its "
          "first result rows, write questions whose exact answer is that result.")
SCHEMA = {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
          "required": ["questions"]}
HINTS = {
    "unmatched": "The query finds the rows of {table} that have no matching row in {other}. Ask for them the way a "
                 "person would.",
    "ranked": "The query ranks {table} by the {measure} of {column}. Name that quantity the way a person would (an "
              "everyday word for it when one fits) and say how many are wanted.",
    "aggregate": "The query computes the {measure} of {column}{filtered}.",
}


def _hash(*parts):
    return int(hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest(), 16)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


class Database:
    """One training database read as the evaluator reads it: its tables' rows, keys and foreign keys."""

    def __init__(self, db_id, meta, path):
        self.db_id, self.meta = db_id, meta
        self.capped = load_capped(path, cap=CAP)
        self.connection = build_mem_db(list(self.capped.values()))
        self.graph = SchemaGraph.from_tables(list(self.capped.values()), spider_foreign_keys(meta))
        names, columns = meta["table_names_original"], meta["column_names_original"]
        keyed = {index for pair in meta.get("foreign_keys", []) for index in pair} | {
            index for key in meta.get("primary_keys", []) for index in (key if isinstance(key, list) else [key])}
        self.keys = {(names[columns[i][0]], columns[i][1]) for i in keyed if columns[i][0] >= 0}
        self.references = []                       # (child table, child column, parent table, parent column)
        for child, parent in meta.get("foreign_keys", []):
            (ct, cc), (pt, pc) = columns[child], columns[parent]
            if ct >= 0 and pt >= 0 and ct != pt and self.table(names[ct]) and self.table(names[pt]):
                self.references.append((self.table(names[ct])["name"], cc, self.table(names[pt])["name"], pc))
        self.references = sorted(set(self.references))

    def table(self, name):
        return self.capped.get(name.lower())

    def values(self, table, column):
        entry = self.table(table)
        if entry is None or column not in entry["columns"]:
            return None
        position = entry["columns"].index(column)
        return [row[position] for row in entry["rows"]]

    def _plain(self, table, column):
        return (table, column) not in self.keys and not KEY_NAME.search(column)

    def label(self, table):
        """The table's mostly distinct text column (its rows' names), a name or title preferred, or None."""
        entry, found = self.table(table), []
        for column in entry["columns"]:
            values = [v for v in self.values(table, column) if v is not None]
            if (len(values) >= 3 and self._plain(table, column) and all(isinstance(v, str) for v in values)
                    and len(set(values)) >= 0.8 * len(values) and all(re.search(r"[A-Za-z]{2}", v) for v in values)):
                found.append(column)
        named = [c for c in found if re.search(r"name|title", c, re.I)]
        return (named or found or [None])[0]

    def categories(self, table):
        """The table's text columns of 2 to 40 repeated word values."""
        found = []
        for column in self.table(table)["columns"]:
            values = [v for v in self.values(table, column) if v]
            distinct = set(values)
            if (self._plain(table, column) and values and all(isinstance(v, str) for v in values)
                    and 2 <= len(distinct) <= 40 and len(distinct) <= 0.6 * len(values)
                    and all(re.search(r"[A-Za-z]{3}", v) for v in distinct)):
                found.append(column)
        return found

    def measures(self, table):
        """The table's numeric columns that are not keys, years or codes, of more than two distinct values."""
        found = []
        for column in self.table(table)["columns"]:
            values = [v for v in self.values(table, column) if v is not None]
            if (self._plain(table, column) and not NOT_A_MEASURE.search(column) and values
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)
                    and len(set(values)) > 2):
                found.append(column)
        return found

    def run(self, sql):
        return self.connection.execute(sql).fetchall()


def _common(values):
    """The values of a column, most frequent first (ties by value)."""
    counts = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return sorted(counts, key=lambda v: (-counts[v], v))


def unmatched(db):
    """Candidate unmatched queries of a database (dicts of shape, sql, value, hint, table, alternatives); the
    candidates that share ``alternatives`` differ only in the filter's value, and the first that runs well is kept."""
    built = []
    for child, ref, parent, key in db.references:
        label = db.label(parent)
        if label is None:
            continue
        head = (f"SELECT {_quote(parent)}.{_quote(label)} FROM {_quote(parent)} "
                f"WHERE {_quote(parent)}.{_quote(key)} NOT IN ")
        order = f" ORDER BY {_quote(parent)}.{_quote(label)}" if _hash(db.db_id, child, parent, "order") % 2 else ""
        common = {"hint": HINTS["unmatched"].format(table=parent, other=child), "table": parent}
        built.append({**common, "shape": "unmatched", "value": None, "alternatives": (child, ref, parent),
                      "sql": f"{head}(SELECT {_quote(child)}.{_quote(ref)} FROM {_quote(child)}){order}"})
        filters = [(child, column, "") for column in db.categories(child)]
        for other_child, other_ref, other, other_key in db.references:
            if other_child == child and other not in (parent, child):
                join = (f" JOIN {_quote(other)} ON {_quote(child)}.{_quote(other_ref)} = "
                        f"{_quote(other)}.{_quote(other_key)}")
                filters.extend((other, column, join) for column in db.categories(other))
        for table, column, join in filters:
            for value in _common(db.values(table, column))[:3]:
                built.append({**common, "shape": "unmatched_filtered", "value": value,
                              "alternatives": (child, ref, parent, table, column),
                              "sql": (f"{head}(SELECT {_quote(child)}.{_quote(ref)} FROM {_quote(child)}{join} WHERE "
                                      f"{_quote(table)}.{_quote(column)} = {_literal(value)}){order}")})
    return built


def ranked(db):
    """Candidate ranked queries of a database: the groups of a referenced table (or a table's own categories) by
    the total or average of a number, each (group, number) pairing asked one way chosen by hash."""
    sources = [(child, f" JOIN {_quote(parent)} ON {_quote(child)}.{_quote(ref)} = {_quote(parent)}.{_quote(key)}",
                parent, [c for c in [db.label(parent)] + db.categories(parent) if c])
               for child, ref, parent, key in db.references]
    sources += [(table["name"], "", table["name"], db.categories(table["name"])) for table in db.capped.values()]
    built = []
    for child, join, group_table, groups in sources:
        pairs = sorted(((group, measure) for group in groups for measure in db.measures(child)),
                       key=lambda pair: _hash(db.db_id, child, group_table, *pair))
        for group, measure in pairs[:MAX_PAIRS]:
            pick = _hash(db.db_id, child, group_table, group, measure, "asked")
            function = "AVG" if pick % 4 == 0 or SHARE.search(measure) else "SUM"
            direction = "DESC" if (pick // 4) % 2 else "ASC"
            total = f"{function}({_quote(child)}.{_quote(measure)})"
            shown = f", {total}" if (pick // 32) % 4 == 0 else ""
            g = f"{_quote(group_table)}.{_quote(group)}"
            base = f"FROM {_quote(child)}{join} GROUP BY {g}"
            try:
                rows = db.run(f"SELECT {g}, {total}, COUNT(*) {base}")
            except Exception:                      # noqa: BLE001 - a pairing this database cannot run
                continue
            fits = [limit for limit in LIMITS if limit < len(rows)]          # fewer groups than there are
            if not fits or any(row[0] is None or row[1] is None for row in rows):
                continue
            limit = fits[(pick // 8) % len(fits)]
            reverse = direction == "DESC"
            by_measure = sorted(rows, key=lambda r: r[1], reverse=reverse)
            by_count = sorted(rows, key=lambda r: r[2], reverse=reverse)
            if by_measure[limit - 1][1] == by_measure[limit][1] or by_count[limit - 1][2] == by_count[limit][2]:
                continue                           # a tie crosses the cut: the answer is not one set
            if {r[0] for r in by_measure[:limit]} == {r[0] for r in by_count[:limit]}:
                continue                           # counting rows answers it too: no contrast to learn
            built.append({"shape": "ranked", "value": None, "limit": limit, "table": group_table,
                          "alternatives": (child, group_table, group, measure),
                          "sql": f"SELECT {g}{shown} {base} ORDER BY {total} {direction} LIMIT {limit}",
                          "hint": HINTS["ranked"].format(table=group_table, column=f"{child}.{measure}",
                                                         measure="total" if function == "SUM" else "average")})
    return built


def aggregated(db):
    """Candidate aggregate queries of a database: for each number of a table, its lowest, highest, average or total
    (chosen by hash), at most once filtered on a category of that table; and each table's number of rows of one
    category."""
    built = []
    for entry in db.capped.values():
        table, categories = entry["name"], db.categories(entry["name"])
        for measure in db.measures(table) + (["*"] if categories else []):
            pick = _hash(db.db_id, table, measure, "aggregate")
            function = "COUNT" if measure == "*" else FUNCTIONS[pick % len(FUNCTIONS)]
            if function == "SUM" and SHARE.search(measure):
                function = "AVG"
            operand = "*" if measure == "*" else f"{_quote(table)}.{_quote(measure)}"
            where, value, filtered = "", None, ""
            if categories and (measure == "*" or (pick // 4) % 2):
                column = categories[(pick // 8) % len(categories)]
                value = _common(db.values(table, column))[0]
                where = f" WHERE {_quote(table)}.{_quote(column)} = {_literal(value)}"
                filtered = f" over the rows whose {column} is {value}"
            if function in ("MIN", "MAX"):
                try:
                    low, high = db.run(f"SELECT MIN({operand}), MAX({operand}) FROM {_quote(table)}{where}")[0]
                except Exception:                  # noqa: BLE001 - a number this database cannot compare
                    continue
                if low is None or low == high:
                    continue                       # one value: the lowest is the highest
            built.append({"shape": "aggregate", "value": value, "table": table, "alternatives": (table, measure),
                          "sql": f"SELECT {function}({operand}) FROM {_quote(table)}{where}",
                          "hint": HINTS["aggregate"].format(measure=WORDS[function], filtered=filtered,
                                                            column=table if measure == "*" else f"{table}.{measure}")})
    return built


def queries(db):
    """The database's runnable, readable queries with their first rows and readings: at most BUDGET[shape] of each
    shape, chosen by hash."""
    jobs = []
    candidates = sorted(unmatched(db) + ranked(db) + aggregated(db),
                        key=lambda c: _hash(db.db_id, c["shape"], *c["alternatives"]))
    kept, done = {}, set()
    for candidate in candidates:
        shape = candidate["shape"]
        if kept.get(shape, 0) >= BUDGET[shape] or candidate["alternatives"] in done:
            continue
        try:
            rows = db.run(candidate["sql"])
            readings = readings_of(import_sql(candidate["sql"], db.graph))
        except Exception:                          # noqa: BLE001 - a query this database or the readings cannot hold
            continue
        if shape.startswith("unmatched") and (not rows or len(rows) >= len(db.table(candidate["table"])["rows"])
                                  or any(row[0] is None for row in rows)):
            continue                               # nothing excluded, or nothing left
        kept[shape] = kept.get(shape, 0) + 1
        done.add(candidate["alternatives"])
        jobs.append({"db_id": db.db_id, "shape": shape, "sql": candidate["sql"], "rows": rows[:3],
                     "value": candidate["value"], "limit": candidate.get("limit"), "hint": candidate["hint"],
                     "readings": readings})
    return jobs


def _schema_text(db):
    lines = []
    for table in db.meta["table_names_original"]:
        entry = db.table(table)
        if entry is None:
            continue
        lines.append(f"- {entry['name']}({', '.join(entry['columns'])}) e.g. {[tuple(r) for r in entry['rows'][:2]]}")
    return "\n".join(lines)


def keeps(job, question):
    """Whether a written question states the query's filter value verbatim and the number of groups it ranks."""
    if job["value"] and job["value"].lower() not in question.lower():
        return False
    if job["limit"] and job["limit"] > 1:
        return any(float(number) == job["limit"] for number, _, _ in numerals.mentions(question))
    return True


def ask(job):
    from engine import llm

    prompt = (f"Schema with sample rows:\n{job['schema']}\n\nQuery:\n{job['sql']}\n\n"
              f"First result rows: {job['rows']}\n\n{job['hint']} Write two different requests a person would type "
              "to get exactly this result, in everyday words: the first a full question, the second only the few "
              "words a person types into a search box, with no verb and no question mark. Do not copy the query's "
              "table or column names unless a person would say them. Keep each value the query compares with word "
              "for word, without quotes around it.")
    for _ in range(2):
        try:
            reply = json.loads(llm.generate_text(system=SYSTEM, prompt=prompt, max_output_tokens=300,
                                                 json_schema=SCHEMA, timeout_seconds=60.0, thinking="LOW"))
            return {**job, "questions": [q.strip() for q in reply.get("questions", []) if q.strip()][:2]}
        except Exception:                          # noqa: BLE001 - one retry, then the query is dropped
            continue
    return {**job, "questions": []}


def main():
    ap = argparse.ArgumentParser(description="Questions for unmatched, ranked and aggregate queries on training "
                                             "databases.")
    ap.add_argument("split_dir", help="the folder of the split's manifest.json (calibration_dbs, test_dbs)")
    ap.add_argument("out_dir")
    ap.add_argument("--bird", help="BIRD's train directory")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, help="at most this many databases (a trial)")
    add_arguments(ap)
    args = ap.parse_args()
    manifest = json.load(open(os.path.join(args.split_dir, "manifest.json")))
    held_out = set(manifest["calibration_dbs"]) | set(manifest.get("test_dbs", []))
    sources = Sources.from_args(args)
    if args.bird:
        sources.add_bird(args.bird)
    training = sorted({q["db_id"] for name in ("train_spider", "train_others", "bird_train")
                       for q in sources.questions.get(name, [])} - held_out)
    training = training[:args.limit] if args.limit else training
    jobs = []
    for db_id in training:
        try:
            db = Database(db_id, sources.tables[db_id], sources.db_path(db_id))
        except (FileNotFoundError, KeyError, ValueError) as exc:
            print(f"skipped {db_id}: {type(exc).__name__}")
            continue
        schema = _schema_text(db)
        jobs.extend({**job, "schema": schema} for job in queries(db))
        db.connection.close()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        answered = list(pool.map(ask, jobs))
    os.makedirs(args.out_dir, exist_ok=True)
    questions, shapes = [], {}
    with open(os.path.join(args.out_dir, "readings_structure.jsonl"), "w", encoding="utf-8", newline="\n") as sink:
        for job in answered:
            for question in job["questions"]:
                if not keeps(job, question):
                    continue
                sink.write(json.dumps({"source": "structure", "index": len(questions), "db_id": job["db_id"],
                                       "status": "ok", "readings": job["readings"]}) + "\n")
                questions.append({"db_id": job["db_id"], "question": question, "query": job["sql"],
                                  "shape": job["shape"]})
                shapes[job["shape"]] = shapes.get(job["shape"], 0) + 1
    with open(os.path.join(args.out_dir, "questions.json"), "w", encoding="utf-8", newline="\n") as sink:
        json.dump(questions, sink, indent=0)
    print(f"{len(training)} databases, {len(jobs)} queries, {len(questions)} questions {shapes} -> {args.out_dir}")


if __name__ == "__main__":
    main()
