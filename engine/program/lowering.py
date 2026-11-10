"""A program as a typed query: the one SQL representation (engine/sql_ast.py) over the request's tables (store.py), and from there one plan for both deterministic emitters.

The fixed semantics live here as SQL shape. Data lines only, unless a condition addresses printed lines. A read takes the first line in sheet order, a printed label the shortest match. A rank keeps every tie and skips a blank. A group with no name is
not a group. In + and - a blank side counts 0, and a line blank on both sides is not a line. A LIKE pattern reads % and _ in a cell's own text as one character each (the AST has no ESCAPE): a slight superset, the same in both programs.
"""
from __future__ import annotations

from dataclasses import replace

from engine.deterministic import AnalysisPlan, lower_calculation, lower_select_query
from engine.program.catalog import Catalog
from engine.program.language import (Across, Agg, Calc, Check, Col, Compare, CompareColumns, Condition, Contains, DateOf, Equals, InPeriod, InSection, IsBlank, IsNumber, Measure, NotEquals, OnLine, Program, Rank, RowArithmetic, SECTION, Show, Size, addresses_printed)
from engine.program.names import ANY, ANYWHERE, AVG, COUNT, COUNT_DISTINCT, ENDS, ENTITY_GROUP, ENTITY_LINE, EQ, EVERY, GE, GT, HIGHEST, LE, LT, MAX, MIN, MINUS, NE, NO, OVER, PLUS, STARTS, SUM
from engine.program.semantics import fold as fold_text
from engine.program.store import (LABEL, LABEL_LENGTH, LINE, ONE_ROW, PEER_FOLD, PEER_NAME, PEER_NUMBER, ROLE, SECTION_FOLD, SECTION_PATH, SECTION_TEXT, day_column, fold_column, number_column, peers_table, text_column)
from engine.sheet.names import RECORD
from engine.sql_ast import (Abs, Aggregate, BinaryExpr, BlankAsZero, BooleanExpr, ColumnRef, Comparison, DatePart, Join, Literal, OrderTerm, ScalarSubquery, SelectItem, SelectQuery, SQLType, Star, and_predicates, column_refs)

_FUNCTIONS = {SUM: "SUM", AVG: "AVG", MIN: "MIN", MAX: "MAX"}
VALUE = "value"
KEY = "key"
POSITION = "position"


def _number(column: Col) -> ColumnRef:
    return ColumnRef(column.table, number_column(column.position), SQLType.REAL)


def _fold(column: Col) -> ColumnRef:
    return ColumnRef(column.table, SECTION_FOLD if column.position == SECTION else fold_column(column.position), SQLType.TEXT)


def _shown(column: Col) -> ColumnRef:
    return ColumnRef(column.table, SECTION_TEXT if column.position == SECTION else text_column(column.position), SQLType.TEXT)


def _day(column: Col) -> ColumnRef:
    return ColumnRef(column.table, day_column(column.position), SQLType.DATE)


def _like(text: str, anchor: str = ANYWHERE) -> Literal:
    body = fold_text(text).replace("%", "_")
    return Literal(f"{body}%" if anchor == STARTS else f"%{body}" if anchor == ENDS else f"%{body}%", SQLType.TEXT)


def _is_null(column: ColumnRef, negated: bool = False) -> Comparison:
    return Comparison(column, "IS NOT" if negated else "IS", Literal(None))


def _condition(condition: Condition):
    if isinstance(condition, Equals):
        if isinstance(condition.value, (int, float)):
            return Comparison(_number(condition.column), "=", Literal(float(condition.value)))
        return Comparison(_fold(condition.column), "=", Literal(fold_text(condition.value), SQLType.TEXT))
    if isinstance(condition, NotEquals):
        return Comparison(_fold(condition.column), "!=", Literal(fold_text(condition.value), SQLType.TEXT))
    if isinstance(condition, Contains):
        return Comparison(_fold(condition.column), "LIKE", _like(condition.text, condition.anchor))
    if isinstance(condition, InPeriod):
        span, day = condition.span, _day(condition.column)
        if span.start is None:
            return Comparison(DatePart("month", day), "=", Literal(span.month))
        return BooleanExpr("AND", (Comparison(day, ">=", Literal(span.start, SQLType.DATE)), Comparison(day, "<=", Literal(span.end or span.start, SQLType.DATE))))
    if isinstance(condition, Compare):
        return Comparison(_number(condition.column), "!=" if condition.op == NE else condition.op, Literal(float(condition.number)))
    if isinstance(condition, CompareColumns):
        return Comparison(_number(condition.column), "!=" if condition.op == NE else condition.op, _number(condition.other))
    if isinstance(condition, IsBlank):
        return _is_null(_fold(condition.column), negated=condition.filled)
    if isinstance(condition, IsNumber):
        return _is_null(_number(condition.column), negated=True)
    if isinstance(condition, (InSection, OnLine)):
        return _line_condition(condition, condition.table)
    raise TypeError(f"unknown condition: {type(condition).__name__}")


def _line_condition(condition, table: str):
    """A condition on a line's bands or label, on its own table or on the melted view, which holds them too."""
    if isinstance(condition, InSection):
        path = ColumnRef(table, SECTION_PATH, SQLType.TEXT)
        return Comparison(path, "LIKE", _like(condition.text) if condition.partial else _like(f"| {condition.text} |"))
    label = ColumnRef(table, LABEL, SQLType.TEXT)
    term = Comparison(label, "LIKE", _like(condition.text)) if fold_text(condition.text) else Comparison(label, "=", Literal("", SQLType.TEXT))
    if condition.role is None:
        return term
    return BooleanExpr("AND", (term, Comparison(ColumnRef(table, ROLE, SQLType.TEXT), "=", Literal(condition.role, SQLType.TEXT))))


def _data_lines(table: str):
    return Comparison(ColumnRef(table, ROLE, SQLType.TEXT), "=", Literal(RECORD, SQLType.TEXT))


def _measure(measure: Measure):
    if isinstance(measure, Col):
        return _number(measure)
    if isinstance(measure, DateOf):
        return _day(measure.column)
    if isinstance(measure, Size):
        return Abs(_measure(measure.measure))
    if isinstance(measure, Across):
        total = BlankAsZero(_number(measure.columns[0]))
        for column in measure.columns[1:]:
            total = BinaryExpr(total, PLUS, BlankAsZero(_number(column)))
        return total
    left, right = _measure(measure.left), _measure(measure.right)
    if measure.op in (PLUS, MINUS):
        left = BlankAsZero(left) if isinstance(left, ColumnRef) else left
        right = BlankAsZero(right) if isinstance(right, ColumnRef) else right
    return BinaryExpr(left, measure.op, right)


def _present(measure: Measure):
    """The lines a measure reads: a blank is skipped, in + and - only when both sides are, and a zero denominator leaves its line out."""
    if isinstance(measure, Col):
        return _is_null(_number(measure), negated=True)
    if isinstance(measure, DateOf):
        return _is_null(_day(measure.column), negated=True)
    if isinstance(measure, Size):
        return _present(measure.measure)
    if isinstance(measure, Across):
        return BooleanExpr("OR", tuple(_is_null(_number(column), negated=True) for column in measure.columns))
    left, right = _present(measure.left), _present(measure.right)
    if measure.op in (PLUS, MINUS):
        return BooleanExpr("OR", (left, right))
    terms = [left, right]
    if measure.op == OVER and isinstance(measure.right, Col):
        terms.append(Comparison(_number(measure.right), "!=", Literal(0)))
    return BooleanExpr("AND", tuple(terms))


def _where(*terms):
    return and_predicates(term for term in terms if term is not None)


def _order(direction: str) -> str:
    return "DESC" if direction == HIGHEST else "ASC"


def _show_query(program: Show, *, scalar: bool = False) -> SelectQuery:
    if program.listing:
        key = _fold(program.shown[0])
        where = _where(None if addresses_printed(program.where) else _data_lines(program.table), *map(_condition, program.where), _is_null(key, negated=True))
        return SelectQuery((SelectItem(key, VALUE),), program.table, where=where, group_by=(key,), order_by=(OrderTerm(Aggregate("MIN", ColumnRef(program.table, LINE, SQLType.INTEGER))),))
    columns = [_number(program.shown[0])] if scalar else [_shown(column) for column in program.shown]
    select = tuple(SelectItem(column, f"shown_{at}") for at, column in enumerate(columns))
    order = [OrderTerm(ColumnRef(program.table, LINE, SQLType.INTEGER))]
    if addresses_printed(program.where):
        order.insert(0, OrderTerm(ColumnRef(program.table, LABEL_LENGTH, SQLType.INTEGER)))
    return SelectQuery(select, program.table, where=_where(*map(_condition, program.where)), order_by=tuple(order), limit=1)


def _reads_printed(measure: Measure, table) -> bool:
    return isinstance(measure, Col) and measure.position in table.printed_only_columns


def _agg_query(program: Agg, catalog: Catalog) -> SelectQuery:
    table = program.table
    guard = None
    if program.function == COUNT:
        value = Aggregate("COUNT", Star())
    elif program.function == COUNT_DISTINCT:
        value = Aggregate("COUNT", _fold(program.measure), distinct=True)
    else:
        value = Aggregate(_FUNCTIONS[program.function], _measure(program.measure))
        if isinstance(program.measure, (RowArithmetic, Size, Across)):
            guard = _present(program.measure)
    if program.magnitude:
        value = Abs(value)
    every_line = addresses_printed(program.where) or (program.measure is not None and _reads_printed(program.measure, catalog.table(table)))
    where = _where(None if every_line else _data_lines(table), *map(_condition, program.where), guard)
    return SelectQuery((SelectItem(value, VALUE),), table, where=where)


def _rank_query(program: Rank, catalog: Catalog) -> SelectQuery:
    table = program.table
    printed = addresses_printed(program.where) or (program.measure is not None and _reads_printed(program.measure, catalog.table(table)))
    order = _order(program.direction)
    if program.entity == ENTITY_LINE:
        value = _measure(program.measure)
        select = tuple(SelectItem(_shown(column), f"shown_{at}") for at, column in enumerate(program.shown)) + (SelectItem(value, VALUE),)
        where = _where(None if printed else _data_lines(table), *map(_condition, program.where), _present(program.measure))
        if program.count > 1:      # exactly that many lines, a tie broken by sheet order
            return SelectQuery(select, table, where=where, order_by=(OrderTerm(value, order), OrderTerm(ColumnRef(table, LINE, SQLType.INTEGER))), limit=program.count)
        return SelectQuery(select, table, where=where, order_by=(OrderTerm(value, order),), limit=1, with_ties=True)
    if program.entity == ENTITY_GROUP:
        key = _fold(program.key)
        if program.function == COUNT:
            value, guard = Aggregate("COUNT", Star()), None
        else:
            value, guard = Aggregate(_FUNCTIONS[program.function], _measure(program.measure)), _present(program.measure)
        where = _where(None if printed else _data_lines(table), *map(_condition, program.where), _is_null(key, negated=True), guard)
        ranked = OrderTerm(Abs(value) if program.by_size else value, order)
        if program.count > 1:      # exactly that many groups, a tie broken by the key
            return SelectQuery((SelectItem(key, KEY), SelectItem(value, VALUE)), table, where=where, group_by=(key,), order_by=(ranked, OrderTerm(key)), limit=program.count)
        return SelectQuery((SelectItem(key, KEY), SelectItem(value, VALUE)), table, where=where, group_by=(key,), order_by=(ranked,), limit=1, with_ties=True)
    return _peer_query(program, printed, order)


def _peer_query(program: Rank, printed: bool, order: str) -> SelectQuery:
    table, peers = program.table, peers_table(program.table)
    name = ColumnRef(peers, PEER_NAME, SQLType.TEXT)
    position = ColumnRef(peers, POSITION, SQLType.INTEGER)
    numbers = ColumnRef(peers, PEER_NUMBER, SQLType.REAL)
    which = [Comparison(position, "=", Literal(at)) for at in program.peers]
    terms = [BooleanExpr("OR", tuple(which)) if len(which) > 1 else which[0]]
    if not printed:
        terms.append(Comparison(ColumnRef(peers, ROLE, SQLType.TEXT), "=", Literal(RECORD, SQLType.TEXT)))
    joins = ()
    for condition in program.where:
        if isinstance(condition, (OnLine, InSection)):
            terms.append(_line_condition(condition, peers))
        else:
            terms.append(_condition(condition))
            joins = (Join(table, ColumnRef(peers, LINE, SQLType.INTEGER), ColumnRef(table, LINE, SQLType.INTEGER)),)
    if program.mark is not None:
        terms.append(Comparison(ColumnRef(peers, PEER_FOLD, SQLType.TEXT), "=", Literal(fold_text(program.mark), SQLType.TEXT)))
        value = Aggregate("COUNT", Star())
    else:
        terms.append(_is_null(numbers, negated=True))
        value = Aggregate("SUM", numbers)
    return SelectQuery((SelectItem(name, KEY), SelectItem(value, VALUE)), peers, joins=joins, where=_where(*terms), group_by=(name,), order_by=(OrderTerm(Abs(value) if program.by_size else value, order),), limit=1, with_ties=True)


def _scalar(side: Program, catalog: Catalog):
    if isinstance(side, Calc):
        return BinaryExpr(_scalar(side.left, catalog), side.op, _scalar(side.right, catalog))
    if isinstance(side, Show):
        return ScalarSubquery(_show_query(side, scalar=True))
    if isinstance(side, Agg):
        return ScalarSubquery(_agg_query(side, catalog))
    raise TypeError("a calc reads two programs of one number each")


_VERDICT_SQL = {GT: ">", GE: ">=", LT: "<", LE: "<=", EQ: "=", NE: "<>"}


def _check_query(program: Check, catalog: Catalog) -> SelectQuery:
    """A check is one verdict over one-number programs on the one-row table: any is count > 0, no is count = 0,
    every is (count with the one more condition) = (count of the base lines), versus is left op (number or right)."""
    left = _scalar(program.left, catalog)
    if program.quantifier == ANY:
        verdict = BinaryExpr(left, ">", Literal(0))
    elif program.quantifier == NO:
        verdict = BinaryExpr(left, "=", Literal(0))
    elif program.quantifier == EVERY:
        verdict = BinaryExpr(left, "=", _scalar(program.right, catalog))
    else:
        right = Literal(program.number) if program.number is not None else _scalar(program.right, catalog)
        verdict = BinaryExpr(left, _VERDICT_SQL[program.op], right)
    return SelectQuery((SelectItem(verdict, VALUE),), ONE_ROW)


def to_query(program: Program, catalog: Catalog) -> SelectQuery:
    """The typed query a program means."""
    if isinstance(program, Check):
        return _check_query(program, catalog)
    if isinstance(program, Show):
        return _show_query(program)
    if isinstance(program, Agg):
        return _agg_query(program, catalog)
    if isinstance(program, Rank):
        return _rank_query(program, catalog)
    expression = BinaryExpr(_scalar(program.left, catalog), program.op, _scalar(program.right, catalog))
    return SelectQuery((SelectItem(Abs(expression) if program.magnitude else expression, VALUE),), ONE_ROW)


def _affinity(column: ColumnRef) -> str:
    return {SQLType.REAL: "REAL", SQLType.INTEGER: "INTEGER"}.get(column.type, "TEXT")


def plan_schema(query: SelectQuery, catalog: Catalog) -> list[dict]:
    """The columns a query reads, as the deterministic lowering wants them. Only those: a plan's generated classes need no more. A join's target lists its line numbers, which prove it one line per value."""
    seen: dict[tuple[str, str], dict] = {}
    for column in column_refs(query):
        seen.setdefault((column.table, column.name), {"table": column.table, "name": column.name, "affinity": _affinity(column), "is_date": column.type == SQLType.DATE})
    if isinstance(query, SelectQuery):
        for join in query.joins:
            seen[(join.table, LINE)]["values"] = list(range(len(catalog.table(join.table).lines)))
    return [seen[key] for key in sorted(seen)]


def to_plan(program: Program, catalog: Catalog, slug: str = "program") -> AnalysisPlan:
    """The one plan both deterministic emitters read."""
    query = to_query(program, catalog)
    schema = plan_schema(query, catalog)
    plan = lower_calculation(slug, query, schema, ()) if isinstance(program, (Calc, Check)) else lower_select_query(slug, query, schema, ())
    # The store holds a number as the sheet's float: both programs read it as its shortest decimal, one exact reading.
    tables = tuple(replace(table, columns=tuple(replace(column, binary_float=column.type == SQLType.REAL) for column in table.columns)) for table in plan.tables)
    return replace(plan, tables=tables)


__all__ = ["KEY", "VALUE", "plan_schema", "to_plan", "to_query"]
