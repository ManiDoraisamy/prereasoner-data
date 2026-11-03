"""Today's own-data readers (engine/readers) as proposers of candidate programs: unnamed models that may propose and never decide.

A compiled workbook is read by the readers as the tables the sheet compiler makes (SheetView): each catalog table a table of its data lines, each column under its header's name, typed as the catalog reads it (number, date, text), the bands above a line as a "section" column. The readers read a question beside that schema as they read any upload, their readings build typed queries (engine/readers/readings.py), and each query that one program of the language means becomes that program (program_of). Those programs join the decoder's candidates; the verifier runs them; the chooser reads the readers' probability as one named evidence term, "reader_score", beside every other term it shows.

Training goes the other way through the same view: a program lowers to its typed query (engine/program/lowering.py), the query is rewritten onto the view (view_query), and readings_of gives the readings a reader learns (training/readers/sheet_gold.py). A program counts as readable only when its readings build back to the same program.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from engine.program.catalog import Catalog
from engine.program.language import (Agg, Col, Compare, Contains, DateOf, Equals, InPeriod, IsBlank, IsNumber, NotEquals, Program, Rank, SECTION, Show, coordinates, is_sound, period_from_name)
from engine.program.lowering import VALUE, to_query
from engine.program.names import ANYWHERE, COUNT, COUNT_DISTINCT, ENDS, ENTITY_GROUP, ENTITY_LINE, HIGHEST, LOWEST, STARTS
from engine.program.semantics import fold
from engine.program.store import LINE, ROLE, SECTION_FOLD, SECTION_TEXT
from engine.readers.readings import NotReadable, readings_of
from engine.sheet.names import RECORD, V_NUMBER
from engine.sql_ast import Aggregate, BooleanExpr, ColumnRef, Comparison, DatePart, Literal, Lower, OrderTerm, SelectItem, SelectQuery, SQLType, Star
from engine.sql_schema import SchemaColumn, SchemaGraph

CAP = 5000                       # lines a view holds per table, as the readers' evaluator reads a database
SECTION_NAME = "section"
_STORE_COLUMN = {"t": "text", "f": "fold", "n": "number", "d": "day"}
_FUNCTIONS = {"SUM": "sum", "AVG": "avg", "MIN": "min", "MAX": "max"}
_KIND_TYPES = {"number": SQLType.REAL, "date": SQLType.DATE, "text": SQLType.TEXT}


@dataclass
class SheetView:
    """A catalog as the readers' schema: names both ways and the schema graph they read."""
    catalog: Catalog
    graph: SchemaGraph
    table_ids: dict[str, str]                      # view table -> catalog id
    names: dict[str, str]                          # catalog id -> view table
    positions: dict[tuple[str, str], int]          # (view table, view column) -> position (SECTION for the bands)
    columns: dict[tuple[str, int], str]            # (catalog id, position) -> view column
    kinds: dict[tuple[str, int], str]              # (catalog id, position) -> number, date or text
    signature: str = field(default="")


def _unique(name: str, taken: set[str]) -> str:
    name = " ".join(str(name).split()) or "column"
    out, n = name, 2
    while out.casefold() in taken:
        out, n = f"{name} {n}", n + 1
    taken.add(out.casefold())
    return out


def _kind(table, position: int) -> str:
    """number, date or text: the compiler's kind, and a column whose filled data cells are mostly numbers reads as numbers, as a program's measure reads it."""
    if position == SECTION:
        return "text"
    if table.is_date_column(position):
        return "date"
    if table.columns[position].kind == V_NUMBER:
        return "number"
    filled = [number for number, text, data in zip(table.numbers[position], table.texts[position], table.is_data) if data and text is not None]
    return "number" if filled and 2 * sum(number is not None for number in filled) > len(filled) else "text"


def _cell(value):
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return int(value)
    return value


def sheet_view(catalog: Catalog, cap: int = CAP) -> SheetView:
    """Every catalog table as a table of its data lines (at most cap), its columns under their header names, typed as the catalog reads them. Deterministic: names and order follow the catalog's."""
    columns: list[SchemaColumn] = []
    table_ids, names, positions, column_names, kinds = {}, {}, {}, {}, {}
    taken_tables: set[str] = set()
    digest = hashlib.blake2b(digest_size=16)
    index = 0
    for table in catalog.tables:
        view_name = _unique((table.table.name or table.tab).replace(".", " "), taken_tables)
        table_ids[view_name], names[table.id] = table.id, view_name
        lines = [at for at, data in enumerate(table.is_data) if data][:cap]
        taken: set[str] = set()
        wanted = list(range(len(table.columns)))
        if any(table.section_texts[at] for at in lines):
            wanted.append(SECTION)
        for position in wanted:
            kind = _kind(table, position)
            if position == SECTION:
                name = _unique(SECTION_NAME, taken)
                values = tuple(table.section_texts[at] for at in lines)
            else:
                name = _unique(table.columns[position].name or f"column {table.columns[position].letter}", taken)
                source = table.numbers if kind == "number" else table.days if kind == "date" else table.texts
                values = tuple(_cell(source[position][at]) for at in lines)
            ref = ColumnRef(view_name, name, _KIND_TYPES[kind])
            columns.append(SchemaColumn(ref, values, index))
            index += 1
            positions[(view_name, name)] = position
            column_names[(table.id, position)] = name
            kinds[(table.id, position)] = kind
            digest.update(repr((view_name, name, kind, values)).encode("utf-8", "surrogatepass"))
    return SheetView(catalog, SchemaGraph(columns, ()), table_ids, names, positions, column_names, kinds, digest.hexdigest())


def program_key(program: Program) -> tuple[str, ...]:
    """What makes two programs the same candidate: the coordinates they fill, in one order."""
    return tuple(sorted(coordinates(program)))


# -------------------------------------------------------------------------------------------- program -> readings
def _store_position(name: str) -> tuple[int, str]:
    """(position, which reading) of a store column c{p}_{t,f,n,d}, or the section's."""
    if name in (SECTION_FOLD, SECTION_TEXT):
        return SECTION, "fold"
    if name.startswith("c") and "_" in name:
        head, tail = name[1:].split("_", 1)
        if head.isdigit() and tail in _STORE_COLUMN:
            return int(head), _STORE_COLUMN[tail]
    raise NotReadable("sheet-internal")


class _Rewrite:
    """A lowered program's typed query onto the view: store columns become the view's, the store's own terms (data lines, sheet order, a blank guard) go, as the view holds data lines in sheet order and a reading skips a blank as SQL does."""

    def __init__(self, view: SheetView, table_id: str):
        self.view, self.table_id = view, table_id
        self.name = view.names[table_id]

    def ref(self, column: ColumnRef) -> ColumnRef:
        if column.table != self.table_id:
            raise NotReadable("other-table")
        position, _ = _store_position(column.name)
        if (self.table_id, position) not in self.view.columns:
            raise NotReadable("unknown-column")
        return self.view.graph.column_map[(self.name, self.view.columns[(self.table_id, position)])].ref

    def spelled(self, column: ColumnRef, value):
        """A folded literal as the sheet spells it, so a reader finds it in the question or in the listing."""
        position, reading = _store_position(column.name)
        if reading != "fold" or not isinstance(value, str):
            return value
        return self.view.catalog.table(self.table_id).spelling.get(position, {}).get(value, value)

    def operand(self, expression):
        if isinstance(expression, ColumnRef):
            return self.ref(expression)
        if isinstance(expression, Aggregate):
            return Aggregate(expression.function, expression.operand if isinstance(expression.operand, Star) else self.operand(expression.operand), expression.distinct)
        if isinstance(expression, DatePart):
            return DatePart(expression.part, self.ref(expression.operand))
        raise NotReadable(f"operand:{type(expression).__name__}")

    def predicate(self, predicate):
        """The predicate without the store's own terms, or None when nothing is left."""
        if predicate is None:
            return None
        if isinstance(predicate, BooleanExpr):
            terms = [term for term in (self.predicate(term) for term in predicate.terms) if term is not None]
            return None if not terms else terms[0] if len(terms) == 1 else BooleanExpr(predicate.operator, tuple(terms))
        if not isinstance(predicate, Comparison):
            raise NotReadable(f"predicate:{type(predicate).__name__}")
        left = predicate.left
        if isinstance(left, ColumnRef) and left.name == ROLE and predicate.operator == "=" and isinstance(predicate.right, Literal) and predicate.right.value == RECORD:
            return None                                        # the view holds data lines only
        if predicate.operator == "IS NOT" and isinstance(predicate.right, Literal) and predicate.right.value is None:
            return None                                        # a blank guard: a reading's aggregate skips a blank, its rank orders it apart
        if predicate.operator in ("IS", "IS NOT"):
            raise NotReadable("blank-condition")
        if not isinstance(predicate.right, Literal):
            raise NotReadable("compares-two-columns")
        value = predicate.right.value
        if predicate.operator in ("LIKE", "NOT LIKE"):
            if not (isinstance(value, str) and value.startswith("%") and value.endswith("%") and len(value) > 2):
                raise NotReadable("anchored-like")
            return Comparison(self.operand(left), predicate.operator, Literal(value, SQLType.TEXT))
        if isinstance(left, ColumnRef):
            value = self.spelled(left, value)
        return Comparison(self.operand(left), predicate.operator, Literal(value, predicate.right.type))


def view_query(program: Program, view: SheetView) -> SelectQuery:
    """The typed query a program lowers to (engine/program/lowering.py), rewritten onto the view. Raises NotReadable for what the readings cannot hold or the view does not show: a calculation, a peer rank, a printed line, a band condition, a blank condition, a row's arithmetic, a size."""
    if not isinstance(program, (Show, Agg, Rank)):
        raise NotReadable("calc")
    if isinstance(program, Rank) and program.entity not in (ENTITY_LINE, ENTITY_GROUP):
        raise NotReadable("peer-rank")
    if any(isinstance(condition, (IsBlank, IsNumber)) for condition in program.where):
        raise NotReadable("blank-condition")                                # the readings compare with values; a blank is none
    table = view.catalog.table(program.table)
    measure = getattr(program, "measure", None)
    if isinstance(measure, Col) and measure.position in table.printed_only_columns:
        raise NotReadable("printed-lines")
    query = to_query(program, view.catalog)
    if not isinstance(query.from_table, str) or query.from_table != program.table or query.joins:
        raise NotReadable("sheet-internal")
    rewrite = _Rewrite(view, program.table)
    items = list(query.select)
    if isinstance(program, Rank):
        items = [item for item in items if item.alias != VALUE]            # a rank names its line or group; the figure it ranks by stays in its order
    select = tuple(SelectItem(rewrite.operand(item.expression)) for item in items)
    order: list[OrderTerm] = []
    for term in query.order_by:
        expression = term.expression
        if isinstance(expression, ColumnRef) and expression.name == LINE:
            continue                                                         # sheet order is the view's own
        if isinstance(expression, Aggregate) and isinstance(expression.operand, ColumnRef) and expression.operand.name == LINE:
            continue
        order.append(OrderTerm(rewrite.operand(expression), term.direction))
    group = tuple(rewrite.operand(column) for column in query.group_by)
    distinct, limit = query.distinct, query.limit
    if isinstance(program, Show):
        limit = None                                                         # a read is the line the conditions keep; a reading lists them all
        if program.listing:
            group, distinct = (), True
    return SelectQuery(select, view.names[program.table], where=rewrite.predicate(query.where), group_by=group, order_by=tuple(order), limit=limit, distinct=distinct)


def program_readings(program: Program, view: SheetView) -> dict:
    """The readings a reader learns for a program. Raises NotReadable."""
    return readings_of(view_query(program, view))


# -------------------------------------------------------------------------------------------- readings -> program
def _days(text) -> date | None:
    try:
        return date.fromisoformat(str(text).strip()[:10])
    except ValueError:
        return None


def _span_name(first: date, last: date) -> str:
    """The period name of a stretch of days: a day, a month, a quarter or a year when it is exactly one, else first..last."""
    if first == last:
        return first.isoformat()
    following = last + timedelta(days=1)
    if first.day == 1 and following.day == 1:
        months = (following.year - first.year) * 12 + following.month - first.month
        if months == 1:
            return first.isoformat()[:7]
        if months == 3 and first.month in (1, 4, 7, 10):
            return f"{first.year}-q{(first.month + 2) // 3}"
        if months == 12 and first.month == 1:
            return str(first.year)
    return f"{first.isoformat()}..{last.isoformat()}"


def _conditions(predicate, view: SheetView, table_id: str) -> tuple | None:
    """A built query's conditions as the language's, or None when one has no form there."""
    if predicate is None:
        return ()
    if isinstance(predicate, BooleanExpr):
        if predicate.operator != "AND":
            return None
        terms = list(predicate.terms)
    else:
        terms = [predicate]
    flat = []
    while terms:
        term = terms.pop(0)
        if isinstance(term, BooleanExpr):
            if term.operator != "AND":
                return None
            terms = list(term.terms) + terms
        else:
            flat.append(term)
    out = []
    bounds: dict[Col, list] = {}
    for term in flat:
        if not isinstance(term, Comparison) or not isinstance(term.right, Literal):
            return None
        left, op, value = term.left, term.operator, term.right.value
        part = None
        if isinstance(left, Lower):
            left = left.operand
        if isinstance(left, DatePart):
            part, left = left.part, left.operand
        if not isinstance(left, ColumnRef) or view.table_ids.get(left.table) != table_id:
            return None
        position = view.positions.get((left.table, left.name))
        if position is None:
            return None
        col, kind = Col(table_id, position), view.kinds[(table_id, position)]
        if part is not None:
            if op != "=" or kind != "date":
                return None
            if part == "month" and isinstance(value, (int, float)) and 1 <= int(value) <= 12:
                out.append(InPeriod(col, period_from_name(f"m{int(value):02d}")))
            elif part == "year_month" and isinstance(value, str) and len(value) == 7 and _days(value + "-01") is not None:
                out.append(InPeriod(col, period_from_name(value)))
            else:
                return None
            continue
        if op in ("LIKE", "NOT LIKE"):
            if op != "LIKE" or not isinstance(value, str) or kind == "number":
                return None
            text = value.strip("%")
            if not text:
                return None
            anchor = ANYWHERE if value.startswith("%") and value.endswith("%") else ENDS if value.startswith("%") else STARTS if value.endswith("%") else None
            if anchor is None:
                return None
            out.append(Contains(col, fold(text), anchor))
            continue
        if kind == "date":
            day = _days(value)
            if day is None:
                return None
            held = bounds.setdefault(col, [None, None])
            if op == "=":
                held[0] = held[1] = day
            elif op in (">=", ">"):
                held[0] = day + timedelta(days=1) if op == ">" else day
            elif op in ("<=", "<"):
                held[1] = day - timedelta(days=1) if op == "<" else day
            else:
                return None
            continue
        if kind == "number":
            try:
                number = float(str(value).replace(",", ""))
            except ValueError:
                return None
            out.append(Equals(col, number) if op == "=" else Compare(col, op, number) if op in (">=", ">", "<=", "<", "!=") else None)
            if out[-1] is None:
                return None
            continue
        if op == "=":
            out.append(Equals(col, fold(str(value))))
        elif op == "!=":
            out.append(NotEquals(col, fold(str(value))))
        else:
            return None
    for col, (first, last) in bounds.items():
        if first is None or last is None or last < first:
            return None
        out.append(InPeriod(col, period_from_name(_span_name(first, last))))
    return tuple(out)


def _col(expression, view: SheetView, table_id: str) -> Col | None:
    if not isinstance(expression, ColumnRef) or view.table_ids.get(expression.table) != table_id:
        return None
    position = view.positions.get((expression.table, expression.name))
    return None if position is None else Col(table_id, position)


def _measure(column: Col | None, view: SheetView):
    """A column a figure is read from: a number as itself, a date as its day; a text column is no measure."""
    if column is None:
        return None
    kind = view.kinds[(column.table, column.position)]
    return column if kind == "number" else DateOf(column) if kind == "date" else None


def _aggregate(expression: Aggregate, view: SheetView, table_id: str) -> tuple[str, object] | None:
    """(function, measure) of an aggregate, or None."""
    function = expression.function
    if function == "COUNT":
        if isinstance(expression.operand, Star) or not expression.distinct:
            return COUNT, None
        column = _col(expression.operand, view, table_id)
        return (COUNT_DISTINCT, column) if column is not None and column.position != SECTION else None
    if function not in _FUNCTIONS or expression.distinct:
        return None
    measure = _measure(_col(expression.operand, view, table_id), view)
    if measure is None or (isinstance(measure, DateOf) and function not in ("MIN", "MAX")):
        return None
    return _FUNCTIONS[function], measure


def program_of(query, view: SheetView) -> Program | None:
    """The program a reader's built query means, or None when no one program of the language means it."""
    if not isinstance(query, SelectQuery) or query.joins or not isinstance(query.from_table, str) or query.having is not None or query.from_alias:
        return None
    table_id = view.table_ids.get(query.from_table)
    if table_id is None:
        return None
    where = _conditions(query.where, view, table_id)
    if where is None:
        return None
    items = [item.expression for item in query.select]
    if not items or any(not isinstance(expression, (ColumnRef, Aggregate)) for expression in items):
        return None
    direction = None
    if query.order_by:
        directions = {term.direction for term in query.order_by}
        if len(directions) != 1 or len(query.order_by) != 1:
            return None
        direction = HIGHEST if directions == {"DESC"} else LOWEST
    try:
        program = _form(query, items, view, table_id, where, direction)
    except ValueError:
        return None
    return program if program is not None and is_sound(program) else None


def _form(query: SelectQuery, items: list, view: SheetView, table_id: str, where: tuple, direction: str | None) -> Program | None:
    aggregates = [expression for expression in items if isinstance(expression, Aggregate)]
    plain = [expression for expression in items if isinstance(expression, ColumnRef)]
    if query.group_by:
        if len(query.group_by) != 1 or query.limit != 1 or direction is None or not isinstance(query.order_by[0].expression, Aggregate):
            return None
        key = _col(query.group_by[0], view, table_id)
        ranked = _aggregate(query.order_by[0].expression, view, table_id)
        if key is None or ranked is None or ranked[0] == COUNT_DISTINCT or any(expression != query.group_by[0] for expression in plain):
            return None
        if any(expression != query.order_by[0].expression for expression in aggregates):
            return None
        function, measure = ranked
        return Rank(ENTITY_GROUP, table_id, direction, where, measure=measure, key=key, function=function)
    if aggregates:
        if len(items) != 1 or query.order_by or query.limit not in (None, 1) or query.distinct:
            return None
        found = _aggregate(aggregates[0], view, table_id)
        if found is None:
            return None
        return Agg(found[0], found[1], table_id, where)
    columns = [_col(expression, view, table_id) for expression in plain]
    if any(column is None for column in columns):
        return None
    if query.order_by:
        ordered = query.order_by[0].expression
        if query.limit != 1 or not isinstance(ordered, ColumnRef):
            return None
        measure = _measure(_col(ordered, view, table_id), view)
        shown = tuple(column for column, expression in zip(columns, plain) if expression != ordered)
        if measure is None or not shown:
            return None
        return Rank(ENTITY_LINE, table_id, direction, where, measure=measure, shown=shown)
    if query.limit not in (None, 1):
        return None
    if query.distinct and len(columns) == 1:
        return Show(table_id, (columns[0],), where, listing=True)
    return Show(table_id, tuple(columns), where)


# --------------------------------------------------------------------------------------------------- proposing
@dataclass(frozen=True, slots=True)
class Proposal:
    """A program the readers propose: its probability under the most probable reading that builds it (the reader score), and the first place a reader listed it."""
    program: Program
    score: float
    rank: int


class ReaderProposer:
    """The readers of one bundle folder (stage1/, stage2_*/; the readers' own re-scorers and choice are not read: the named chooser decides). Proposals are kept per (question, view), so a question read again over the same sheet is read once."""

    def __init__(self, directory: str | Path):
        from engine.readers.search import ReadingSearch
        self.directory = str(directory)
        self.search = ReadingSearch(directory, rescore=False)
        self.cache: dict[tuple[str, str], tuple[Proposal, ...]] = {}

    def propose(self, question: str, view: SheetView) -> tuple[Proposal, ...]:
        key = (question, view.signature)
        held = self.cache.get(key)
        if held is None:
            held = proposals_of(self.search.pool(question, view.graph), view)
            self.cache[key] = held
        return held


def proposals_of(pool, view: SheetView) -> tuple[Proposal, ...]:
    """The readers' pool (engine/readers/search.py, most probable first) as programs, each once at its best score."""
    best: dict[tuple[str, ...], Proposal] = {}
    for rank, candidate in enumerate(pool):
        program = program_of(candidate.query, view)
        if program is None:
            continue
        key = program_key(program)
        score = round(math.exp(min(float(candidate.score), 0.0)), 4)
        held = best.get(key)
        if held is None:
            best[key] = Proposal(program, score, rank)
        elif score > held.score:
            best[key] = Proposal(held.program, score, held.rank)
    return tuple(sorted(best.values(), key=lambda proposal: (proposal.rank, -proposal.score)))


__all__ = ["CAP", "Proposal", "ReaderProposer", "SheetView", "program_key", "program_of", "program_readings", "proposals_of", "sheet_view", "view_query"]
