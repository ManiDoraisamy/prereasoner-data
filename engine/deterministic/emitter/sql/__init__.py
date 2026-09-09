"""Emit one readable SQL view stack from the deterministic view plan."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from engine.deterministic.plan import (
    AggregateValue,
    AnalysisPlan,
    BinaryValue,
    CalculatedView,
    ColumnValue,
    CombinedView,
    EnrichedView,
    FilteredView,
    LiteralValue,
    PredicateValue,
    ProjectedView,
    ReducedView,
    Value,
    ViewValue,
)

_BINARY = {"+": "+", "-": "-", "*": "*", "/": "/"}


@dataclass(frozen=True)
class GeneratedSQL:
    """Exact SQL source emitted for one immutable analysis revision."""

    statements: tuple[str, ...]
    manifest: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "statements", tuple(self.statements))
        object.__setattr__(self, "manifest", MappingProxyType(dict(self.manifest)))

    def __iter__(self) -> Iterator[str]:
        return iter(self.statements)

    @property
    def source(self) -> str:
        return ";\n\n".join(self.statements) + ";\n"

    @property
    def source_sha256(self) -> str:
        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()

    def record(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_sha256": self.source_sha256,
            "manifest": dict(self.manifest),
        }


class SQLEmitter:
    VERSION = 1

    def __init__(self, schema_map: Mapping[str, str] | None = None):
        self.schema_map = MappingProxyType(dict(schema_map or {}))

    def emit(
        self,
        plan: AnalysisPlan,
        *,
        dataset_version: str | None = None,
        knowledgebase_release: str | None = None,
    ) -> GeneratedSQL:
        statements = []
        available_tables: set[str] = set()
        available_values: set[str] = set()
        for view in plan.views:
            if isinstance(view, CombinedView):
                sql, available_tables = self._combined(plan, view)
                available_values = set()
            elif isinstance(view, EnrichedView):
                sql, added = self._enriched(plan, view, available_tables)
                available_tables |= added
            elif isinstance(view, FilteredView):
                predicate = self._predicate(
                    view.predicate, available_tables, available_values
                )
                sql = f"SELECT * FROM {_q(view.source)} WHERE {predicate}"
            elif isinstance(view, CalculatedView):
                selected = ", ".join(
                    f"{self._value(item.value, available_tables, available_values)} AS {_q(item.name)}"
                    for item in view.values
                )
                sql = f"SELECT {_q(view.source)}.*, {selected} FROM {_q(view.source)}"
                available_values |= {item.name for item in view.values}
            elif isinstance(view, ProjectedView):
                selected = ", ".join(
                    f"{self._value(item.value, available_tables, available_values)} AS {_q(item.name)}"
                    for item in view.values
                )
                sql = f"SELECT {selected} FROM {_q(view.source)}"
                available_tables = set()
                available_values = {item.name for item in view.values}
            elif isinstance(view, ReducedView):
                sql = self._reduced(view, available_tables, available_values)
                available_tables = set()
                available_values = {item.name for item in view.group_by} | {
                    item.name for item in view.aggregates
                }
            else:
                raise TypeError(f"unsupported SQL view: {type(view).__name__}")
            statements.append(f"CREATE TEMP VIEW {_q(view.name)} AS {sql}")
        return GeneratedSQL(
            tuple(statements),
            {
                "emitter": "sql",
                "emitter_version": self.VERSION,
                "slug": plan.slug,
                "dataset_version": dataset_version,
                "knowledgebase_release": knowledgebase_release,
                "views": [view.name for view in plan.views],
                "view_columns": {
                    name: list(columns)
                    for name, columns in plan.view_columns().items()
                },
                "view_operations": plan.view_operations(),
            },
        )

    def _combined(self, plan: AnalysisPlan, view: CombinedView) -> tuple[str, set[str]]:
        tables = [plan.table(name) for name in view.tables]
        projections = [
            f"{self._qualified(table.schema, table.name, column.name)} AS {_q(_flat(table.name, column.name))}"
            for table in tables
            for column in table.columns
        ]
        root = tables[0]
        sql = f"SELECT {', '.join(projections)} FROM {self._table(root.schema, root.name)}"
        joined = {root.name}
        for table in tables[1:]:
            connection = self._connecting_relationship(plan, joined, table.name)
            if connection is None:
                raise ValueError(f"combined view cannot connect table {table.name}")
            source, relationship = connection
            target = plan.table(relationship.target_table)
            comparisons = [
                f"{self._qualified(source.schema, source.name, local)} = "
                f"{self._qualified(target.schema, target.name, remote)}"
                for local, remote in zip(
                    relationship.local_columns, relationship.remote_columns
                )
            ]
            sql += f" JOIN {self._table(table.schema, table.name)} ON " + " AND ".join(
                comparisons
            )
            joined.add(table.name)
        return sql, set(view.tables)

    def _enriched(
        self,
        plan: AnalysisPlan,
        view: EnrichedView,
        available_tables: set[str],
    ) -> tuple[str, set[str]]:
        sql = f"SELECT {_q(view.source)}.*"
        joins = []
        joined_here: set[str] = set()
        projected = tuple(
            dict.fromkeys(enrichment.target_table for enrichment in view.enrichments)
        )
        for enrichment in view.enrichments:
            current = plan.table(enrichment.source_table)
            if current.name not in available_tables:
                raise ValueError(f"enrichment source {current.name!r} is unavailable")
            for relationship_name in enrichment.relationship_path:
                relationship = current.relationship(relationship_name)
                target = plan.table(relationship.target_table)
                if (
                    target.name not in joined_here
                    and target.name not in available_tables
                ):
                    comparisons = []
                    for local, remote in zip(
                        relationship.local_columns, relationship.remote_columns
                    ):
                        left = (
                            f"{_q(view.source)}.{_q(_flat(current.name, local))}"
                            if current.name in available_tables
                            else self._qualified(current.schema, current.name, local)
                        )
                        comparisons.append(
                            f"{left} = {self._qualified(target.schema, target.name, remote)}"
                        )
                    joins.append(
                        f"JOIN {self._table(target.schema, target.name)} ON "
                        + " AND ".join(comparisons)
                    )
                    joined_here.add(target.name)
                current = target
        for target_name in projected:
            target = plan.table(target_name)
            sql += ", " + ", ".join(
                f"{self._qualified(target.schema, target.name, column.name)} "
                f"AS {_q(_flat(target.name, column.name))}"
                for column in target.columns
            )
        sql += f" FROM {_q(view.source)}"
        if joins:
            sql += " " + " ".join(joins)
        return sql, set(projected)

    def _reduced(self, view: ReducedView, tables: set[str], values: set[str]) -> str:
        selected = [
            f"{self._value(item.value, tables, values)} AS {_q(item.name)}"
            for item in view.group_by
        ]
        selected.extend(
            f"{self._aggregate(item, tables, values)} AS {_q(item.name)}"
            for item in view.aggregates
        )
        sql = f"SELECT {', '.join(selected)} FROM {_q(view.source)}"
        if view.group_by:
            sql += " GROUP BY " + ", ".join(
                self._value(item.value, tables, values) for item in view.group_by
            )
        return sql

    def _aggregate(
        self, aggregate: AggregateValue, tables: set[str], values: set[str]
    ) -> str:
        operand = (
            "*"
            if aggregate.operand is None
            else self._value(aggregate.operand, tables, values)
        )
        return f"{aggregate.function}({operand})"

    def _predicate(
        self, predicate: PredicateValue, tables: set[str], values: set[str]
    ) -> str:
        if predicate.operator in {"IS", "IS NOT"} and isinstance(
            predicate.right, LiteralValue
        ):
            if predicate.right.value is None:
                right = "NULL"
            elif isinstance(predicate.right.value, bool):
                right = "TRUE" if predicate.right.value else "FALSE"
            else:
                raise ValueError("IS/IS NOT supports only NULL and boolean literals")
            return (
                f"{self._value(predicate.left, tables, values)} "
                f"{predicate.operator} {right}"
            )
        return (
            f"{self._value(predicate.left, tables, values)} {predicate.operator} "
            f"{self._value(predicate.right, tables, values)}"
        )

    def _value(self, value: Value, tables: set[str], values: set[str]) -> str:
        if isinstance(value, LiteralValue):
            return _literal(value.value)
        if isinstance(value, ViewValue):
            if value.name not in values:
                raise ValueError(f"view value {value.name!r} is unavailable")
            return _q(value.name)
        if isinstance(value, ColumnValue):
            if value.table not in tables:
                raise ValueError(f"table {value.table!r} is unavailable")
            return _q(_flat(value.table, value.column))
        if isinstance(value, BinaryValue):
            if value.operator == "/":
                left = self._value(value.left, tables, values)
                right = self._value(value.right, tables, values)
                return f"(CAST({left} AS NUMERIC) / NULLIF({right}, 0))"
            return (
                f"({self._value(value.left, tables, values)} {_BINARY[value.operator]} "
                f"{self._value(value.right, tables, values)})"
            )
        raise TypeError(f"unsupported SQL value: {type(value).__name__}")

    def _table(self, schema: str, table: str) -> str:
        return _table(self.schema_map.get(schema, schema), table)

    def _qualified(self, schema: str, table: str, column: str) -> str:
        return _qualified(self.schema_map.get(schema, schema), table, column)

    @staticmethod
    def _connecting_relationship(plan: AnalysisPlan, joined: set[str], table_name: str):
        for source_name in sorted(joined):
            source = plan.table(source_name)
            for relationship in source.relationships:
                if relationship.target_table == table_name:
                    return source, relationship
        table = plan.table(table_name)
        for relationship in table.relationships:
            if relationship.target_table in joined:
                return table, relationship
        return None


def _q(value: object) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table(schema: str, table: str) -> str:
    return f"{_q(schema)}.{_q(table)}"


def _qualified(schema: str, table: str, column: str) -> str:
    return f"{_table(schema, table)}.{_q(column)}"


def _flat(table: str, column: str) -> str:
    return f"{table}__{column}"


def _literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal literal")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float literal")
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, date):
        return "DATE '" + value.isoformat() + "'"
    return "'" + str(value).replace("'", "''") + "'"
