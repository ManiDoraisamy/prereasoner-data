"""Running a program on the request's tables: through the typed AST for the candidates the verifier reads, and through the dual deterministic emitters, both programs checked against each other, for a chosen one."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from engine.deterministic import DeterministicAnalysis, ExecutionMode
from engine.numeric import canonical_decimal
from engine.prepared import SharedConnection
from engine.program.catalog import CatalogTable
from engine.program.language import Agg, Breakdown, Calc, Check, Col, Program, Rank, Show, tables_of
from engine.program.lowering import to_plan, to_query
from engine.program.names import ENTITY_GROUP, ENTITY_LINE, ENTITY_PEER, GRAIN_CALENDAR_MONTH, MAX, MIN
from engine.program.store import RequestTables
from engine.sql_ast import render_query

# The candidates run on SQLite's own floats: fifty of them on a large table cost what one costs in exact decimals. Their answers are compared with printed figures at 1e-6 and differ from exact arithmetic at about 1e-13. A chosen program's answer comes from run_dual, in exact decimals.
SQL_DIALECT = "standard"


@dataclass(frozen=True, slots=True)
class Result:
    """A program's answer: rows of cells as the sheet wrote them, its own spelling restored where the program keyed by the fold."""
    rows: tuple[tuple, ...]


def needs_peers(program: Program) -> tuple[str, ...]:
    if isinstance(program, Calc):
        return needs_peers(program.left) + needs_peers(program.right)
    return (program.table,) if isinstance(program, Rank) and program.entity == ENTITY_PEER else ()


def prepare(program: Program, tables: RequestTables) -> None:
    """Load what a program reads."""
    for table_id in tables_of(program):
        tables.ensure(table_id)
    for table_id in needs_peers(program):
        tables.ensure_peers(table_id)


_LETTERS = re.compile(r"[^\W\d_]+")     # a run of letters of any script


def month_words(table: CatalogTable, position: int) -> dict[int, str]:
    """Each month of any year a date column holds, as its cells write it: the one run of letters that every cell of that
    month holds and no cell of another month does ("October" in "October 5, 1978" and "Sun, October 12, 1978"; "Sept"
    in "Sept. 28"). A month whose cells share no such run, or more than one, is not named: written in digits alone, or
    a lone cell whose weekday is in no other month's cells."""
    every: dict[int, set[str]] = {}     # the runs each of the month's cells holds
    some: dict[int, set[str]] = {}      # the runs any of its cells holds
    for line, day in zip(table.lines, table.days[position]):
        cell = line.cells.get(position)
        if cell is None or not isinstance(cell.raw, str) or not day or len(day) < 7:     # the text as written, a typed day's too
            continue
        month, runs = int(day[5:7]), set(_LETTERS.findall(cell.raw))
        every[month] = every[month] & runs if month in every else runs
        some[month] = some.get(month, set()) | runs
    named = {}
    for month, runs in every.items():
        alone = runs.difference(*(held for at, held in some.items() if at != month))
        if len(alone) == 1:
            named[month] = alone.pop()
    return named


def _displayed(program: Program, rows, tables: RequestTables) -> tuple[tuple, ...]:
    """Rows with the sheet's spelling back, and no row at all for a result that says nothing: a sum over no lines is one row of NULL."""
    if isinstance(program, Rank) and program.entity == ENTITY_LINE and program.count == 1:
        rows = [tuple(row[1:]) for row in rows]      # the line number that put tied lines in sheet order (lowering.TIED_LINE)
    if not rows or all(all(cell is None for cell in row) for row in rows):
        return ()
    if isinstance(program, Check) and program.said is not None:     # the question's word that holds; equal says neither
        verdict = float(rows[0][0])
        return ((program.said[0],),) if verdict > 0 else ((program.said[1],),) if verdict < 0 else ()
    if isinstance(program, Check):          # a verdict, 1 or 0 from either program, reads as the yes or no it is
        return (("True" if rows[0][0] else "False",),)
    if isinstance(program, Breakdown):      # each group's cells by their fold, back in the sheet's spelling
        spellings = [tables.catalog.table(column.table).written_spelling[column.position] for column in program.groups]
        width = len(spellings)
        return tuple(tuple(words.get(cell, cell) for words, cell in zip(spellings, row[:width])) + tuple(row[width:]) for row in rows)
    if isinstance(program, Rank) and program.grain == GRAIN_CALENDAR_MONTH:     # the month as the column writes it
        words = month_words(tables.catalog.table(program.key.table), program.key.position)
        return tuple((words.get(int(row[0]), row[0]) if row[0] is not None else None, *row[1:]) for row in rows)
    keyed: Col | None = None
    if isinstance(program, Show) and program.every:     # the lines' own text, as stored
        return tuple(tuple(row) for row in rows)
    if isinstance(program, Show) and program.listing and len(program.shown) > 1:     # each column's folds back in its spelling
        spellings = [tables.catalog.table(column.table).written_spelling[column.position] for column in program.shown]
        return tuple(tuple(words.get(cell, cell) for words, cell in zip(spellings, row)) for row in rows)
    if isinstance(program, Show) and program.listing:
        keyed = program.shown[0]
    elif isinstance(program, Rank) and program.entity == ENTITY_GROUP and program.grain is None:     # a month or year is its own text
        keyed = program.key
    if keyed is None:
        return tuple(tuple(row) for row in rows)
    words = tables.catalog.table(keyed.table).written_spelling[keyed.position]
    return tuple((words.get(row[0], row[0]), *row[1:]) for row in rows)


def run(program: Program, tables: RequestTables) -> Result:
    """The program through the typed AST, on SQLite's floats."""
    prepare(program, tables)
    return Result(_displayed(program, tables.execute(render_query(to_query(program, tables.catalog), dialect=SQL_DIALECT)), tables))


def _plain(cell):
    """A cell as an answer prints it: an exact number as its canonical decimal text (engine/numeric.py), so the SQL and
    the Python program print the same number alike ("6431836", never "6431836.0" from one of them); a day as ISO text."""
    if isinstance(cell, Decimal):
        return canonical_decimal(cell)
    return cell.isoformat() if hasattr(cell, "isoformat") else str(cell) if cell is not None and not isinstance(cell, (str, int, float)) else cell


def displayed(program: Program, rows, tables: RequestTables) -> tuple[tuple, ...]:
    """The rows a deterministic run of the program returned (each a sequence of cells), as the program shows them: exact
    numbers as their canonical decimal text, days as ISO text, and the sheet's own spelling restored (_displayed)."""
    return _displayed(program, [tuple(_plain(cell) for cell in row) for row in rows], tables)


def answered(program: Program, rows, catalog) -> tuple[tuple, ...]:
    """The rows as the answer gives them, the choice having read them as shown (_displayed): a verdict says Yes or No,
    and a cell the answer takes from a column of numbers or days (a shown cell, the top line's, a column's lowest or
    highest) reads as the sheet writes it ("£5.00", "April 27, 1966") where the column writes that value one way."""
    rows = tuple(tuple(row) for row in rows)
    if not rows:
        return rows
    if isinstance(program, Check) and program.said is None:
        return tuple((("Yes" if row[0] == "True" else "No" if row[0] == "False" else row[0]), *row[1:]) for row in rows)
    sources: tuple = ()
    if isinstance(program, Show) and not program.every:
        sources = program.shown
    elif isinstance(program, Rank) and program.entity == ENTITY_LINE:
        sources = program.shown + ((program.measure,) if isinstance(program.measure, Col) else ())
    elif isinstance(program, Agg) and program.function in (MIN, MAX) and isinstance(program.measure, Col):
        sources = (program.measure,)
    if not sources:
        return rows
    writings = [_writings(catalog.table(column.table), column.position) for column in sources]
    return tuple(tuple(writings[at].get(_cell_key(cell), cell) if at < len(writings) and cell is not None else cell
                       for at, cell in enumerate(row)) for row in rows)


def _cell_key(cell) -> tuple:
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return ("number", float(cell))
    text = str(cell)
    try:
        return ("number", float(text))
    except ValueError:
        return ("day", text)


_PLAIN_WRITING = re.compile(r"^[\d\s.,+\-−]*$")     # digits, separators and signs: a number written plainly


def _writings(table, position: int) -> dict[tuple, str]:
    """{a number or day of the column's cells -> how the sheet writes it}, for the values every line holding them
    writes one way and writes with more than digits, separators and signs ("£3.00", "April 27, 1966", "12%"): a number
    written plainly ("18,123") or a day as ISO keeps its stored value."""
    held: dict[tuple, set[str]] = {}
    for at, line in enumerate(table.lines):
        cell = line.cells.get(position)
        raw = cell.raw if cell is not None else None
        written = " ".join(raw.split()) if isinstance(raw, str) else table.written[position][at]
        number, day = table.numbers[position][at], table.days[position][at]
        key = ("number", float(number)) if number is not None else ("day", day) if day is not None else None
        if key is not None and written not in (None, ""):
            held.setdefault(key, set()).add(str(written))
    return {key: next(iter(texts)) for key, texts in held.items() if len(texts) == 1 and not _PLAIN_WRITING.match(next(iter(texts)))}


def run_dual(program: Program, tables: RequestTables, *, slug: str = "program", python_row_limit: int = 10_000) -> Result:
    """The chosen program through both deterministic emitters: the SQL program and the Python program run on one snapshot, every stage compared. A disagreement raises VerificationMismatch."""
    prepare(program, tables)
    shared = SharedConnection(tables.connection)
    engine = create_engine("sqlite+pysqlite://", creator=lambda: shared, poolclass=StaticPool)
    plan = to_plan(program, tables.catalog, slug)
    estimated = sum(len(tables.catalog.table(table_id).lines) for table_id in tables_of(program))
    result = DeterministicAnalysis(plan, conversation_schema="main", dataset_version="sheet").run(engine, mode=ExecutionMode.VERIFY, estimated_rows=estimated, python_row_limit=python_row_limit)
    return Result(displayed(program, [tuple(row.values()) for row in result.rows], tables))


__all__ = ["Result", "SQL_DIALECT", "displayed", "prepare", "run", "run_dual"]
