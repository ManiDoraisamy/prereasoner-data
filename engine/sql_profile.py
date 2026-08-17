"""Schema-independent structural profiles for typed SQL ASTs."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping

from engine.sql_ast import (
    Aggregate,
    BooleanExpr,
    ColumnRef,
    Comparison,
    ExistsPredicate,
    InPredicate,
    Literal,
    Query,
    ScalarSubquery,
    SetQuery,
    Star,
    SubquerySource,
)


SCHEMA_ROLES = ("projection", "aggregate", "filter", "group", "having", "order", "join")


def canonical_name(value: Any) -> str:
    """Normalize an identifier while retaining readable boundaries."""
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


@dataclass(frozen=True)
class SQLProfile:
    """A deterministic SQL sketch and its role-aware schema references."""

    sketch: tuple[tuple[str, int], ...]
    tables: tuple[str, ...]
    roles: tuple[tuple[str, tuple[str, ...]], ...]

    @classmethod
    def build(
        cls,
        sketch: Mapping[str, int],
        tables: Iterable[str],
        roles: Mapping[str, Iterable[str]],
    ) -> "SQLProfile":
        return cls(
            tuple(sorted((str(name), int(value)) for name, value in sketch.items() if value)),
            tuple(sorted(set(tables))),
            tuple(
                (role, tuple(sorted(set(roles.get(role, ())))))
                for role in SCHEMA_ROLES
                if roles.get(role)
            ),
        )

    @property
    def sketch_map(self) -> dict[str, int]:
        return dict(self.sketch)

    @property
    def role_map(self) -> dict[str, tuple[str, ...]]:
        return dict(self.roles)

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(sorted({column for _, columns in self.roles for column in columns}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sketch": dict(self.sketch),
            "tables": list(self.tables),
            "roles": {role: list(columns) for role, columns in self.roles},
        }


class _ProfileBuilder:
    def __init__(self) -> None:
        self.sketch: Counter[str] = Counter()
        self.tables: set[str] = set()
        self.roles: defaultdict[str, set[str]] = defaultdict(set)

    def feature(self, name: str, count: int = 1) -> None:
        if count:
            self.sketch[name] += int(count)

    def table(self, name: Any) -> str:
        normalized = canonical_name(name)
        if normalized:
            self.tables.add(normalized)
        return normalized

    def column(self, table: Any, column: Any, *roles: str) -> None:
        table_name = self.table(table)
        column_name = canonical_name(column)
        if not table_name or not column_name or column_name == "star":
            return
        qualified = f"{table_name}.{column_name}"
        for role in roles:
            self.roles[role].add(qualified)

    def finish(self) -> SQLProfile:
        return SQLProfile.build(self.sketch, self.tables, self.roles)


class _ASTProfileBuilder(_ProfileBuilder):
    def visit_query(self, query: Query, inherited_aliases: Mapping[str, str] | None = None) -> None:
        aliases = dict(inherited_aliases or {})
        if isinstance(query, SetQuery):
            self.feature(f"set.{query.operator.lower()}")
            self.visit_query(query.left, aliases)
            self.visit_query(query.right, aliases)
            return

        self.feature("blocks")
        local_aliases = dict(aliases)
        from_tables = 0
        if isinstance(query.from_table, SubquerySource):
            self.feature("subquery.from")
            self.visit_query(query.from_table.query, aliases)
            local_aliases[query.from_table.alias] = canonical_name(query.from_table.alias)
        else:
            root = self.table(query.from_table)
            from_tables += 1
            local_aliases[query.from_table] = root
            if query.from_alias:
                local_aliases[query.from_alias] = root

        for join in query.joins:
            physical = self.table(join.table)
            local_aliases[join.table] = physical
            if join.alias:
                local_aliases[join.alias] = physical
            for left, right in join.predicates:
                self.feature("join_predicates")
                self.feature("predicate.=")
                self._column(left, "join", aliases=local_aliases)
                self._column(right, "join", aliases=local_aliases)
            from_tables += 1
        self.feature("from_tables", from_tables)
        self.feature("joins", len(query.joins))
        if len({join.table for join in query.joins}) < len(query.joins):
            self.feature("self_join")

        self.feature("select_items", len(query.select))
        if query.distinct:
            self.feature("distinct")
        for item in query.select:
            self._expression(item.expression, "projection", local_aliases)

        self._predicate(query.where, "filter", local_aliases)
        self.feature("filter_predicates", self._predicate_atoms(query.where))
        self.feature("group_items", len(query.group_by))
        for column in query.group_by:
            self._column(column, "group", aliases=local_aliases)
        self._predicate(query.having, "having", local_aliases)
        self.feature("having_predicates", self._predicate_atoms(query.having))
        for term in query.order_by:
            self.feature(f"order.{term.direction.lower()}")
            self._expression(term.expression, "order", local_aliases)
        if query.limit is not None:
            self.feature("limit")

    def _resolve_table(self, name: str, aliases: Mapping[str, str]) -> str:
        return aliases.get(name, canonical_name(name))

    def _column(
        self, column: ColumnRef, role: str, aliases: Mapping[str, str], aggregate: bool = False
    ) -> None:
        roles = [role]
        if aggregate:
            roles.append("aggregate")
        self.column(self._resolve_table(column.table, aliases), column.name, *roles)

    def _expression(self, expression: Any, role: str, aliases: Mapping[str, str]) -> None:
        if isinstance(expression, ColumnRef):
            self._column(expression, role, aliases)
        elif isinstance(expression, Aggregate):
            self.feature(f"aggregate.{expression.function.upper()}")
            if expression.distinct:
                self.feature("distinct")
            if isinstance(expression.operand, ColumnRef):
                self._column(expression.operand, role, aliases, aggregate=True)
        elif isinstance(expression, ScalarSubquery):
            self.feature("subquery.scalar")
            self.visit_query(expression.query, aliases)
        elif isinstance(expression, (Literal, Star)):
            return

    def _predicate(self, predicate: Any, role: str, aliases: Mapping[str, str]) -> None:
        if predicate is None:
            return
        if isinstance(predicate, BooleanExpr):
            self.feature(f"boolean.{predicate.operator.lower()}", max(1, len(predicate.terms) - 1))
            for term in predicate.terms:
                self._predicate(term, role, aliases)
            return
        if isinstance(predicate, Comparison):
            self.feature(f"predicate.{predicate.operator.lower()}")
            self._expression(predicate.left, role, aliases)
            self._expression(predicate.right, role, aliases)
            return
        if isinstance(predicate, InPredicate):
            self.feature("predicate.not" if predicate.negated else "predicate.in")
            if predicate.negated:
                self.feature("predicate.in")
            self._expression(predicate.left, role, aliases)
            if isinstance(predicate.source, tuple):
                for expression in predicate.source:
                    self._expression(expression, role, aliases)
            else:
                self.feature("subquery.predicate")
                self.visit_query(predicate.source, aliases)
            return
        if isinstance(predicate, ExistsPredicate):
            if predicate.negated:
                self.feature("predicate.not")
            self.feature("predicate.exists")
            self.feature("subquery.predicate")
            self.visit_query(predicate.query, aliases)

    def _predicate_atoms(self, predicate: Any) -> int:
        if predicate is None:
            return 0
        if isinstance(predicate, BooleanExpr):
            return sum(self._predicate_atoms(term) for term in predicate.terms)
        return 1


def profile_query(query: Query) -> SQLProfile:
    """Profile an engine typed AST recursively."""
    builder = _ASTProfileBuilder()
    builder.visit_query(query)
    return builder.finish()
