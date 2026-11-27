"""retry.py — the own-data retry: Gemini 4 Neon writes one query when the Decisions path's answer is most likely wrong,
or when the person retries an answer.

Owner's decision, 2026-11-21: the path never asks back for its own uncertainty ("you can't push your failure to
users"). Every answer is served and marked (engine/program/closed.py: answered, warned, erred); an erred answer is
retried here at once, and the person may retry any marked answer. A retry reads the workbook the Decisions path reads
(``workbook_tables`` of engine/tables.py TableQuery.workbook's catalog: each compiled table of a tab, its record lines,
with the columns the request's links add to it, "bank → country", "country → continent", "amount → USD"). It describes
those tables (each table's columns with their types, its row count, its first lines and, for a text column of few
values, those values), sends
the question with it through engine/llm.py ``write_query`` (one call), guards the query the model writes (one SELECT,
no forbidden keyword: engine/tables.py ``TableQuery.guard``) and runs it on the request's database (engine/prepared.py)
over typed copies of the request's tables: TEMP tables built from their rows, each named as its table, numbers as
numbers and text as text by the column's own cells (a column whose every filled cell is a plain number, none
zero-padded, is numbers), read-only, bounded in time and rows; the database's own tables are not touched. A query that fails is sent back with its
error once. The model may say the tables cannot answer the question: that is the one case the person is asked (the
question is wrong). The query, its reason and the model are recorded on the answer. Nothing here logs a question, a
cell or a query.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal

from engine import config
from engine.llm import LLMUnavailable, write_query
from engine.request_timing import span

SAMPLE_ROWS = 5               # a table's first lines shown to the model
FEW_VALUES = 25               # a text column of at most this many distinct values lists them all
VALUE_CHARS = 60              # a shown cell is cut to this many characters
ROW_LIMIT = 10_000            # rows a retried answer returns at most
QUERY_SECONDS = 15.0          # a written query runs this long at most
REPAIRS = 1                   # a failed query is sent back with its error this many times

# Where the answer stands in the rows the query returns. The public benchmark told the models it compares this so and
# the retry was not told: its right values then stood behind a label column (9 of the 71 DataBench test answers it
# rewrote at release 4a3b86e5, 2 of 102 on WikiTableQuestions).
ANSWER_FIRST = ("Put the answer itself in the first output column: one row holding the value for a single answer, one row "
                "per item for a list; supporting columns may follow it.")
SYSTEM = (
    "You answer a person's question about their spreadsheet by writing ONE SQLite SELECT statement over the tables "
    "described. A column named \"a → b\" holds what the sheet's column a reaches (a bank's country, a country's "
    "continent, an amount in another currency): use it as any column. "
    "The tables are the tabs of one workbook, in its order: answer from ONE tab, the tab in view when one "
    "is named, else the first tab that holds what the question asks, and combine tabs only when the question asks "
    "across them. Use only those tables and columns, each name in double quotes exactly as given. Numbers are stored as "
    "numbers and dates as ISO text (YYYY-MM-DD). Do not use WITH clauses or the REPLACE function; use subqueries. "
    "Name each output column for what it holds. Read the question literally: a total is a SUM, a count of lines a "
    "COUNT, and a filter matches the values the tables actually hold (match text case-insensitively when the "
    "question's spelling differs). If the tables do not hold what the question asks, set answerable to false, leave "
    "sql empty, and say in one sentence, in the person's terms, what is missing. The reason is one plain sentence "
    "saying how the query reads the question. " + ANSWER_FIRST
)


@dataclass(frozen=True)
class Retried:
    """A retry's outcome: the model's query and reason; the rows it returned (columns, rows) when it ran; answerable
    False when the model says the tables cannot answer the question; error when the query could not run (guarded out
    or failed, after its one repair) or the call failed."""
    answerable: bool
    sql: str
    reason: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple, ...] = ()
    error: str | None = None

    @property
    def answered(self) -> bool:
        return self.answerable and self.error is None

    def record(self) -> dict:
        return {"model": config.QUERY_MODEL, "sql": self.sql or None, "reason": self.reason,
                "answerable": self.answerable, "error": self.error}


def workbook_tables(catalog) -> list[dict]:
    """The retry's tables of a workbook's catalog (engine/program/catalog.py; connected, engine/program/connected.py, in a
    served request): one per compiled table of a tab, named by its tab (a tab's second table "<tab> (2)"), its columns by
    the catalog's names (the sheet's headers, a repeated one by its letter, a link's column by its path), its record
    lines (no title, header or printed total) with each cell as a number, an ISO day or the text the sheet wrote."""
    from engine.program.catalog import KIND_TABLE
    from engine.sheet.names import V_DATE, V_NUMBER

    out, seen_tabs = [], {}
    for table in catalog.tables:
        if table.kind != KIND_TABLE:
            continue
        count = seen_tabs.get(table.tab, 0) + 1
        seen_tabs[table.tab] = count
        columns, names = [], set()
        for position in range(len(table.table.columns)):
            name, at = str(table.column_name(position)), 2
            while name in names:
                name, at = f"{table.column_name(position)} ({at})", at + 1
            names.add(name)
            columns.append(name)
        kinds = [column.kind for column in table.table.columns]
        rows = [[table.numbers[p][i] if kinds[p] == V_NUMBER else
                 (table.days[p][i] or table.written[p][i]) if kinds[p] == V_DATE else table.written[p][i]
                 for p in range(len(columns))]
                for i, data in enumerate(table.is_data) if data]
        out.append({"name": table.tab if count == 1 else f"{table.tab} ({count})", "columns": columns, "rows": rows})
    return out


def retry(question: str, tables: list[dict], guard) -> Retried:
    """The retry of ``question`` over the request's ``tables`` (normalized: name, columns, rows), on the request's
    database. ``guard`` is the one SQL guard (engine/tables.py TableQuery.guard). Never raises: a failed call is a
    Retried with its error."""
    from engine.prepared import scratch_database

    with span("retry"), scratch_database() as database:
        connection = database.connection
        names = _typed_copies(connection, tables)
        try:
            prompt = _prompt(question, connection, names, database.active_sheet)
            try:
                written = write_query(SYSTEM, prompt)
            except LLMUnavailable as exc:
                return Retried(True, "", "", error=str(exc))
            for attempt in range(REPAIRS + 1):
                if not written.answerable:
                    return Retried(False, "", written.reason)
                ok, why = guard(written.sql)
                error = None if ok else f"guard: {why}"
                if ok:
                    try:
                        columns, rows = _run(connection, written.sql)
                        return Retried(True, written.sql, written.reason, columns, rows)
                    except sqlite3.Error as exc:
                        error = f"{type(exc).__name__}: {exc}"
                if attempt == REPAIRS:
                    return Retried(True, written.sql, written.reason, error=error)
                try:
                    written = write_query(SYSTEM, f"{prompt}\n\nYOUR QUERY:\n{written.sql}\n\nIT FAILED: {error}\n"
                                                  "Write it again, following the rules.")
                except LLMUnavailable as exc:
                    return Retried(True, written.sql, written.reason, error=str(exc))
            return Retried(True, written.sql, written.reason, error="no query ran")
        finally:
            for name in names:
                connection.execute(f"DROP TABLE IF EXISTS temp.{_q(name)}")


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _kind(values) -> str:
    """INTEGER or REAL when every filled cell is a plain number (none zero-padded, so a code such as "007" stays text)
    and one is filled; TEXT otherwise."""
    kind, seen = "INTEGER", False
    for value in values:
        if _blank(value):
            continue
        if isinstance(value, bool) or not (isinstance(value, (int, float, Decimal)) or _NUMBER.fullmatch(str(value).strip())):
            return "TEXT"
        seen = True
        if isinstance(value, str) and ("." in value or "e" in value.lower()):
            kind = "REAL"
        elif not isinstance(value, (int, str)) and not float(value).is_integer():
            kind = "REAL"
        elif isinstance(value, (float, Decimal)):
            kind = "REAL"
    return kind if seen else "TEXT"


def _cell(value, kind: str):
    if _blank(value):
        return None
    if kind == "INTEGER":
        return int(float(value))
    if kind == "REAL":
        return float(value)
    return str(value)


def _typed_copies(connection, tables: list[dict]) -> list[str]:
    """TEMP tables of the request's tables, each named as its table so the written query's unqualified names read it,
    each column typed by its cells (``_kind``). Returns their names."""
    names = []
    for table in tables:
        name, columns, rows = str(table["name"]), [str(column) for column in table["columns"]], table["rows"]
        kinds = [_kind(row[at] if at < len(row) else None for row in rows) for at in range(len(columns))]
        connection.execute(f"DROP TABLE IF EXISTS temp.{_q(name)}")
        connection.execute(f"CREATE TEMP TABLE {_q(name)} (" + ", ".join(f"{_q(column)} {kind}" for column, kind in zip(columns, kinds)) + ")")
        connection.executemany(f"INSERT INTO temp.{_q(name)} VALUES ({','.join('?' * len(columns))})",
                               ([_cell(row[at] if at < len(row) else None, kinds[at]) for at in range(len(columns))] for row in rows))
        names.append(name)
    return names


def _shown(value) -> str:
    if value is None:
        return "NULL"
    text = repr(value) if not isinstance(value, str) else repr(value[:VALUE_CHARS])
    return text


def _prompt(question: str, connection, names: list[str], in_view: str | None = None) -> str:
    """The question and its tables as the model reads them: per table its columns with their SQLite types, its row
    count, its first SAMPLE_ROWS lines, and the values of each text column that holds at most FEW_VALUES of them; the
    tab the client has in view (engine/prepared.py RequestDatabase.active_sheet) when the workbook has several."""
    parts = []
    for name in names:
        info = connection.execute(f"PRAGMA temp.table_info({_q(name)})").fetchall()
        columns = [(row[1], row[2] or "TEXT") for row in info]
        count = connection.execute(f"SELECT COUNT(*) FROM temp.{_q(name)}").fetchone()[0]
        lines = [f"TABLE {_q(name)} ({count} rows)"]
        for column, kind in columns:
            line = f"  {_q(column)} {kind}"
            if kind.upper() not in ("REAL", "INTEGER", "NUM", "NUMERIC"):
                distinct = connection.execute(
                    f"SELECT DISTINCT {_q(column)} FROM temp.{_q(name)} WHERE {_q(column)} IS NOT NULL LIMIT {FEW_VALUES + 1}").fetchall()
                if 0 < len(distinct) <= FEW_VALUES:
                    line += " values: " + ", ".join(_shown(row[0]) for row in distinct)
            lines.append(line)
        sample = connection.execute(f"SELECT * FROM temp.{_q(name)} LIMIT {SAMPLE_ROWS}").fetchall()
        if sample:
            lines.append("  first rows:")
            lines.extend("    (" + ", ".join(_shown(value) for value in row) + ")" for row in sample)
        parts.append("\n".join(lines))
    view = f"\n\nTAB IN VIEW: {_q(in_view)}" if in_view and len(names) > 1 and in_view in names else ""
    return "TABLES:\n" + "\n\n".join(parts) + view + f"\n\nQUESTION: {question}"


def _run(connection, sql: str) -> tuple[tuple[str, ...], tuple[tuple, ...]]:
    """The written query's columns and rows (at most ROW_LIMIT), run read-only and stopped after QUERY_SECONDS."""
    deadline = time.monotonic() + QUERY_SECONDS
    connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    connection.execute("PRAGMA query_only = ON")
    try:
        cursor = connection.execute(sql)
        rows = cursor.fetchmany(ROW_LIMIT)
        columns = tuple(str(d[0]) for d in cursor.description or ())
    finally:
        connection.execute("PRAGMA query_only = OFF")
        connection.set_progress_handler(None, 0)
    return columns, tuple(tuple(_value(value) for value in row) for row in rows)


def _value(value):
    """A cell as the answer shows it: a float printed to its 12 significant digits (a sum of money read as floats is
    not 0.30000000000000004), a whole float as an integer."""
    if isinstance(value, float) and math.isfinite(value):
        rounded = float(f"{value:.12g}")
        return int(rounded) if rounded.is_integer() and abs(rounded) < 1e15 else rounded
    return value


__all__ = ["ANSWER_FIRST", "Retried", "retry", "SYSTEM", "workbook_tables"]
