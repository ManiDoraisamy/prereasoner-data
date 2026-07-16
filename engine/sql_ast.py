"""Typed, immutable SQL AST for deterministic SQL search.

The tree is recursive: SELECT blocks may contain scalar and membership subqueries,
compound queries, aliased self-joins, and derived-table sources.  Validation is
scope-aware and always runs before rendering.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from numbers import Real
from typing import Any, Iterable, TypeAlias


class SQLType(str, Enum):
    UNKNOWN = "unknown"
    TEXT = "text"
    INTEGER = "integer"
    REAL = "real"
    BOOLEAN = "boolean"
    DATE = "date"

    @property
    def numeric(self) -> bool:
        return self in (SQLType.INTEGER, SQLType.REAL)


@dataclass(frozen=True, order=True)
class ColumnRef:
    # ``table`` is the SQL qualifier.  It is a physical table name for ordinary
    # queries and an alias inside aliased/self-join or derived-table scopes.
    table: str
    name: str
    type: SQLType = SQLType.UNKNOWN


@dataclass(frozen=True)
class Star:
    pass


@dataclass(frozen=True)
class Literal:
    value: Any
    type: SQLType = SQLType.UNKNOWN


@dataclass(frozen=True)
class Aggregate:
    function: str
    operand: ColumnRef | Star
    distinct: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "function", self.function.upper())


@dataclass(frozen=True)
class ScalarSubquery:
    query: "Query"


ScalarExpr: TypeAlias = ColumnRef | Star | Literal | Aggregate | ScalarSubquery


@dataclass(frozen=True)
class Comparison:
    left: ColumnRef | Aggregate | ScalarSubquery
    operator: str
    right: ColumnRef | Literal | Aggregate | ScalarSubquery

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", self.operator.upper())


@dataclass(frozen=True)
class BooleanExpr:
    operator: str
    terms: tuple["Predicate", ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", self.operator.upper())


@dataclass(frozen=True)
class InPredicate:
    left: ColumnRef | Aggregate
    source: "Query | tuple[ScalarExpr, ...]"
    negated: bool = False


@dataclass(frozen=True)
class ExistsPredicate:
    query: "Query"
    negated: bool = False


Predicate: TypeAlias = Comparison | BooleanExpr | InPredicate | ExistsPredicate


@dataclass(frozen=True)
class SelectItem:
    expression: ScalarExpr
    alias: str | None = None


@dataclass(frozen=True)
class Join:
    table: str
    left: ColumnRef
    right: ColumnRef
    kind: str = "INNER"
    alias: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", self.kind.upper())


@dataclass(frozen=True)
class OrderTerm:
    expression: ColumnRef | Aggregate
    direction: str = "ASC"

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", self.direction.upper())


@dataclass(frozen=True)
class SubquerySource:
    query: "Query"
    alias: str


@dataclass(frozen=True)
class SelectQuery:
    select: tuple[SelectItem, ...]
    from_table: str | SubquerySource
    joins: tuple[Join, ...] = ()
    where: Predicate | None = None
    group_by: tuple[ColumnRef, ...] = ()
    having: Predicate | None = None
    order_by: tuple[OrderTerm, ...] = ()
    limit: int | None = None
    distinct: bool = False
    from_alias: str | None = None

    def referenced_tables(self) -> frozenset[str]:
        out = set()
        if isinstance(self.from_table, SubquerySource):
            out.update(self.from_table.query.referenced_tables())
        else:
            out.add(self.from_table)
        out.update(join.table for join in self.joins)
        for item in self.select:
            out.update(_expr_query_tables(item.expression))
        for term in self.order_by:
            out.update(_expr_query_tables(term.expression))
        out.update(_predicate_query_tables(self.where))
        out.update(_predicate_query_tables(self.having))
        return frozenset(out)


@dataclass(frozen=True)
class SetQuery:
    left: "Query"
    operator: str
    right: "Query"

    def __post_init__(self) -> None:
        object.__setattr__(self, "operator", self.operator.upper())

    def referenced_tables(self) -> frozenset[str]:
        return self.left.referenced_tables() | self.right.referenced_tables()


Query: TypeAlias = SelectQuery | SetQuery


class ASTValidationError(ValueError):
    pass


AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})
COMPARISONS = frozenset({"=", "!=", "<>", ">", "<", ">=", "<=", "LIKE", "NOT LIKE", "IS", "IS NOT"})
SET_OPERATORS = frozenset({"UNION", "INTERSECT", "EXCEPT"})


def expression_type(expr: ScalarExpr) -> SQLType:
    if isinstance(expr, ColumnRef):
        return expr.type
    if isinstance(expr, Literal):
        return expr.type
    if isinstance(expr, Aggregate):
        if expr.function == "COUNT":
            return SQLType.INTEGER
        if expr.function == "AVG":
            return SQLType.REAL
        return expression_type(expr.operand)
    return SQLType.UNKNOWN


def validate_query(query: Query) -> None:
    _validate_query(query, frozenset())


def _validate_query(query: Query, outer_scope: frozenset[str]) -> None:
    if isinstance(query, SetQuery):
        if query.operator not in SET_OPERATORS:
            raise ASTValidationError(f"unsupported set operator: {query.operator}")
        _validate_query(query.left, outer_scope)
        _validate_query(query.right, outer_scope)
        if _has_star_projection(query.left) or _has_star_projection(query.right):
            raise ASTValidationError("set operands cannot use SELECT * without known output arity")
        if _output_arity(query.left) != _output_arity(query.right):
            raise ASTValidationError("set operands must project the same number of columns")
        for operand in (query.left, query.right):
            if isinstance(operand, SelectQuery) and (operand.order_by or operand.limit is not None):
                raise ASTValidationError("ORDER BY/LIMIT belong outside compound-query operands")
        return

    if not query.select:
        raise ASTValidationError("SELECT must contain at least one item")
    if query.limit is not None and query.limit <= 0:
        raise ASTValidationError("LIMIT must be positive")
    if isinstance(query.from_table, SubquerySource) and query.from_alias is not None:
        raise ASTValidationError("derived-table aliases belong on SubquerySource")

    if isinstance(query.from_table, SubquerySource):
        if not query.from_table.alias:
            raise ASTValidationError("derived tables require an alias")
        _validate_query(query.from_table.query, frozenset())
        root = query.from_table.alias
    else:
        root = query.from_alias or query.from_table
    joined = {root}

    for join in query.joins:
        if join.kind not in {"INNER", "LEFT"}:
            raise ASTValidationError(f"unsupported join kind: {join.kind}")
        qualifier = join.alias or join.table
        if qualifier in joined:
            raise ASTValidationError(f"duplicate table qualifier: {qualifier}")
        edge_tables = {join.left.table, join.right.table}
        if qualifier not in edge_tables:
            raise ASTValidationError("joined table alias is not present in its ON edge")
        if not (edge_tables & joined):
            raise ASTValidationError("joins must add one table to the connected FROM graph")
        unknown = edge_tables - joined - {qualifier} - set(outer_scope)
        if unknown:
            raise ASTValidationError(f"join edge references unknown qualifiers: {sorted(unknown)}")
        joined.add(qualifier)

    visible = frozenset(joined) | outer_scope
    for item in query.select:
        _validate_expr(item.expression, visible)
    for term in query.order_by:
        _validate_expr(term.expression, visible)
        if term.direction not in {"ASC", "DESC"}:
            raise ASTValidationError(f"unsupported order direction: {term.direction}")
    _validate_predicate(query.where, visible)
    _validate_predicate(query.having, visible)
    if _predicate_has_aggregate(query.where):
        raise ASTValidationError("aggregate predicates belong in HAVING, not WHERE")

    referenced = set()
    for item in query.select:
        referenced.update(_expr_tables(item.expression))
    referenced.update(column.table for column in query.group_by)
    for term in query.order_by:
        referenced.update(_expr_tables(term.expression))
    referenced.update(_predicate_tables(query.where))
    referenced.update(_predicate_tables(query.having))
    missing = referenced - set(visible)
    if missing:
        raise ASTValidationError(f"columns reference tables outside visible scope: {sorted(missing)}")

    grouped_query = (
        bool(query.group_by)
        or any(_expr_has_aggregate(item.expression) for item in query.select)
        or any(_expr_has_aggregate(term.expression) for term in query.order_by)
        or _predicate_has_aggregate(query.having)
    )
    if grouped_query:
        groups = set(query.group_by)
        ungrouped = [item.expression for item in query.select
                     if isinstance(item.expression, ColumnRef) and item.expression not in groups]
        if ungrouped:
            names = [f"{column.table}.{column.name}" for column in ungrouped]
            raise ASTValidationError(f"non-aggregate projections must be grouped: {names}")
        unordered = [term.expression for term in query.order_by
                     if isinstance(term.expression, ColumnRef) and term.expression not in groups]
        if unordered:
            names = [f"{column.table}.{column.name}" for column in unordered]
            raise ASTValidationError(f"non-aggregate ordering columns must be grouped: {names}")


def render_query(query: Query) -> str:
    validate_query(query)
    return _render_query(query)


def _render_query(query: Query) -> str:
    if isinstance(query, SetQuery):
        return f"{_render_query(query.left)} {query.operator} {_render_query(query.right)}"

    select = ", ".join(_render_select(item) for item in query.select)
    sql = "SELECT " + ("DISTINCT " if query.distinct else "") + select
    if isinstance(query.from_table, SubquerySource):
        sql += f" FROM ({_render_query(query.from_table.query)}) AS {_qident(query.from_table.alias)}"
    else:
        sql += f" FROM {_qident(query.from_table)}"
        if query.from_alias is not None:
            sql += f" AS {_qident(query.from_alias)}"
    for join in query.joins:
        prefix = "LEFT JOIN" if join.kind == "LEFT" else "JOIN"
        sql += f" {prefix} {_qident(join.table)}"
        if join.alias is not None:
            sql += f" AS {_qident(join.alias)}"
        sql += f" ON {_render_expr(join.left)} = {_render_expr(join.right)}"
    if query.where is not None:
        sql += " WHERE " + _render_predicate(query.where)
    if query.group_by:
        sql += " GROUP BY " + ", ".join(_render_expr(column) for column in query.group_by)
    if query.having is not None:
        sql += " HAVING " + _render_predicate(query.having)
    if query.order_by:
        sql += " ORDER BY " + ", ".join(
            f"{_render_expr(term.expression)} {term.direction}" for term in query.order_by
        )
    if query.limit is not None:
        sql += f" LIMIT {query.limit}"
    return sql


def and_predicates(predicates: Iterable[Predicate]) -> Predicate | None:
    terms = tuple(predicate for predicate in predicates if predicate is not None)
    if not terms:
        return None
    if len(terms) == 1:
        return terms[0]
    return BooleanExpr("AND", terms)


def _validate_expr(expr: ScalarExpr, visible: frozenset[str]) -> None:
    if isinstance(expr, ScalarSubquery):
        _validate_query(expr.query, visible)
        if _output_arity(expr.query) != 1:
            raise ASTValidationError("scalar subqueries must project exactly one column")
        return
    if isinstance(expr, Aggregate):
        if expr.function not in AGGREGATES:
            raise ASTValidationError(f"unsupported aggregate: {expr.function}")
        if isinstance(expr.operand, Star) and expr.function != "COUNT":
            raise ASTValidationError(f"{expr.function}(*) is not valid")
        if isinstance(expr.operand, Star) and expr.distinct:
            raise ASTValidationError("COUNT(DISTINCT *) is not valid")
        if (expr.function in {"SUM", "AVG"} and isinstance(expr.operand, ColumnRef)
                and not expr.operand.type.numeric):
            raise ASTValidationError(f"{expr.function} requires a numeric column")
        return
    if isinstance(expr, Literal):
        value = expr.value
        if expr.type.numeric and value is not None and not (
            isinstance(value, Real) and not isinstance(value, bool)
        ):
            raise ASTValidationError("numeric literals require a numeric value")
        if (expr.type == SQLType.BOOLEAN and value is not None
                and not isinstance(value, bool)
                and not (isinstance(value, int) and value in (0, 1))):
            raise ASTValidationError("boolean literals require true, false, 0, or 1")


def _validate_predicate(predicate: Predicate | None, visible: frozenset[str]) -> None:
    if predicate is None:
        return
    if isinstance(predicate, BooleanExpr):
        if predicate.operator not in {"AND", "OR"} or len(predicate.terms) < 2:
            raise ASTValidationError("boolean expressions require AND/OR and at least two terms")
        for term in predicate.terms:
            _validate_predicate(term, visible)
        return
    if isinstance(predicate, ExistsPredicate):
        _validate_query(predicate.query, visible)
        return
    if isinstance(predicate, InPredicate):
        _validate_expr(predicate.left, visible)
        if isinstance(predicate.source, tuple):
            if not predicate.source:
                raise ASTValidationError("IN lists cannot be empty")
            for expression in predicate.source:
                _validate_expr(expression, visible)
        else:
            _validate_query(predicate.source, visible)
            if _output_arity(predicate.source) != 1:
                raise ASTValidationError("IN subqueries must project exactly one column")
        return
    if predicate.operator not in COMPARISONS:
        raise ASTValidationError(f"unsupported comparison: {predicate.operator}")
    _validate_expr(predicate.left, visible)
    _validate_expr(predicate.right, visible)
    left_type, right_type = expression_type(predicate.left), expression_type(predicate.right)
    if predicate.operator in {">", "<", ">=", "<="}:
        comparable = ((left_type.numeric and right_type.numeric) or left_type == right_type
                      or SQLType.UNKNOWN in {left_type, right_type}
                      or {left_type, right_type} <= {SQLType.TEXT, SQLType.DATE})
        if not comparable:
            raise ASTValidationError(
                f"incompatible comparison types: {left_type.value} {predicate.operator} {right_type.value}"
            )


def _render_select(item: SelectItem) -> str:
    value = _render_expr(item.expression)
    return value if item.alias is None else f"{value} AS {_qident(item.alias)}"


def _render_expr(expr: ScalarExpr) -> str:
    if isinstance(expr, ColumnRef):
        return f"{_qident(expr.table)}.{_qident(expr.name)}"
    if isinstance(expr, Star):
        return "*"
    if isinstance(expr, Literal):
        return _render_literal(expr)
    if isinstance(expr, Aggregate):
        distinct = "DISTINCT " if expr.distinct else ""
        return f"{expr.function}({distinct}{_render_expr(expr.operand)})"
    if isinstance(expr, ScalarSubquery):
        return f"({_render_query(expr.query)})"
    raise TypeError(f"unsupported expression: {type(expr).__name__}")


def _render_predicate(predicate: Predicate) -> str:
    if isinstance(predicate, BooleanExpr):
        return "(" + f" {predicate.operator} ".join(_render_predicate(term) for term in predicate.terms) + ")"
    if isinstance(predicate, ExistsPredicate):
        prefix = "NOT " if predicate.negated else ""
        return f"{prefix}EXISTS ({_render_query(predicate.query)})"
    if isinstance(predicate, InPredicate):
        prefix = "NOT " if predicate.negated else ""
        if isinstance(predicate.source, tuple):
            source = ", ".join(_render_expr(expression) for expression in predicate.source)
        else:
            source = _render_query(predicate.source)
        return f"{_render_expr(predicate.left)} {prefix}IN ({source})"
    return f"{_render_expr(predicate.left)} {predicate.operator} {_render_expr(predicate.right)}"


def _render_literal(literal: Literal) -> str:
    value = literal.value
    if value is None:
        return "NULL"
    if literal.type == SQLType.BOOLEAN or isinstance(value, bool):
        return "1" if bool(value) else "0"
    if isinstance(value, Real) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ASTValidationError("non-finite numeric literal")
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _qident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _output_arity(query: Query) -> int:
    if isinstance(query, SetQuery):
        return _output_arity(query.left)
    return len(query.select)


def _has_star_projection(query: Query) -> bool:
    if isinstance(query, SetQuery):
        return _has_star_projection(query.left) or _has_star_projection(query.right)
    return any(isinstance(item.expression, Star) for item in query.select)


def _expr_has_aggregate(expression: ScalarExpr) -> bool:
    return isinstance(expression, Aggregate)


def _predicate_has_aggregate(predicate: Predicate | None) -> bool:
    if predicate is None:
        return False
    if isinstance(predicate, BooleanExpr):
        return any(_predicate_has_aggregate(term) for term in predicate.terms)
    if isinstance(predicate, ExistsPredicate):
        return False
    if isinstance(predicate, InPredicate):
        return _expr_has_aggregate(predicate.left)
    return _expr_has_aggregate(predicate.left) or _expr_has_aggregate(predicate.right)


def _expr_tables(expr: ScalarExpr) -> set[str]:
    if isinstance(expr, ColumnRef):
        return {expr.table}
    if isinstance(expr, Aggregate):
        return _expr_tables(expr.operand)
    return set()


def _predicate_tables(predicate: Predicate | None) -> set[str]:
    if predicate is None:
        return set()
    if isinstance(predicate, BooleanExpr):
        out: set[str] = set()
        for term in predicate.terms:
            out.update(_predicate_tables(term))
        return out
    if isinstance(predicate, ExistsPredicate):
        return set()
    if isinstance(predicate, InPredicate):
        out = _expr_tables(predicate.left)
        if isinstance(predicate.source, tuple):
            for expression in predicate.source:
                out.update(_expr_tables(expression))
        return out
    return _expr_tables(predicate.left) | _expr_tables(predicate.right)


def _expr_query_tables(expr: ScalarExpr) -> set[str]:
    if isinstance(expr, ScalarSubquery):
        return set(expr.query.referenced_tables())
    return set()


def _predicate_query_tables(predicate: Predicate | None) -> set[str]:
    if predicate is None:
        return set()
    if isinstance(predicate, BooleanExpr):
        out: set[str] = set()
        for term in predicate.terms:
            out.update(_predicate_query_tables(term))
        return out
    if isinstance(predicate, ExistsPredicate):
        return set(predicate.query.referenced_tables())
    if isinstance(predicate, InPredicate) and not isinstance(predicate.source, tuple):
        return set(predicate.source.referenced_tables())
    if isinstance(predicate, Comparison):
        return _expr_query_tables(predicate.left) | _expr_query_tables(predicate.right)
    return set()
