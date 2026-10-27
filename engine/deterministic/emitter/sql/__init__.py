"""Emit one readable SQL view stack from the deterministic view plan.

The stack runs on the request's SQLite database (engine/prepared.py). SQLite has no exact decimal type, so
every number is computed through the decimal functions engine.numeric registers on that database: arithmetic,
SUM/AVG/MIN/MAX, comparisons and ordering read numbers exactly, as the Python program does. A number a stage
returns is decimal text, which the runtime reads back as a Decimal (``view_numbers`` in the manifest)."""

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
    AntiJoinView,
    BinaryValue,
    CalculatedView,
    ColumnValue,
    CombinedView,
    CrossView,
    EnrichedView,
    FilteredView,
    FunctionValue,
    JunctionValue,
    LiteralValue,
    PredicateValue,
    ProjectedView,
    ReducedView,
    SortedView,
    SpanValue,
    Value,
    ViewValue,
    WindowView,
)
from engine.numeric import canonical_decimal
from engine.sql_ast import date_span_sql, month_of_date_sql, year_month_of_date_sql


def _function(function: str, operand: str) -> str:
    if function == "TEXT":
        return f"CAST({operand} AS TEXT)"
    if function == "MONTH":
        return month_of_date_sql(operand)
    if function == "YEAR_MONTH":
        return year_month_of_date_sql(operand)
    return f"LOWER({operand})"

_DECIMAL = {"+": "decimal_add", "-": "decimal_sub", "*": "decimal_mul", "/": "decimal_div"}
_DECIMAL_AGGREGATE = {"SUM": "decimal_sum", "AVG": "decimal_avg", "MIN": "decimal_min", "MAX": "decimal_max"}
_ORDERED = frozenset({"=", "!=", "<>", ">", "<", ">=", "<="})


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
    VERSION = 8

    def __init__(self, schema_map: Mapping[str, str] | None = None):
        self.schema_map = MappingProxyType(dict(schema_map or {}))
        self._plan: AnalysisPlan | None = None
        # The numeric values of the stage whose expressions are being rendered (AnalysisPlan.numeric_values).
        self._numbers: frozenset[str] = frozenset()

    def emit(
        self,
        plan: AnalysisPlan,
        *,
        dataset_version: str | None = None,
        knowledgebase_release: str | None = None,
    ) -> GeneratedSQL:
        statements = []
        tables_by_view: dict[str, set[str]] = {}
        values_by_view: dict[str, set[str]] = {}
        columns_by_view = plan.view_columns()
        numbers_by_view = plan.numeric_values()
        self._plan = plan
        inlined = plan.inlined_entries()
        entries: dict[str, str] = {}

        def item(name: str) -> str:
            # An inlined one-sheet entry is no view: its one consumer reads its SELECT inline.
            return f"({entries[name]})" if name in entries else _q(name)

        def relation(name: str) -> str:
            return f"{item(name)} AS {_q(name)}" if name in entries else _q(name)

        for view in plan.views:
            inputs = plan.inputs(view)
            self._numbers = numbers_by_view[inputs[0]] if len(inputs) == 1 else numbers_by_view[view.name]
            if isinstance(view, CombinedView):
                sql, available_tables = self._combined(plan, view)
                available_values = set()
                if view.name in inlined:
                    entries[view.name] = sql
                    tables_by_view[view.name] = set(available_tables)
                    values_by_view[view.name] = available_values
                    continue
            elif isinstance(view, CrossView):
                left = columns_by_view[view.left]
                selected = [f"l.{_q(name)} AS {_q(name)}" for name in left]
                selected.extend(
                    f"r.{_q(name)} AS {_q(view.right_prefix + name if name in left else name)}"
                    for name in columns_by_view[view.right]
                )
                sql = (
                    f"SELECT {', '.join(selected)} FROM {item(view.left)} l "
                    f"CROSS JOIN {item(view.right)} r"
                )
                available_tables = set()
                available_values = set(columns_by_view[view.name])
            elif isinstance(view, AntiJoinView):
                comparisons = " AND ".join(
                    f"r.{_q(key.right)} = l.{_q(key.left)}" for key in view.keys
                )
                sql = (
                    f"SELECT l.* FROM {item(view.left)} l WHERE NOT EXISTS "
                    f"(SELECT 1 FROM {item(view.right)} r WHERE {comparisons})"
                )
                available_tables = set()
                available_values = set(columns_by_view[view.name])
                if view.order:
                    sql += " ORDER BY " + ", ".join(
                        self._order(item, available_tables, available_values) for item in view.order
                    )
            elif isinstance(view, EnrichedView):
                available_tables = set(tables_by_view[view.source])
                available_values = set(values_by_view[view.source])
                sql, added = self._enriched(
                    plan, view, available_tables, relation(view.source)
                )
                available_tables |= added
            elif isinstance(view, FilteredView):
                available_tables = set(tables_by_view[view.source])
                available_values = set(values_by_view[view.source])
                predicate = self._predicate(
                    view.predicate, available_tables, available_values
                )
                sql = f"SELECT * FROM {relation(view.source)} WHERE {predicate}"
            elif isinstance(view, CalculatedView):
                available_tables = set(tables_by_view[view.source])
                available_values = set(values_by_view[view.source])
                selected = ", ".join(
                    f"{self._value(item.value, available_tables, available_values)} AS {_q(item.name)}"
                    for item in view.values
                )
                sql = f"SELECT {_q(view.source)}.*, {selected} FROM {relation(view.source)}"
                available_values |= {item.name for item in view.values}
            elif isinstance(view, ProjectedView):
                available_tables = set(tables_by_view[view.source])
                available_values = set(values_by_view[view.source])
                selected = ", ".join(
                    f"{self._value(item.value, available_tables, available_values)} AS {_q(item.name)}"
                    for item in view.values
                )
                sql = f"SELECT {selected} FROM {relation(view.source)}"
                available_tables = set()
                available_values = {item.name for item in view.values}
            elif isinstance(view, ReducedView):
                available_tables = set(tables_by_view[view.source])
                available_values = set(values_by_view[view.source])
                sql = self._reduced(
                    view, available_tables, available_values, relation(view.source)
                )
                available_tables = set()
                available_values = {item.name for item in view.group_by} | {
                    item.name for item in view.aggregates
                }
            elif isinstance(view, SortedView):
                available_tables = set(tables_by_view[view.source])
                available_values = set(values_by_view[view.source])
                terms = [self._order(item, available_tables, available_values) for item in view.order]
                order = ", ".join(terms)
                # A sort that shows fewer values than it ranks by selects them in the same statement, so the
                # ranking values order the rows and are dropped without a later step losing the order.
                shown = ", ".join(_q(name) for name in view.shown) if view.shown else None
                if view.ties_on:
                    # FETCH FIRST n ROWS WITH TIES, which SQLite lacks: rank by the leading terms and keep
                    # the rows ranked within the limit, in the full order.
                    columns = ", ".join(_q(name) for name in columns_by_view[view.source])
                    sql = (
                        f"SELECT {shown or columns} FROM (SELECT *, RANK() OVER (ORDER BY {', '.join(terms[:view.ties_on])}) "
                        f"AS {_q('__rank')} FROM {relation(view.source)}) AS {_q(view.source)} "
                        f"WHERE {_q('__rank')} <= {view.limit} ORDER BY {order}"
                    )
                else:
                    sql = f"SELECT {shown or '*'} FROM {relation(view.source)} ORDER BY {order}"
                    if view.limit is not None:
                        sql += f" LIMIT {view.limit}"
                if view.shown:
                    available_tables = set()
                    available_values = set(view.shown)
            elif isinstance(view, WindowView):
                available_tables = set(tables_by_view[view.source])
                available_values = set(values_by_view[view.source])
                sql = self._window(
                    view, available_tables, available_values, item(view.source)
                )
                available_values.add(view.output)
            else:
                raise TypeError(f"unsupported SQL view: {type(view).__name__}")
            statements.append(f"CREATE TEMP VIEW {_q(view.name)} AS {sql}")
            tables_by_view[view.name] = set(available_tables)
            values_by_view[view.name] = set(available_values)
        stages = {view.name for view in plan.stages()}
        return GeneratedSQL(
            tuple(statements),
            {
                "emitter": "sql",
                "emitter_version": self.VERSION,
                "slug": plan.slug,
                "dataset_version": dataset_version,
                "knowledgebase_release": knowledgebase_release,
                **plan.stage_manifest(),
                "view_numbers": {
                    name: sorted(numbers) for name, numbers in numbers_by_view.items() if name in stages
                },
            },
        )

    def _combined(self, plan: AnalysisPlan, view: CombinedView) -> tuple[str, set[str]]:
        tables = [plan.table(name) for name in view.tables]
        projections = [
            f"{self._read(table, column)} AS {_q(_flat(table.name, column.name))}"
            for table in tables
            for column in table.columns
        ]
        root = tables[0]
        sql = f"SELECT {', '.join(projections)} FROM {self._table(root.schema, root.name)}"
        joined = {root.name}
        for table in tables[1:]:
            source, relationship = plan.connecting_relationship(joined, table.name)
            target = plan.table(relationship.target_table)
            if relationship.condition is not None:
                join_kind = "LEFT JOIN" if view.outer else "JOIN"
                resolve = lambda value: self._qualified(
                    plan.table(value.table).schema, value.table, value.column
                )
                if relationship.secondary:
                    bridge = plan.table(relationship.secondary)
                    sql += (
                        f" {join_kind} {self._table(bridge.schema, bridge.name)} ON "
                        + self._join_predicate(relationship.condition, resolve)
                    )
                    condition = relationship.secondary_condition
                else:
                    condition = relationship.condition
                sql += (
                    f" {join_kind} {self._table(table.schema, table.name)} ON "
                    + self._join_predicate(condition, resolve)
                )
                joined.add(table.name)
                continue
            comparisons = [
                f"{self._qualified(source.schema, source.name, local)} = "
                f"{self._qualified(target.schema, target.name, remote)}"
                for local, remote in zip(
                    relationship.local_columns, relationship.remote_columns
                )
            ]
            sql += (
                f" {'LEFT JOIN' if view.outer else 'JOIN'} {self._table(table.schema, table.name)} ON "
                + " AND ".join(comparisons)
            )
            joined.add(table.name)
        return sql, set(view.tables)

    def _enriched(
        self,
        plan: AnalysisPlan,
        view: EnrichedView,
        available_tables: set[str],
        source: str,
    ) -> tuple[str, set[str]]:
        sql = f"SELECT {_q(view.source)}.*"
        joins = []
        joined_here: set[str] = set()
        projected = tuple(
            dict.fromkeys(enrichment.target_table for enrichment in view.enrichments)
        )
        for enrichment in view.enrichments:
            join_kind = "JOIN" if enrichment.required else "LEFT JOIN"
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
                    if relationship.condition is not None:

                        def resolve(value):
                            owner = plan.table(value.table)
                            return (
                                f"{_q(view.source)}.{_q(_flat(owner.name, value.column))}"
                                if owner.name in available_tables
                                else self._qualified(
                                    owner.schema, owner.name, value.column
                                )
                            )

                        if relationship.secondary:
                            bridge = plan.table(relationship.secondary)
                            joins.append(
                                f"{join_kind} {self._table(bridge.schema, bridge.name)} ON "
                                + self._join_predicate(relationship.condition, resolve)
                            )
                            condition = relationship.secondary_condition
                        else:
                            condition = relationship.condition
                        joins.append(
                            f"{join_kind} {self._table(target.schema, target.name)} ON "
                            + self._join_predicate(condition, resolve)
                        )
                        joined_here.add(target.name)
                        current = target
                        continue
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
                        f"{join_kind} {self._table(target.schema, target.name)} ON "
                        + " AND ".join(comparisons)
                    )
                    joined_here.add(target.name)
                current = target
        for target_name in projected:
            target = plan.table(target_name)
            sql += ", " + ", ".join(
                f"{self._read(target, column)} AS {_q(_flat(target.name, column.name))}"
                for column in target.columns
            )
        sql += f" FROM {source}"
        if joins:
            sql += " " + " ".join(joins)
        return sql, set(projected)

    def _reduced(
        self, view: ReducedView, tables: set[str], values: set[str], source: str
    ) -> str:
        selected = [
            f"{self._value(item.value, tables, values)} AS {_q(item.name)}"
            for item in view.group_by
        ]
        selected.extend(
            f"{self._aggregate(item, tables, values)} AS {_q(item.name)}"
            for item in view.aggregates
        )
        sql = f"SELECT {', '.join(selected)} FROM {source}"
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
        function = aggregate.function
        if function in _DECIMAL_AGGREGATE and (
            function in {"SUM", "AVG"} or self._plan.numeric(aggregate.operand, self._numbers)
        ):
            # Exact: a decimal sum, and an average rounded half up at the division scale, as the Python
            # program's SUM and AVG (engine.numeric.decimal_divide).
            function = _DECIMAL_AGGREGATE[function]
        return f"{function}({'DISTINCT ' if aggregate.distinct else ''}{operand})"

    def _window(self, view, tables, values, source):
        def scoped(value, alias):
            if isinstance(value, (ViewValue, ColumnValue)):
                return f"{alias}." + self._value(value, tables, values)
            raise TypeError("correlated operators require bound columns")

        current, previous = scoped(view.measure, "t"), scoped(view.measure, "p")
        conditions = [
            self._compare(scoped(key, "p"), "=", scoped(key, "t"), self._plan.numeric(key, self._numbers))
            for key in view.partition
        ]
        timed = view.time is not None and self._plan.numeric(view.time, self._numbers)
        if view.function == "yoy":
            conditions.append(
                self._compare(scoped(view.time, "t"), "=", f"decimal_add({scoped(view.time, 'p')}, 1)", timed)
            )
            expression = f"decimal_div(decimal_sub({current}, {previous}), {previous})"
            return (
                f"SELECT t.*, {expression} AS {_q(view.output)} FROM {source} t JOIN {source} p ON "
                + " AND ".join(conditions)
            )
        if view.function == "running":
            conditions.append(self._compare(scoped(view.time, "p"), "<=", scoped(view.time, "t"), timed))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = f"(SELECT decimal_sum({previous}) FROM {source} p{where})"
        expression = total if view.function == "running" else f"decimal_div({current}, {total})"
        return f"SELECT t.*, {expression} AS {_q(view.output)} FROM {source} t"

    def _predicate(
        self, predicate: PredicateValue, tables: set[str], values: set[str]
    ) -> str:
        if isinstance(predicate, JunctionValue):
            return (
                "("
                + f" {predicate.operator} ".join(
                    self._predicate(child, tables, values)
                    for child in predicate.predicates
                )
                + ")"
            )
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
        return self._compare(
            self._value(predicate.left, tables, values),
            predicate.operator,
            self._value(predicate.right, tables, values),
            self._plan.numeric(predicate.left, self._numbers)
            and self._plan.numeric(predicate.right, self._numbers),
        )

    @staticmethod
    def _compare(left: str, operator: str, right: str, numeric: bool) -> str:
        """A comparison; two numbers compare exactly, by their decimal values."""
        if numeric and operator in _ORDERED:
            return f"decimal_cmp({left}, {right}) {'<>' if operator == '!=' else operator} 0"
        return f"{left} {operator} {right}"

    def _order(self, item, tables: set[str], values: set[str]) -> str:
        """One ORDER BY term: numbers in decimal order, empty values last in both directions."""
        rendered = self._value(item.value, tables, values)
        if self._plan.numeric(item.value, self._numbers):
            rendered += " COLLATE decimal"
        return f"{rendered} {'DESC' if item.descending else 'ASC'} NULLS LAST"

    def _value(self, value: Value, tables: set[str], values: set[str]) -> str:
        if isinstance(value, FunctionValue):
            return _function(value.function, self._value(value.operand, tables, values))
        if isinstance(value, SpanValue):
            end = None if value.end is None else self._value(value.end, tables, values)
            return date_span_sql(self._value(value.start, tables, values), end, value.unit, value.until)
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
            return (
                f"{_DECIMAL[value.operator]}({self._value(value.left, tables, values)}, "
                f"{self._value(value.right, tables, values)})"
            )
        raise TypeError(f"unsupported SQL value: {type(value).__name__}")

    def _join_predicate(self, predicate, resolve):
        """Render typed ORM join conditions; never accept a raw SQL fragment."""

        def value(item):
            if isinstance(item, ColumnValue):
                return resolve(item)
            if isinstance(item, FunctionValue):
                return _function(item.function, value(item.operand))
            if isinstance(item, SpanValue):
                raise TypeError("a span is not a join value")
            if isinstance(item, BinaryValue):
                return f"{_DECIMAL[item.operator]}({value(item.left)}, {value(item.right)})"
            return self._value(item, set(), set())

        if isinstance(predicate, JunctionValue):
            return (
                "("
                + f" {predicate.operator} ".join(
                    self._join_predicate(child, resolve)
                    for child in predicate.predicates
                )
                + ")"
            )
        return f"{value(predicate.left)} {predicate.operator} {value(predicate.right)}"

    def _table(self, schema: str, table: str) -> str:
        return _table(self.schema_map.get(schema, schema), table)

    def _read(self, table, column) -> str:
        """A stored column as a stage reads it. A binary float (ColumnSpec.binary_float) is read as stored: the
        decimal functions and the runtime read it as its shortest decimal, as the Python program does."""
        return self._qualified(table.schema, table.name, column.name)

    def _qualified(self, schema: str, table: str, column: str) -> str:
        return _qualified(self.schema_map.get(schema, schema), table, column)


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
        # Decimal text: SQLite would read 0.1 as a binary float, and the decimal functions read text exactly.
        return "'" + canonical_decimal(value) + "'"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float literal")
        return "'" + canonical_decimal(Decimal(repr(value))) + "'"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, date):
        # An untyped ISO literal compares with a DATE column and with the TEXT column an upload stores its
        # dates in alike; PostgreSQL has no TEXT < DATE ("contracts signed before July 10, 2026", 2026-10-02).
        return "'" + value.isoformat() + "'"
    return "'" + str(value).replace("'", "''") + "'"
