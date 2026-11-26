"""A program as a typed query: the one SQL representation (engine/sql_ast.py) over the request's tables (store.py), and from there one plan for both deterministic emitters.

The fixed semantics live here as SQL shape. Data lines only, unless a condition addresses printed lines. A read takes the first line in sheet order, a printed label the shortest match. A rank keeps every tie and skips a blank. A group with no name is
not a group. In + and - a blank side counts 0, and a line blank on both sides is not a line. A LIKE pattern reads % and _ in a cell's own text as one character each (the AST has no ESCAPE): a slight superset, the same in both programs.
"""
from __future__ import annotations

from dataclasses import replace

from engine.deterministic import AnalysisPlan, lower_calculation, lower_select_query
from engine.program.catalog import Catalog
from engine.program.language import (Absent, Between, LineOrder, Words, Across, Agg, Breakdown, Calc, Check, Col, OneOf, Compare, CompareColumns, CompareLine, Condition, Contains, DateOf, Equals, InPeriod, InYears, InSection, IsBlank, IsNumber, Measure, NotEquals, OnLine, PercentOf, Program, Rank, RowArithmetic, SECTION, Show, Size, addresses_printed)
from engine.program.names import AFTER, ANY, DEVIATION, IS, MEDIAN, SAME, ANYWHERE, AVG, COUNT, COUNT_DISTINCT, LONGEST_RUN, ENDS, ENTITY_GROUP, ENTITY_LINE, EQ, EVERY, GE, GT, HIGHEST, LE, LT, MAX, MIN, MINUS, NE, NO, OVER, PLUS, STARTS, SUM, TIMES
from engine.program.semantics import fold as fold_text
from engine.program.store import (DATE, INTEGER, LABEL, LABEL_LENGTH, LINE, ONE_ROW, PEER_FOLD, PEER_NAME, PEER_NUMBER, REAL, ROLE, SECTION_FOLD, SECTION_PATH, SECTION_TEXT, TEXT, day_column, fold_column,
                                  number_column, peers_table, sql_table, table_columns, table_id_of, text_column, words_column)
from engine.sheet.names import RECORD
from engine.sql_ast import (Abs, Aggregate, BinaryExpr, BlankAsZero, BooleanExpr, ColumnRef, Comparison, DatePart, InPredicate, Join, Literal, LongestRun, OrderTerm, ScalarSubquery, SelectItem, SelectQuery, SQLType, Star, and_predicates, column_refs)

_FUNCTIONS = {SUM: "SUM", AVG: "AVG", MIN: "MIN", MAX: "MAX", MEDIAN: "MEDIAN", DEVIATION: "STDEV"}
VALUE = "value"
KEY = "key"
TIED_LINE = "tied_line"     # the line number a line rank's tied lines are ordered by (execute drops it from the answer)
POSITION = "position"


def _number(column: Col) -> ColumnRef:
    return ColumnRef(sql_table(column.table), number_column(column.position), SQLType.REAL)


def _fold(column: Col) -> ColumnRef:
    return ColumnRef(sql_table(column.table), SECTION_FOLD if column.position == SECTION else fold_column(column.position), SQLType.TEXT)


def _shown(column: Col) -> ColumnRef:
    return ColumnRef(sql_table(column.table), SECTION_TEXT if column.position == SECTION else text_column(column.position), SQLType.TEXT)


def _day(column: Col) -> ColumnRef:
    return ColumnRef(sql_table(column.table), day_column(column.position), SQLType.DATE)


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
    if isinstance(condition, OneOf):
        if condition.numbers:
            return BooleanExpr("OR", tuple(Comparison(_number(condition.column), "=", Literal(float(value))) for value in condition.values))
        return BooleanExpr("OR", tuple(Comparison(_fold(condition.column), "=", Literal(fold_text(value), SQLType.TEXT)) for value in condition.values))
    if isinstance(condition, NotEquals):
        return Comparison(_fold(condition.column), "!=", Literal(fold_text(condition.value), SQLType.TEXT))
    if isinstance(condition, Contains):
        return Comparison(_fold(condition.column), "LIKE", _like(condition.text, condition.anchor))
    if isinstance(condition, InPeriod):
        span, day = condition.span, _day(condition.column)
        if span.start is None:
            return Comparison(DatePart("month", day), "=", Literal(span.month))
        return BooleanExpr("AND", (Comparison(day, ">=", Literal(span.start, SQLType.DATE)), Comparison(day, "<=", Literal(span.end or span.start, SQLType.DATE))))
    if isinstance(condition, InYears):
        year = _number(condition.column)
        return BooleanExpr("AND", (Comparison(year, ">=", Literal(float(condition.first))), Comparison(year, "<=", Literal(float(condition.last)))))
    if isinstance(condition, Compare):
        return Comparison(_number(condition.column), "!=" if condition.op == NE else condition.op, Literal(float(condition.number)))
    if isinstance(condition, Between):
        number = _number(condition.column)
        return BooleanExpr("AND", (Comparison(number, ">=", Literal(float(condition.low))), Comparison(number, "<=", Literal(float(condition.high)))))
    if isinstance(condition, CompareColumns):
        return Comparison(_number(condition.column), "!=" if condition.op == NE else condition.op, _number(condition.other))
    if isinstance(condition, CompareLine):     # against the first data line the value names; that line's key kept out
        table, key, named = sql_table(condition.column.table), _fold(condition.key), Literal(fold_text(condition.value), SQLType.TEXT)
        # a line's place is its line as the sheet lists it; any other side is the column's number
        figure = ColumnRef(table, LINE, SQLType.INTEGER) if isinstance(condition.column, LineOrder) else _number(condition.column)
        anchor = ScalarSubquery(SelectQuery((SelectItem(figure, VALUE),), table, where=_where(_data_lines(condition.column.table), Comparison(key, "=", named)),
                                            order_by=(OrderTerm(ColumnRef(table, LINE, SQLType.INTEGER)),), limit=1))
        other = BooleanExpr("OR", (_is_null(key), Comparison(key, "!=", named)))
        return BooleanExpr("AND", (Comparison(figure, "!=" if condition.op == NE else condition.op, anchor), other))
    if isinstance(condition, IsBlank):
        return _is_null(_fold(condition.column), negated=condition.filled)
    if isinstance(condition, IsNumber):
        return _is_null(_number(condition.column), negated=True)
    if isinstance(condition, (InSection, OnLine)):
        return _line_condition(condition, sql_table(condition.table))
    raise TypeError(f"unknown condition: {type(condition).__name__}")


def _line_condition(condition, table: str):
    """A condition on a line's bands or label, on its own SQL table or on the melted view, which holds them too."""
    if isinstance(condition, InSection):
        path = ColumnRef(table, SECTION_PATH, SQLType.TEXT)
        return Comparison(path, "LIKE", _like(condition.text) if condition.partial else _like(f"| {condition.text} |"))
    label = ColumnRef(table, LABEL, SQLType.TEXT)
    term = Comparison(label, "LIKE", _like(condition.text)) if fold_text(condition.text) else Comparison(label, "=", Literal("", SQLType.TEXT))
    if condition.role is None:
        return term
    return BooleanExpr("AND", (term, Comparison(ColumnRef(table, ROLE, SQLType.TEXT), "=", Literal(condition.role, SQLType.TEXT))))


def _data_lines(table: str):
    """The data lines of a catalog table."""
    return Comparison(ColumnRef(sql_table(table), ROLE, SQLType.TEXT), "=", Literal(RECORD, SQLType.TEXT))


def _words(column: Col) -> ColumnRef:
    return ColumnRef(sql_table(column.table), words_column(column.position), SQLType.REAL)


def _measure(measure: Measure):
    if isinstance(measure, Col):
        return _number(measure)
    if isinstance(measure, LineOrder):
        return ColumnRef(sql_table(measure.table), LINE, SQLType.INTEGER)
    if isinstance(measure, Words):
        return _words(measure.column)
    if isinstance(measure, DateOf):
        return _day(measure.column)
    if isinstance(measure, Size):
        return Abs(_measure(measure.measure))
    if isinstance(measure, PercentOf):
        return BinaryExpr(BinaryExpr(_measure(measure.measure), TIMES, _number(measure.rate)), OVER, Literal(100.0))
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
    if isinstance(measure, LineOrder):     # every line stands somewhere
        return None
    if isinstance(measure, Words):
        return _is_null(_words(measure.column), negated=True)
    if isinstance(measure, DateOf):
        return _is_null(_day(measure.column), negated=True)
    if isinstance(measure, Size):
        return _present(measure.measure)
    if isinstance(measure, PercentOf):
        return BooleanExpr("AND", (_present(measure.measure), _is_null(_number(measure.rate), negated=True)))
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
    if program.every:     # every kept data line, its cells as the sheet wrote them, in sheet order
        select = tuple(SelectItem(_shown(column), f"shown_{at}") for at, column in enumerate(program.shown))
        return SelectQuery(select, sql_table(program.table), where=_where(_data_lines(program.table), *map(_condition, program.where)),
                           order_by=(OrderTerm(ColumnRef(sql_table(program.table), LINE, SQLType.INTEGER)),))
    if program.listing:
        keys = tuple(_fold(column) for column in program.shown)
        where = _where(None if addresses_printed(program.where) else _data_lines(program.table), *map(_condition, program.where), *(_is_null(key, negated=True) for key in keys), *_absent(program.absent))
        select = (SelectItem(keys[0], VALUE),) if len(keys) == 1 else tuple(SelectItem(key, f"{KEY}_{at}") for at, key in enumerate(keys))
        return SelectQuery(select, sql_table(program.table), where=where, group_by=keys, order_by=(OrderTerm(Aggregate("MIN", ColumnRef(sql_table(program.table), LINE, SQLType.INTEGER))),))
    columns = [_number(program.shown[0])] if scalar else [_shown(column) for column in program.shown]
    select = tuple(SelectItem(column, f"shown_{at}") for at, column in enumerate(columns))
    line = ColumnRef(sql_table(program.table), LINE, SQLType.INTEGER)
    if program.step is not None:
        # The data line nearest the first line the conditions keep, below it (after) or above it (before).
        after = program.step == AFTER
        anchor = SelectQuery((SelectItem(Aggregate("MIN", line), VALUE),), sql_table(program.table), where=_where(*map(_condition, program.where)))
        beside = Comparison(line, ">" if after else "<", ScalarSubquery(anchor))
        return SelectQuery(select, sql_table(program.table), where=_where(_data_lines(program.table), beside),
                           order_by=(OrderTerm(line, "ASC" if after else "DESC"),), limit=1)
    order = [OrderTerm(line)]
    if addresses_printed(program.where):
        order.insert(0, OrderTerm(ColumnRef(sql_table(program.table), LABEL_LENGTH, SQLType.INTEGER)))
    return SelectQuery(select, sql_table(program.table), where=_where(*map(_condition, program.where)), order_by=tuple(order), limit=1)


def _absent(absent: Absent | None) -> tuple:
    """A key no kept line of the table holding it holds: NOT IN over that table's filled keys, the key itself filled
    (so a blank key is in none, as SQL's NOT IN and an anti-join both read it)."""
    if absent is None:
        return ()
    key, by = _fold(absent.key), _fold(absent.by)
    held = SelectQuery((SelectItem(by, VALUE),), sql_table(absent.by.table), where=_where(_data_lines(absent.by.table), *map(_condition, absent.where), _is_null(by, negated=True)))
    return (_is_null(key, negated=True), InPredicate(key, held, negated=True))


def _reads_printed(measure: Measure, table) -> bool:
    return isinstance(measure, Col) and measure.position in table.printed_only_columns


def _agg_query(program: Agg, catalog: Catalog) -> SelectQuery:
    table = program.table
    if program.function == LONGEST_RUN:     # every data line stands in line; the conditions keep the run's lines
        run = LongestRun(_where(*map(_condition, program.where)), ColumnRef(sql_table(table), LINE, SQLType.INTEGER))
        return SelectQuery((SelectItem(run, VALUE),), sql_table(table), where=_data_lines(table))
    guard = None
    if program.function == COUNT:
        value = Aggregate("COUNT", Star())
    elif program.function == COUNT_DISTINCT:
        value = Aggregate("COUNT", _fold(program.measure), distinct=True)
    else:
        value = Aggregate(_FUNCTIONS[program.function], _measure(program.measure))
        if isinstance(program.measure, (RowArithmetic, PercentOf, Size, Across, Words)):
            guard = _present(program.measure)
    if program.magnitude:
        value = Abs(value)
    every_line = addresses_printed(program.where) or (program.measure is not None and _reads_printed(program.measure, catalog.table(table)))
    where = _where(None if every_line else _data_lines(table), *map(_condition, program.where), guard)
    return SelectQuery((SelectItem(value, VALUE),), sql_table(table), where=where)


def _breakdown_query(program: Breakdown, catalog: Catalog) -> SelectQuery:
    """One row per group: its key cells (and its unit) by fold, then the figure; groups in the order they first appear."""
    table = program.table
    if program.function == COUNT:
        value, guard = Aggregate("COUNT", Star()), None
    else:
        value, guard = Aggregate(_FUNCTIONS[program.function], _measure(program.measure)), _present(program.measure)
    groups = tuple(DatePart(program.grain, _day(column)) if program.grain is not None and at == 0 else _fold(column) for at, column in enumerate(program.groups))
    printed = addresses_printed(program.where) or (program.measure is not None and _reads_printed(program.measure, catalog.table(table)))
    where = _where(None if printed else _data_lines(table), *map(_condition, program.where), *(_is_null(group, negated=True) for group in groups), guard)
    select = tuple(SelectItem(group, f"{KEY}_{at}") for at, group in enumerate(groups)) + (SelectItem(value, VALUE),)
    first = OrderTerm(Aggregate("MIN", ColumnRef(sql_table(table), LINE, SQLType.INTEGER)))
    return SelectQuery(select, sql_table(table), where=where, group_by=groups, order_by=(first, *(OrderTerm(group) for group in groups)))


def _rank_query(program: Rank, catalog: Catalog) -> SelectQuery:
    table = program.table
    printed = addresses_printed(program.where) or (program.measure is not None and _reads_printed(program.measure, catalog.table(table)))
    order = _order(program.direction)
    if program.entity == ENTITY_LINE:
        value = _measure(program.measure)
        select = tuple(SelectItem(_shown(column), f"shown_{at}") for at, column in enumerate(program.shown))
        if not isinstance(program.measure, LineOrder):     # where a line stands decides which it is; it is no figure of the answer
            select += (SelectItem(value, VALUE),)
        where = _where(None if printed else _data_lines(table), *map(_condition, program.where), _present(program.measure))
        if program.count > 1:      # exactly that many lines, a tie broken by sheet order
            return SelectQuery(select, sql_table(table), where=where, order_by=(OrderTerm(value, order), OrderTerm(ColumnRef(sql_table(table), LINE, SQLType.INTEGER))), limit=program.count)
        # every tied line, in sheet order: its line number leads the row, so both programs order tied lines by it
        tied = SelectItem(ColumnRef(sql_table(table), LINE, SQLType.INTEGER), TIED_LINE)
        return SelectQuery((tied, *select), sql_table(table), where=where, order_by=(OrderTerm(value, order),), limit=1, with_ties=True)
    if program.entity == ENTITY_GROUP:     # a date key may group by its year-month, year or month of any year (grain)
        key = DatePart(program.grain, _day(program.key)) if program.grain is not None else _fold(program.key)
        if program.function == COUNT:
            value, guard = Aggregate("COUNT", Star()), None
        else:
            value, guard = Aggregate(_FUNCTIONS[program.function], _measure(program.measure)), _present(program.measure)
        where = _where(None if printed else _data_lines(table), *map(_condition, program.where), _is_null(key, negated=True), guard)
        ranked = OrderTerm(Abs(value) if program.by_size else value, order)
        if program.count > 1:      # exactly that many groups, a tie broken by the key
            return SelectQuery((SelectItem(key, KEY), SelectItem(value, VALUE)), sql_table(table), where=where, group_by=(key,), order_by=(ranked, OrderTerm(key)), limit=program.count)
        return SelectQuery((SelectItem(key, KEY), SelectItem(value, VALUE)), sql_table(table), where=where, group_by=(key,), order_by=(ranked,), limit=1, with_ties=True)
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
            joins = (Join(sql_table(table), ColumnRef(peers, LINE, SQLType.INTEGER), ColumnRef(sql_table(table), LINE, SQLType.INTEGER)),)
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


def _top_value(program: Rank, catalog: Catalog, numeric: bool, line: bool = False) -> ScalarSubquery:
    """The one value a rank of one gives a check: the top line's shown cell (its number, or its folded text; with line,
    its line number) or the top group's folded key; a tie goes to the first line in sheet order, or the first key."""
    table = program.table
    printed = addresses_printed(program.where) or (program.measure is not None and _reads_printed(program.measure, catalog.table(table)))
    order = _order(program.direction)
    if program.entity == ENTITY_LINE:
        column = program.shown[0]
        value = ColumnRef(sql_table(table), number_column(column.position), SQLType.REAL) if numeric and column.position != SECTION else _fold(column)
        if line:
            value = ColumnRef(sql_table(table), LINE, SQLType.INTEGER)
        measure = _measure(program.measure)
        where = _where(None if printed else _data_lines(table), *map(_condition, program.where), _present(program.measure))
        return ScalarSubquery(SelectQuery((SelectItem(value, VALUE),), sql_table(table), where=where, order_by=(OrderTerm(measure, order), OrderTerm(ColumnRef(sql_table(table), LINE, SQLType.INTEGER))), limit=1))
    key = _fold(program.key)
    if program.function == COUNT:
        ranked, guard = Aggregate("COUNT", Star()), None
    else:
        ranked, guard = Aggregate(_FUNCTIONS[program.function], _measure(program.measure)), _present(program.measure)
    where = _where(None if printed else _data_lines(table), *map(_condition, program.where), _is_null(key, negated=True), guard)
    return ScalarSubquery(SelectQuery((SelectItem(key, VALUE),), sql_table(table), where=where, group_by=(key,), order_by=(OrderTerm(Abs(ranked) if program.by_size else ranked, order), OrderTerm(key)), limit=1))


def _check_query(program: Check, catalog: Catalog) -> SelectQuery:
    """A check is one verdict over one-number programs on the one-row table: any is count > 0, no is count = 0,
    every is (count with the one more condition) = (count of the base lines), versus is left op (number or right), and
    a versus said in the question's words is (left > right) - (left < right)."""
    if program.quantifier == SAME:          # the same line (its number) or the same key, the top of each side
        same = BinaryExpr(_top_value(program.left, catalog, numeric=False, line=True), "=", _top_value(program.right, catalog, numeric=False, line=True))
        return SelectQuery((SelectItem(same, VALUE),), ONE_ROW)
    if program.quantifier == IS:
        left = _top_value(program.left, catalog, numeric=program.number is not None)
        right = Literal(program.number) if program.number is not None else Literal(fold_text(program.text), SQLType.TEXT)
        return SelectQuery((SelectItem(BinaryExpr(left, _VERDICT_SQL[program.op], right), VALUE),), ONE_ROW)
    left = _scalar(program.left, catalog)
    if program.quantifier == ANY:
        verdict = BinaryExpr(left, ">", Literal(0))
    elif program.quantifier == NO:
        verdict = BinaryExpr(left, "=", Literal(0))
    elif program.quantifier == EVERY:
        verdict = BinaryExpr(left, "=", _scalar(program.right, catalog))
    else:
        right = Literal(program.number) if program.number is not None else _scalar(program.right, catalog)
        if program.said is not None:        # which of the question's two words holds: +1 above, -1 below, 0 equal
            verdict = BinaryExpr(BinaryExpr(left, ">", right), "-", BinaryExpr(left, "<", right))
        else:
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
    if isinstance(program, Breakdown):
        return _breakdown_query(program, catalog)
    expression = BinaryExpr(_scalar(program.left, catalog), program.op, _scalar(program.right, catalog))
    return SelectQuery((SelectItem(Abs(expression) if program.magnitude else expression, VALUE),), ONE_ROW)


def _affinity(column: ColumnRef) -> str:
    return {SQLType.REAL: "REAL", SQLType.INTEGER: "INTEGER"}.get(column.type, "TEXT")


_KIND_TYPES = {INTEGER: SQLType.INTEGER, TEXT: SQLType.TEXT, REAL: SQLType.REAL, DATE: SQLType.DATE}


def plan_schema(query: SelectQuery, catalog: Catalog, whole_tables: bool = False) -> list[dict]:
    """The columns a query reads, as the deterministic lowering wants them. Only those, a plan's generated classes need no more, unless
    whole_tables: then every column of each table it reads, so the plans of several queries over one table declare it alike (a decomposition
    fuses its leaves' plans, engine/decomposition.py). A join's target lists its line numbers, which prove it one line per value."""
    seen: dict[tuple[str, str], dict] = {}
    for column in column_refs(query):
        seen.setdefault((column.table, column.name), {"table": column.table, "name": column.name, "affinity": _affinity(column), "is_date": column.type == SQLType.DATE})
    if whole_tables:
        for name in sorted({table for table, _column in seen}):
            for column, kind in table_columns(catalog, name):
                seen.setdefault((name, column), {"table": name, "name": column, "affinity": _affinity(ColumnRef(name, column, _KIND_TYPES[kind])), "is_date": kind == DATE})
    if isinstance(query, SelectQuery):
        for join in query.joins:
            seen[(join.table, LINE)]["values"] = list(range(len(catalog.table(table_id_of(join.table)).lines)))
    return [seen[key] for key in sorted(seen)]


def to_plan(program: Program, catalog: Catalog, slug: str = "program", whole_tables: bool = False) -> AnalysisPlan:
    """The one plan both deterministic emitters read; whole_tables: each table it reads declared whole (plan_schema)."""
    query = to_query(program, catalog)
    schema = plan_schema(query, catalog, whole_tables)
    plan = lower_calculation(slug, query, schema, ()) if isinstance(program, (Calc, Check)) else lower_select_query(slug, query, schema, ())
    # The store holds a number as the sheet's float: both programs read it as its shortest decimal, one exact reading.
    tables = tuple(replace(table, columns=tuple(replace(column, binary_float=column.type == SQLType.REAL) for column in table.columns)) for table in plan.tables)
    return replace(plan, tables=tables)


def shown_plan(plan: AnalysisPlan) -> AnalysisPlan:
    """``plan`` ending in the columns its answer shows: a line rank's tied lines leave out the line number that put them
    in sheet order (TIED_LINE; engine/program/execute.py drops it from a single answer), so a decomposition's fused plan,
    which orders its own rows, never shows or binds on it (engine/decomposition.py)."""
    from engine.deterministic.plan import ProjectedView, SelectedValue, ViewValue

    columns = plan.view_columns()[plan.output]
    if TIED_LINE not in columns:
        return plan
    shown = ProjectedView(f"{plan.output}_shown", plan.output,
                          tuple(SelectedValue(column, ViewValue(column)) for column in columns if column != TIED_LINE))
    return replace(plan, views=(*plan.views, shown), output=shown.name)


__all__ = ["KEY", "TIED_LINE", "VALUE", "plan_schema", "shown_plan", "to_plan", "to_query"]
