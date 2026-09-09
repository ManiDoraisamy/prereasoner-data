"""Evaluate a validated typed AST over hydrated Python objects.

Written from SQL semantics — not from `deterministic/emitter/sql/render.py`. Where the two
emitters must agree, they agree because both are faithful readings of the same tree, not because
one was copied from the other.

The semantics implemented here, and why each one matters:

* **Three-valued logic.** `WHERE`/`HAVING`/`ON` keep a row only when the predicate is TRUE.
  UNKNOWN (any comparison touching NULL) drops the row exactly as FALSE does, but `NOT UNKNOWN`
  is still UNKNOWN, so `NOT IN` over a NULL-bearing list matches nothing.
* **Aggregates skip NULLs.** `COUNT(*)` counts rows; `COUNT(col)` counts non-NULL values;
  `SUM`/`AVG`/`MIN`/`MAX` over no surviving value are NULL, while `COUNT` over none is 0.
* **A bare aggregate always returns one row**, even over an empty table — but a query with
  `GROUP BY` over an empty table returns none.
* **SQLite's cross-type ordering**, NULL < number < text, with NULLs first ascending.
* **Set operations deduplicate.** The AST's `SET_OPERATORS` has no `ALL` variant.

Arithmetic and numeric comparison come from `engine/numeric.py` — the same kernel
`register_sqlite_decimal` installs into SQLite — so a fractional result cannot disagree merely
because one side used binary floats.
"""
from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Iterable, Sequence

from engine.numeric import (
    DecimalAggregate,
    DecimalAverage,
    DecimalMaximum,
    DecimalMinimum,
    decimal_arg,
    decimal_binary,
    decimal_compare,
    parse_decimal,
)
from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    BooleanExpr,
    ColumnRef,
    ExistsPredicate,
    InPredicate,
    Literal,
    Predicate,
    Query,
    ScalarExpr,
    ScalarSubquery,
    SelectQuery,
    SetQuery,
    Star,
    SubquerySource,
)

# A row is keyed by (qualifier, column) so it lines up one-to-one with ColumnRef(table, name).
Row = dict[tuple[str, str], Any]

_UNKNOWN = object()          # SQL's third truth value, distinct from both True and False


class EvaluationError(RuntimeError):
    """The evaluator met a node it does not implement. Never a data-dependent failure."""


# --------------------------------------------------------------------------- values


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


def _sort_key(value: Any) -> tuple[int, Decimal | str]:
    """SQLite's storage-class ordering: NULL < INTEGER/REAL < TEXT."""
    if value is None:
        return (0, Decimal(0))
    if _is_number(value):
        return (1, decimal_arg(value) or Decimal(0))
    return (2, str(value))


def _compare_values(left: Any, right: Any) -> int | None:
    """Three-valued comparison. None means UNKNOWN (a NULL operand)."""
    if left is None or right is None:
        return None
    if _is_number(left) and _is_number(right):
        return decimal_compare(left, right)
    if _is_number(left) != _is_number(right):
        # Cross-class comparison never matches; order by storage class, as SQLite does.
        return (_sort_key(left) > _sort_key(right)) - (_sort_key(left) < _sort_key(right))
    a, b = str(left), str(right)
    return (a > b) - (a < b)


def _like(value: Any, pattern: Any) -> bool | None:
    if value is None or pattern is None:
        return None
    regex = "".join(
        ".*" if ch == "%" else "." if ch == "_" else re.escape(ch)
        for ch in str(pattern)
    )
    return re.fullmatch(regex, str(value), flags=re.IGNORECASE | re.DOTALL) is not None


# --------------------------------------------------------------------------- expressions


def _column(row: Row, ref: ColumnRef) -> Any:
    try:
        return row[(ref.table, ref.name)]
    except KeyError as exc:                      # a validated tree cannot reach this
        raise EvaluationError(f"column not in scope: {ref.table}.{ref.name}") from exc


def _scalar(expr: ScalarExpr, row: Row, ctx: "_Context") -> Any:
    if isinstance(expr, ColumnRef):
        return _column(row, expr)
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, Star):
        raise EvaluationError("* is not a scalar expression")
    if isinstance(expr, BinaryExpr):
        left, right = _scalar(expr.left, row, ctx), _scalar(expr.right, row, ctx)
        if left is None or right is None:
            return None
        result = decimal_binary(expr.operator, left, right)   # None on divide-by-zero, as NULLIF does
        return None if result is None else parse_decimal(result, enforce_input_bounds=False)
    if isinstance(expr, ScalarSubquery):
        rows = _run(expr.query, ctx.with_outer(row))
        if not rows:
            return None
        return rows[0][0]
    if isinstance(expr, Aggregate):
        raise EvaluationError("aggregate evaluated outside a group")
    raise EvaluationError(f"unsupported expression: {type(expr).__name__}")


def _aggregate(expr: Aggregate, group: Sequence[Row], ctx: "_Context") -> Any:
    if isinstance(expr.operand, Star):
        return len(group)                                    # COUNT(*) counts rows, NULLs included
    values = [_scalar(expr.operand, row, ctx) for row in group]
    values = [value for value in values if value is not None]
    if expr.distinct:
        seen, unique = set(), []
        for value in values:
            key = _sort_key(value)
            if key not in seen:
                seen.add(key)
                unique.append(value)
        values = unique
    if expr.function == "COUNT":
        return len(values)
    if not values:
        return None                                          # SUM/AVG/MIN/MAX over nothing is NULL
    if all(_is_number(value) for value in values):
        accumulator = {"SUM": DecimalAggregate, "AVG": DecimalAverage,
                       "MIN": DecimalMinimum, "MAX": DecimalMaximum}[expr.function]()
        for value in values:
            accumulator.step(value)
        result = accumulator.finalize()
        return None if result is None else parse_decimal(result, enforce_input_bounds=False)
    if expr.function == "MIN":
        return min(values, key=_sort_key)
    if expr.function == "MAX":
        return max(values, key=_sort_key)
    raise EvaluationError(f"{expr.function} over non-numeric values")


def _has_aggregate(expr: ScalarExpr) -> bool:
    if isinstance(expr, BinaryExpr):
        return _has_aggregate(expr.left) or _has_aggregate(expr.right)
    return isinstance(expr, Aggregate)


def _grouped_scalar(expr: ScalarExpr, group: Sequence[Row], ctx: "_Context") -> Any:
    """Evaluate a select/order/having expression against one group.

    A bare column inside a grouped query is a grouping key, so any row of the group carries it.
    """
    if isinstance(expr, Aggregate):
        return _aggregate(expr, group, ctx)
    if isinstance(expr, BinaryExpr) and _has_aggregate(expr):
        left = _grouped_scalar(expr.left, group, ctx)
        right = _grouped_scalar(expr.right, group, ctx)
        if left is None or right is None:
            return None
        result = decimal_binary(expr.operator, left, right)
        return None if result is None else parse_decimal(result, enforce_input_bounds=False)
    if not group:
        return None
    return _scalar(expr, group[0], ctx)


# --------------------------------------------------------------------------- predicates


def _truth(predicate: Predicate | None, row: Row, ctx: "_Context",
           group: Sequence[Row] | None = None) -> Any:
    """Return True, False, or _UNKNOWN."""
    if predicate is None:
        return True

    def value_of(expr: ScalarExpr) -> Any:
        return _grouped_scalar(expr, group, ctx) if group is not None else _scalar(expr, row, ctx)

    if isinstance(predicate, BooleanExpr):
        results = [_truth(term, row, ctx, group) for term in predicate.terms]
        if predicate.operator == "AND":
            if any(result is False for result in results):
                return False
            return _UNKNOWN if any(result is _UNKNOWN for result in results) else True
        if any(result is True for result in results):
            return True
        return _UNKNOWN if any(result is _UNKNOWN for result in results) else False

    if isinstance(predicate, ExistsPredicate):
        found = bool(_run(predicate.query, ctx.with_outer(row)))
        return found != predicate.negated                    # EXISTS is never UNKNOWN

    if isinstance(predicate, InPredicate):
        left = value_of(predicate.left)
        if isinstance(predicate.source, tuple):
            candidates = [value_of(item) for item in predicate.source]
        else:
            candidates = [values[0] for values in _run(predicate.source, ctx.with_outer(row))]
        if left is None:
            return _UNKNOWN
        matched = any(_compare_values(left, candidate) == 0 for candidate in candidates)
        if matched:
            return not predicate.negated
        # No match: NULL on the right leaves membership undecidable (SQL's NOT IN trap).
        if any(candidate is None for candidate in candidates):
            return _UNKNOWN
        return predicate.negated

    left, right = value_of(predicate.left), value_of(predicate.right)
    operator = predicate.operator
    if operator in {"IS", "IS NOT"}:                          # IS is null-safe by definition
        same = (left is None and right is None) or (
            left is not None and right is not None and _compare_values(left, right) == 0
        )
        return same if operator == "IS" else not same
    if operator in {"LIKE", "NOT LIKE"}:
        matched = _like(left, right)
        if matched is None:
            return _UNKNOWN
        return matched if operator == "LIKE" else not matched
    order = _compare_values(left, right)
    if order is None:
        return _UNKNOWN
    return {"=": order == 0, "!=": order != 0, "<>": order != 0, ">": order > 0,
            "<": order < 0, ">=": order >= 0, "<=": order <= 0}[operator]


def _keeps(predicate: Predicate | None, row: Row, ctx: "_Context",
           group: Sequence[Row] | None = None) -> bool:
    return _truth(predicate, row, ctx, group) is True         # UNKNOWN drops the row, like FALSE


# --------------------------------------------------------------------------- query


class _Context:
    """Hydrated tables plus the correlated row a subquery may see."""

    __slots__ = ("tables", "outer", "notes")

    def __init__(self, tables: dict[str, list[Row]], outer: Row | None = None,
                 notes: dict | None = None) -> None:
        self.tables = tables
        self.outer = outer
        # Shared by reference so a note raised inside a subquery reaches the caller.
        self.notes = {"ambiguous_limit": False} if notes is None else notes

    def with_outer(self, row: Row) -> "_Context":
        merged = dict(self.outer or {})
        merged.update(row)
        return _Context(self.tables, merged, self.notes)


def output_names(query: Query) -> list[str]:
    """The column names a query projects — what a derived table exposes to its parent."""
    if isinstance(query, SetQuery):
        return output_names(query.left)
    names = []
    for index, item in enumerate(query.select):
        if item.alias is not None:
            names.append(item.alias)
        elif isinstance(item.expression, ColumnRef):
            names.append(item.expression.name)
        else:
            names.append(f"column_{index + 1}")
    return names


def _source_rows(query: SelectQuery, ctx: "_Context") -> tuple[str, list[Row]]:
    if isinstance(query.from_table, SubquerySource):
        alias = query.from_table.alias
        names = output_names(query.from_table.query)
        rows = [dict(zip(((alias, name) for name in names), values))
                for values in _run(query.from_table.query, ctx)]
        return alias, rows
    qualifier = query.from_alias or query.from_table
    if query.from_table not in ctx.tables:
        raise EvaluationError(f"unknown table: {query.from_table}")
    rows = [{(qualifier, column): value for (_, column), value in row.items()}
            for row in ctx.tables[query.from_table]]
    return qualifier, rows


def _apply_joins(query: SelectQuery, rows: list[Row], ctx: "_Context") -> list[Row]:
    for join in query.joins:
        qualifier = join.alias or join.table
        if join.table not in ctx.tables:
            raise EvaluationError(f"unknown table: {join.table}")
        right_rows = [{(qualifier, column): value for (_, column), value in row.items()}
                      for row in ctx.tables[join.table]]
        null_row = {key: None for key in (right_rows[0] if right_rows else {})}
        combined: list[Row] = []
        for left_row in rows:
            matched = False
            for right_row in right_rows:
                candidate = {**left_row, **right_row}
                if all(_compare_values(_scalar(left, candidate, ctx),
                                       _scalar(right, candidate, ctx)) == 0
                       for left, right in join.predicates):
                    combined.append(candidate)
                    matched = True
            if not matched and join.kind == "LEFT":
                combined.append({**left_row, **null_row})
        rows = combined
    return rows


def _group(query: SelectQuery, rows: list[Row], ctx: "_Context") -> list[list[Row]]:
    if query.group_by:
        buckets: dict[tuple, list[Row]] = {}
        for row in rows:                                      # insertion order: first appearance
            key = tuple(_sort_key(_scalar(column, row, ctx)) for column in query.group_by)
            buckets.setdefault(key, []).append(row)
        return list(buckets.values())
    # A bare aggregate collapses everything to exactly one row, even with no input rows at all.
    return [rows]


def _is_grouped(query: SelectQuery) -> bool:
    return bool(query.group_by) or any(
        _has_aggregate(item.expression) for item in query.select
    ) or any(_has_aggregate(term.expression) for term in query.order_by) or _predicate_aggregates(query.having)


def _predicate_aggregates(predicate: Predicate | None) -> bool:
    if predicate is None:
        return False
    if isinstance(predicate, BooleanExpr):
        return any(_predicate_aggregates(term) for term in predicate.terms)
    if isinstance(predicate, ExistsPredicate):
        return False
    if isinstance(predicate, InPredicate):
        return _has_aggregate(predicate.left)
    return _has_aggregate(predicate.left) or _has_aggregate(predicate.right)


def _project(items, group: Sequence[Row], ctx: "_Context",
             star_keys: Sequence[tuple[str, str]], *, grouped: bool) -> tuple:
    """Build one output row. `SELECT *` expands to every column of the query's own relations."""
    values: list[Any] = []
    for item in items:
        if isinstance(item.expression, Star):
            values.extend(group[0][key] for key in star_keys)
        elif grouped:
            values.append(_grouped_scalar(item.expression, group, ctx))
        else:
            values.append(_scalar(item.expression, group[0], ctx))
    return tuple(values)


def _run(query: Query, ctx: "_Context") -> list[tuple]:
    if isinstance(query, SetQuery):
        left, right = _run(query.left, ctx), _run(query.right, ctx)
        left_keys = [tuple(_sort_key(value) for value in row) for row in left]
        right_keys = {tuple(_sort_key(value) for value in row) for row in right}
        if query.operator == "UNION":
            merged, seen = [], set()
            for row, key in list(zip(left, left_keys)) + [
                (row, tuple(_sort_key(value) for value in row)) for row in right
            ]:
                if key not in seen:
                    seen.add(key)
                    merged.append(row)
            return merged
        keep = (lambda key: key in right_keys) if query.operator == "INTERSECT" else (
            lambda key: key not in right_keys)
        result, seen = [], set()
        for row, key in zip(left, left_keys):
            if keep(key) and key not in seen:
                seen.add(key)
                result.append(row)
        return result

    qualifier, rows = _source_rows(query, ctx)
    rows = _apply_joins(query, rows, ctx)
    # `SELECT *` spans this query's own relations only — never the correlated row merged in below.
    star_keys = list(rows[0].keys()) if rows else []
    if ctx.outer:                                             # make correlated columns visible
        rows = [{**ctx.outer, **row} for row in rows]
    rows = [row for row in rows if _keeps(query.where, row, ctx)]

    if _is_grouped(query):
        groups = _group(query, rows, ctx)
        groups = [group for group in groups if _keeps(query.having, group[0] if group else {}, ctx, group)]
        projected = [_project(query.select, group, ctx, star_keys, grouped=True) for group in groups]
        order_keys = [[_grouped_scalar(term.expression, group, ctx) for term in query.order_by]
                      for group in groups]
    else:
        projected = [_project(query.select, [row], ctx, star_keys, grouped=False) for row in rows]
        order_keys = [[_scalar(term.expression, row, ctx) for term in query.order_by]
                      for row in rows]

    paired = list(zip(projected, order_keys))
    if query.distinct:
        seen, unique = set(), []
        for values, keys in paired:
            key = tuple(_sort_key(value) for value in values)
            if key not in seen:
                seen.add(key)
                unique.append((values, keys))
        paired = unique

    for index in reversed(range(len(query.order_by))):        # stable sorts, least-significant first
        descending = query.order_by[index].direction == "DESC"
        paired.sort(key=lambda item: _sort_key(item[1][index]), reverse=descending)

    result = [values for values, _ in paired]
    if query.limit is not None:
        # A LIMIT that cuts through tied order keys returns an unspecified row: SQL does not say
        # which. Record it so a caller comparing two engines can decline to treat it as evidence.
        if len(paired) > query.limit and query.order_by:
            boundary = [_sort_key(value) for value in paired[query.limit - 1][1]]
            following = [_sort_key(value) for value in paired[query.limit][1]]
            if boundary == following:
                ctx.notes["ambiguous_limit"] = True
        result = result[:query.limit]
    return result


def evaluate_query(query: Query, tables: dict[str, list[Row]]) -> tuple[list[tuple], dict]:
    """Run a validated AST over hydrated rows; return the projected rows and what SQL left open.

    `tables` maps a table name to its rows, each row keyed by ``(table_name, column_name)`` — the
    shape `deterministic.emitter.py.hydrate.hydrate` produces.

    `notes["ambiguous_limit"]` is True when a `LIMIT` cut through tied `ORDER BY` keys, so these
    rows are one valid answer among several. Two correct engines may legitimately differ there,
    which makes such a query useless as differential evidence. The notes ride along with every
    result rather than living behind a second entry point, so no caller can forget to ask.
    """
    ctx = _Context(tables)
    return _run(query, ctx), dict(ctx.notes)
