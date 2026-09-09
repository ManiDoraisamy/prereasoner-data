"""Render a validated typed AST to SQL text.

This is the production lowering of the IR in `engine/sql_ast.py`. The IR owns the node grammar,
typing, and validation; this module owns only the mapping from validated nodes to a SQL string.
Nothing here decides semantics — a rendered string is always a faithful reading of the tree it
came from, which is what lets the Python emitter be checked against it.

``sqlite_decimal`` is the exact local-execution dialect. Its function names are installed by
:func:`engine.numeric.register_sqlite_decimal`, so exact-decimal arithmetic is Python code that
SQLite calls; ``postgres_numeric`` is the live-serving equivalent over ``numeric``.
"""
from __future__ import annotations

from decimal import Decimal
import math
from numbers import Real

from engine.sql_ast import (
    Aggregate,
    ASTValidationError,
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
    SelectItem,
    SetQuery,
    SQLType,
    Star,
    SubquerySource,
    expression_tables,
    expression_type,
    validate_expression,
    validate_query,
)

DIALECTS = frozenset({"standard", "sqlite_decimal", "postgres_numeric"})


def render_query(query: Query, *, dialect: str = "standard") -> str:
    """Render validated SQL.

    ``sqlite_decimal`` is the exact local-execution dialect. Its function names
    are installed by :func:`engine.numeric.register_sqlite_decimal`.
    """
    if dialect not in DIALECTS:
        raise ValueError(f"unsupported SQL dialect: {dialect}")
    validate_query(query)
    return _render_query(query, dialect)


def render_scalar_expression(expression: ScalarExpr, *, dialect: str = "standard") -> str:
    """Render one typed scalar expression for a larger, externally-owned SELECT.

    Some serving adapters own joins that are not ordinary tenant-table AST nodes, such as the
    request-local world bridge. They may still consume a registered calculation expression without
    translating it back into ad hoc SQL. Validation uses exactly the qualifiers referenced by the
    expression; the owning query remains responsible for making those tables visible.
    """
    if dialect not in DIALECTS:
        raise ValueError(f"unsupported SQL dialect: {dialect}")
    validate_expression(expression, frozenset(expression_tables(expression)))
    return _render_expr(expression, dialect)


def _render_query(query: Query, dialect: str = "standard") -> str:
    if isinstance(query, SetQuery):
        return f"{_render_query(query.left, dialect)} {query.operator} {_render_query(query.right, dialect)}"

    select = ", ".join(_render_select(item, dialect) for item in query.select)
    sql = "SELECT " + ("DISTINCT " if query.distinct else "") + select
    if isinstance(query.from_table, SubquerySource):
        sql += f" FROM ({_render_query(query.from_table.query, dialect)}) AS {_qident(query.from_table.alias)}"
    else:
        sql += f" FROM {_qident(query.from_table)}"
        if query.from_alias is not None:
            sql += f" AS {_qident(query.from_alias)}"
    for join in query.joins:
        prefix = "LEFT JOIN" if join.kind == "LEFT" else "JOIN"
        sql += f" {prefix} {_qident(join.table)}"
        if join.alias is not None:
            sql += f" AS {_qident(join.alias)}"
        sql += " ON " + " AND ".join(
            _render_comparison(left, "=", right, dialect)
            for left, right in join.predicates
        )
    if query.where is not None:
        sql += " WHERE " + _render_predicate(query.where, dialect)
    if query.group_by:
        sql += " GROUP BY " + ", ".join(_render_expr(column, dialect) for column in query.group_by)
    if query.having is not None:
        sql += " HAVING " + _render_predicate(query.having, dialect)
    if query.order_by:
        sql += " ORDER BY " + ", ".join(
            f"{_render_order_expr(term.expression, dialect)} {term.direction}" for term in query.order_by
        )
    if query.limit is not None:
        sql += f" LIMIT {query.limit}"
    return sql


def _render_select(item: SelectItem, dialect: str = "standard") -> str:
    value = _render_expr(item.expression, dialect)
    return value if item.alias is None else f"{value} AS {_qident(item.alias)}"


def _render_expr(expr: ScalarExpr, dialect: str = "standard") -> str:
    if isinstance(expr, ColumnRef):
        return f"{_qident(expr.table)}.{_qident(expr.name)}"
    if isinstance(expr, Star):
        return "*"
    if isinstance(expr, Literal):
        return _render_literal(expr)
    if isinstance(expr, Aggregate):
        distinct = "DISTINCT " if expr.distinct else ""
        function = expr.function
        if dialect == "sqlite_decimal" and expression_type(expr.operand).numeric:
            function = {
                "SUM": "decimal_sum", "AVG": "decimal_avg",
                "MIN": "decimal_min", "MAX": "decimal_max",
            }.get(function, function)
        return f"{function}({distinct}{_render_expr(expr.operand, dialect)})"
    if isinstance(expr, BinaryExpr):
        if dialect == "sqlite_decimal":
            function = {"+": "decimal_add", "-": "decimal_sub", "*": "decimal_mul", "/": "decimal_div"}[
                expr.operator
            ]
            return f"{function}({_render_expr(expr.left, dialect)}, {_render_expr(expr.right, dialect)})"
        if expr.operator == "/":
            # SQLite and PostgreSQL perform integer division for integer operands.
            # The AST contract declares division REAL, so force portable real arithmetic. A
            # data-dependent zero denominator yields NULL instead of aborting the whole query.
            right = _render_expr(expr.right, dialect)
            if not (
                isinstance(expr.right, Literal)
                and isinstance(expr.right.value, (Real, Decimal))
                and not isinstance(expr.right.value, bool)
                and expr.right.value != 0
            ):
                right = f"NULLIF({right}, 0)"
            cast_type = "NUMERIC" if dialect == "postgres_numeric" else "REAL"
            return f"(CAST({_render_expr(expr.left, dialect)} AS {cast_type}) / {right})"
        return f"({_render_expr(expr.left, dialect)} {expr.operator} {_render_expr(expr.right, dialect)})"
    if isinstance(expr, ScalarSubquery):
        return f"({_render_query(expr.query, dialect)})"
    raise TypeError(f"unsupported expression: {type(expr).__name__}")


def _render_predicate(predicate: Predicate, dialect: str = "standard") -> str:
    if isinstance(predicate, BooleanExpr):
        return "(" + f" {predicate.operator} ".join(
            _render_predicate(term, dialect) for term in predicate.terms
        ) + ")"
    if isinstance(predicate, ExistsPredicate):
        prefix = "NOT " if predicate.negated else ""
        return f"{prefix}EXISTS ({_render_query(predicate.query, dialect)})"
    if isinstance(predicate, InPredicate):
        prefix = "NOT " if predicate.negated else ""
        if isinstance(predicate.source, tuple):
            source = ", ".join(_render_expr(expression, dialect) for expression in predicate.source)
        else:
            source = _render_query(predicate.source, dialect)
        return f"{_render_expr(predicate.left, dialect)} {prefix}IN ({source})"
    return _render_comparison(predicate.left, predicate.operator, predicate.right, dialect)


def _render_comparison(left: ScalarExpr, operator: str, right: ScalarExpr, dialect: str) -> str:
    rendered_left = _render_expr(left, dialect)
    rendered_right = _render_expr(right, dialect)
    if (dialect == "sqlite_decimal" and expression_type(left).numeric and expression_type(right).numeric
            and operator in {"=", "!=", "<>", ">", "<", ">=", "<="}):
        return f"decimal_cmp({rendered_left}, {rendered_right}) {operator} 0"
    return f"{rendered_left} {operator} {rendered_right}"


def _render_order_expr(expression: ScalarExpr, dialect: str) -> str:
    rendered = _render_expr(expression, dialect)
    if dialect == "sqlite_decimal" and expression_type(expression).numeric:
        return f"{rendered} COLLATE decimal"
    return rendered


def _render_literal(literal: Literal) -> str:
    value = literal.value
    if value is None:
        return "NULL"
    if literal.type == SQLType.BOOLEAN or isinstance(value, bool):
        return "1" if bool(value) else "0"
    if isinstance(value, (Real, Decimal)) and not isinstance(value, bool):
        if isinstance(value, Decimal):
            finite = value.is_finite()
        else:
            finite = math.isfinite(float(value))
        if not finite:
            raise ASTValidationError("non-finite numeric literal")
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _qident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'
