"""Running a program on the request's tables: through the typed AST for the candidates the verifier reads, and through the dual deterministic emitters, both programs checked against each other, for a chosen one."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from engine.deterministic import DeterministicAnalysis, ExecutionMode
from engine.numeric import canonical_decimal
from engine.prepared import SharedConnection
from engine.program.language import Breakdown, Calc, Check, Col, Program, Rank, Show, tables_of
from engine.program.lowering import to_plan, to_query
from engine.program.names import ENTITY_GROUP, ENTITY_LINE, ENTITY_PEER
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
    keyed: Col | None = None
    if isinstance(program, Show) and program.listing and len(program.shown) > 1:     # each column's folds back in its spelling
        spellings = [tables.catalog.table(column.table).written_spelling[column.position] for column in program.shown]
        return tuple(tuple(words.get(cell, cell) for words, cell in zip(spellings, row)) for row in rows)
    if isinstance(program, Show) and program.listing:
        keyed = program.shown[0]
    elif isinstance(program, Rank) and program.entity == ENTITY_GROUP:
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
