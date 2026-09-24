"""Literal grounding: a text literal compared with a column must be a value that column can hold.

The SQL proposer reads the schema, never the values (engine/sql_prompt.py). For "product names
bought by Lyon customers" it wrote ``purchases.customer_name = 'Lyon'``, a filter that matches no
row because 'Lyon' is a city; the deterministic search links values against the data and bound it
to ``purchases.city``. A comparison is mis-grounded when its literal occurs in no row of its own
column but does occur in another column of the request's tables. Such a query answers a different
question, so it is never eligible for selection (``TableQuery.select_query``). A literal that
occurs in no column is left alone: the question may name a value the data does not hold, and the
honest answer is then empty.

Matching is case- and whitespace-insensitive, and only equality and membership tests of a text
column against a text literal are checked. Numeric, date and pattern comparisons are out of scope.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    BooleanExpr,
    ColumnRef,
    Comparison,
    ExistsPredicate,
    InPredicate,
    Literal,
    ScalarSubquery,
    SetQuery,
    SQLType,
    SubquerySource,
)

_EQUALITY = frozenset({"=", "!=", "<>"})
_TEXT_COLUMNS = frozenset({SQLType.TEXT, SQLType.UNKNOWN})

# (physical table, column, literal text)
Binding = tuple[str, str, str]


def literal_bindings(query) -> tuple[Binding, ...]:
    """Every equality or membership test of a text column against a text literal in ``query``,
    with the column's qualifier resolved to its physical table. Columns of derived tables are
    skipped: their values are computed, not uploaded."""
    out: list[Binding] = []
    _walk_query(query, {}, out)
    return tuple(out)


def grounded_members(pool: Sequence, tables: Mapping[str, dict]) -> tuple[bool, ...]:
    """Per pool member (a ``ScoredQuery``): False when any of its literal tests is mis-grounded
    against ``tables`` (name -> {"columns", "rows"}), the request's tables."""
    bindings = [literal_bindings(candidate.query) for candidate in pool]
    wanted = {_fold(text) for member in bindings for _, _, text in member}
    if not wanted:
        return (True,) * len(pool)
    holders = _value_holders(tables, wanted)
    return tuple(
        all(_grounded(binding, holders) for binding in member) for member in bindings
    )


def _grounded(binding: Binding, holders: Mapping[str, set[tuple[str, str]]]) -> bool:
    table, column, text = binding
    places = holders.get(_fold(text))
    return not places or (table, column) in places


def _value_holders(tables: Mapping[str, dict], wanted: set[str]) -> dict[str, set[tuple[str, str]]]:
    """Folded literal -> the (table, column) pairs whose values contain it."""
    holders: dict[str, set[tuple[str, str]]] = {}
    for name, table in tables.items():
        columns = table["columns"]
        for row in table["rows"]:
            for column, value in zip(columns, row):
                if value is None:
                    continue
                folded = _fold(value)
                if folded in wanted:
                    holders.setdefault(folded, set()).add((name, column))
    return holders


def _fold(value) -> str:
    return " ".join(str(value).split()).casefold()


def _walk_query(query, outer: Mapping[str, str | None], out: list[Binding]) -> None:
    if isinstance(query, SetQuery):
        _walk_query(query.left, outer, out)
        _walk_query(query.right, outer, out)
        return
    scope = dict(outer)                      # an inner qualifier shadows an outer one
    if isinstance(query.from_table, SubquerySource):
        _walk_query(query.from_table.query, {}, out)
        scope[query.from_table.alias] = None
    else:
        scope[query.from_alias or query.from_table] = query.from_table
    for join in query.joins:
        scope[join.alias or join.table] = join.table
    for item in query.select:
        _walk_expr(item.expression, scope, out)
    for term in query.order_by:
        _walk_expr(term.expression, scope, out)
    _walk_predicate(query.where, scope, out)
    _walk_predicate(query.having, scope, out)


def _walk_expr(expr, scope: Mapping[str, str | None], out: list[Binding]) -> None:
    if isinstance(expr, ScalarSubquery):
        _walk_query(expr.query, scope, out)
    elif isinstance(expr, Aggregate):
        _walk_expr(expr.operand, scope, out)
    elif isinstance(expr, BinaryExpr):
        _walk_expr(expr.left, scope, out)
        _walk_expr(expr.right, scope, out)


def _walk_predicate(predicate, scope: Mapping[str, str | None], out: list[Binding]) -> None:
    if predicate is None:
        return
    if isinstance(predicate, BooleanExpr):
        for term in predicate.terms:
            _walk_predicate(term, scope, out)
    elif isinstance(predicate, ExistsPredicate):
        _walk_query(predicate.query, scope, out)
    elif isinstance(predicate, InPredicate):
        _walk_expr(predicate.left, scope, out)
        if isinstance(predicate.source, tuple):
            for value in predicate.source:
                _bind(predicate.left, value, scope, out)
        else:
            _walk_query(predicate.source, scope, out)
    elif isinstance(predicate, Comparison):
        _walk_expr(predicate.left, scope, out)
        _walk_expr(predicate.right, scope, out)
        if predicate.operator in _EQUALITY:
            _bind(predicate.left, predicate.right, scope, out)


def _bind(column, literal, scope: Mapping[str, str | None], out: list[Binding]) -> None:
    if not (isinstance(column, ColumnRef) and isinstance(literal, Literal)):
        return
    if column.type not in _TEXT_COLUMNS or not isinstance(literal.value, str):
        return
    if not literal.value.strip():
        return
    table = scope.get(column.table)
    if table is not None:
        out.append((table, column.name, literal.value))
