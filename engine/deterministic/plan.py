"""Backend-neutral, immutable view plan for dual SQL and Python emission.

The plan is deliberately smaller than either output language.  A named analysis is
an ordered chain of materialized relations: the ORM supplies ``combined`` and every
later step consumes only the relation immediately before it.
"""

from __future__ import annotations

import keyword
from dataclasses import dataclass
from typing import TypeAlias

from engine.sql_ast import SQLType

_SCHEMAS = frozenset({"conversation", "knowledgebase", "public"})
_AGGREGATES = frozenset({"SUM", "MAX", "MIN", "COUNT", "AVG"})
_BINARY = frozenset({"+", "-", "*", "/"})
_COMPARISONS = frozenset({"=", "!=", "<>", ">", "<", ">=", "<=", "IS", "IS NOT"})


@dataclass(frozen=True)
class ColumnSpec:
    """One physical column and its safe Python attribute."""

    name: str
    attribute: str
    type: SQLType
    primary_key: bool = False
    nullable: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("physical column names must be non-empty")
        _require_identifier(self.attribute, "column attribute")


@dataclass(frozen=True)
class RelationshipSpec:
    """A generated ORM object reference backed by one ordered FK edge."""

    attribute: str
    target_table: str
    local_columns: tuple[str, ...]
    remote_columns: tuple[str, ...]
    many: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.attribute, "relationship attribute")
        if not self.local_columns or len(self.local_columns) != len(
            self.remote_columns
        ):
            raise ValueError("relationship columns must be equally sized and non-empty")


@dataclass(frozen=True)
class TableSpec:
    """One generated SQLAlchemy class."""

    name: str
    class_name: str
    attribute: str
    schema: str
    columns: tuple[ColumnSpec, ...]
    relationships: tuple[RelationshipSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("physical table names must be non-empty")
        if self.schema not in _SCHEMAS:
            raise ValueError(f"unsupported logical schema: {self.schema}")
        _require_identifier(self.class_name, "table class name")
        _require_identifier(self.attribute, "table attribute")
        names = [column.name for column in self.columns]
        attrs = [column.attribute for column in self.columns]
        if len(names) != len(set(names)) or len(attrs) != len(set(attrs)):
            raise ValueError("table columns must have unique physical and Python names")
        relationship_attrs = [
            relationship.attribute for relationship in self.relationships
        ]
        if len(relationship_attrs) != len(set(relationship_attrs)):
            raise ValueError("table relationships must have unique Python attributes")
        if not any(column.primary_key for column in self.columns):
            raise ValueError(f"ORM table {self.name!r} requires a stable primary key")

    def column(self, name: str) -> ColumnSpec:
        try:
            return next(column for column in self.columns if column.name == name)
        except StopIteration as exc:
            raise ValueError(f"unknown column {self.name}.{name}") from exc

    def relationship(self, attribute: str) -> RelationshipSpec:
        try:
            return next(
                item for item in self.relationships if item.attribute == attribute
            )
        except StopIteration as exc:
            raise ValueError(f"unknown relationship {self.name}.{attribute}") from exc


@dataclass(frozen=True)
class ColumnValue:
    table: str
    column: str


@dataclass(frozen=True)
class LiteralValue:
    value: object


@dataclass(frozen=True)
class ViewValue:
    """A value calculated by an earlier materialized view."""

    name: str

    def __post_init__(self) -> None:
        _require_identifier(self.name, "view-value name")


@dataclass(frozen=True)
class BinaryValue:
    left: Value
    operator: str
    right: Value

    def __post_init__(self) -> None:
        if self.operator not in _BINARY:
            raise ValueError(f"unsupported binary operator: {self.operator}")


@dataclass(frozen=True)
class PredicateValue:
    left: Value
    operator: str
    right: Value

    def __post_init__(self) -> None:
        operator = self.operator.upper()
        if operator not in _COMPARISONS:
            raise ValueError(f"unsupported comparison operator: {operator}")
        object.__setattr__(self, "operator", operator)


Value: TypeAlias = ColumnValue | LiteralValue | ViewValue | BinaryValue


@dataclass(frozen=True)
class SelectedValue:
    name: str
    value: Value

    def __post_init__(self) -> None:
        _require_identifier(self.name, "selected-value name")


@dataclass(frozen=True)
class AggregateValue:
    name: str
    function: str
    operand: Value | None = None

    def __post_init__(self) -> None:
        function = self.function.upper()
        if function not in _AGGREGATES:
            raise ValueError(f"unsupported aggregate: {function}")
        if function != "COUNT" and self.operand is None:
            raise ValueError(f"{function} requires an operand")
        _require_identifier(self.name, "aggregate name")
        object.__setattr__(self, "function", function)


@dataclass(frozen=True)
class CombinedView:
    name: str
    tables: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.name, "view name")
        if not self.tables:
            raise ValueError("combined view requires a name and at least one table")
        if len(self.tables) != len(set(self.tables)):
            raise ValueError("combined view tables must be unique")


@dataclass(frozen=True)
class Enrichment:
    target_table: str
    source_table: str
    relationship_path: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.relationship_path:
            raise ValueError("enrichment requires a relationship path")


@dataclass(frozen=True)
class EnrichedView:
    name: str
    source: str
    enrichments: tuple[Enrichment, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.name, "view name")
        _require_identifier(self.source, "source view name")
        if not self.enrichments:
            raise ValueError("enriched view requires at least one enrichment")


@dataclass(frozen=True)
class FilteredView:
    name: str
    source: str
    predicate: PredicateValue

    def __post_init__(self) -> None:
        _require_identifier(self.name, "view name")
        _require_identifier(self.source, "source view name")


@dataclass(frozen=True)
class CalculatedView:
    name: str
    source: str
    values: tuple[SelectedValue, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.name, "view name")
        _require_identifier(self.source, "source view name")
        if not self.values:
            raise ValueError("calculated view requires at least one selected value")


@dataclass(frozen=True)
class ProjectedView:
    """A projection that emits only its named values, without retaining source objects."""

    name: str
    source: str
    values: tuple[SelectedValue, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.name, "view name")
        _require_identifier(self.source, "source view name")
        if not self.values:
            raise ValueError("projected view requires at least one selected value")


@dataclass(frozen=True)
class ReducedView:
    name: str
    source: str
    aggregates: tuple[AggregateValue, ...]
    group_by: tuple[SelectedValue, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.name, "view name")
        _require_identifier(self.source, "source view name")
        if not self.aggregates:
            raise ValueError("reduced view requires at least one aggregate")


ViewStep: TypeAlias = (
    CombinedView
    | EnrichedView
    | FilteredView
    | CalculatedView
    | ProjectedView
    | ReducedView
)


@dataclass(frozen=True)
class AnalysisPlan:
    """One slug-named function and its feed-forward SQL/Python view chain."""

    slug: str
    tables: tuple[TableSpec, ...]
    views: tuple[ViewStep, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.slug, "analysis slug")
        table_names = [table.name for table in self.tables]
        table_classes = [table.class_name for table in self.tables]
        table_attributes = [table.attribute for table in self.tables]
        if len(table_names) != len(set(table_names)):
            raise ValueError("analysis table names must be unique")
        if len(table_classes) != len(set(table_classes)):
            raise ValueError("analysis table class names must be unique")
        if len(table_attributes) != len(set(table_attributes)):
            raise ValueError("analysis table attributes must be unique")
        if not self.views or not isinstance(self.views[0], CombinedView):
            raise ValueError("analysis must begin with one combined ORM view")
        if not isinstance(self.views[-1], (ProjectedView, ReducedView)):
            raise TypeError("analysis must end with one projected or reduced result view")
        table_by_name = {table.name: table for table in self.tables}
        for table in self.tables:
            column_names = {column.name for column in table.columns}
            column_attributes = {column.attribute for column in table.columns}
            for relationship in table.relationships:
                target = table_by_name.get(relationship.target_table)
                if target is None:
                    raise ValueError(
                        f"relationship {table.name}.{relationship.attribute} targets an unknown table"
                    )
                if not set(relationship.local_columns) <= column_names:
                    raise ValueError(
                        f"relationship {table.name}.{relationship.attribute} has unknown local columns"
                    )
                if not set(relationship.remote_columns) <= {
                    column.name for column in target.columns
                }:
                    raise ValueError(
                        f"relationship {table.name}.{relationship.attribute} has unknown remote columns"
                    )
                if relationship.attribute in column_attributes and (
                    len(relationship.local_columns) != 1
                    or table.column(relationship.local_columns[0]).attribute
                    != relationship.attribute
                ):
                    raise ValueError(
                        "a relationship may replace only the matching scalar attribute of a single FK column"
                    )

        emitted: set[str] = set()
        previous = None
        stage_tables: set[str] = set()
        stage_values: set[str] = set()
        for index, view in enumerate(self.views):
            if not view.name.startswith(self.slug + "_"):
                raise ValueError("every view name must use the analysis slug prefix")
            if view.name in emitted:
                raise ValueError("view names must be unique")
            if isinstance(view, CombinedView):
                if index != 0 or any(
                    table not in table_by_name for table in view.tables
                ):
                    raise ValueError("combined view references an unknown table")
                joined = {view.tables[0]}
                for table_name in view.tables[1:]:
                    connections = [
                        relationship
                        for source_name in joined
                        for relationship in table_by_name[source_name].relationships
                        if relationship.target_table == table_name
                    ]
                    connections.extend(
                        relationship
                        for relationship in table_by_name[table_name].relationships
                        if relationship.target_table in joined
                    )
                    if len(connections) != 1:
                        raise ValueError(
                            f"combined table {table_name!r} requires exactly one relationship to the joined graph"
                        )
                    joined.add(table_name)
                stage_tables = set(view.tables)
                stage_values = set()
            else:
                if view.source != previous:
                    raise ValueError(
                        "every view must consume the immediately preceding view"
                    )
                if isinstance(view, EnrichedView):
                    targets = [
                        enrichment.target_table for enrichment in view.enrichments
                    ]
                    if len(targets) != len(set(targets)):
                        raise ValueError(
                            "one enriched view may materialize each target table only once"
                        )
                    incoming_tables = set(stage_tables)
                    for enrichment in view.enrichments:
                        if enrichment.source_table not in incoming_tables:
                            raise ValueError(
                                f"enrichment source {enrichment.source_table!r} is unavailable"
                            )
                        current = table_by_name[enrichment.source_table]
                        for relationship_name in enrichment.relationship_path:
                            relationship = current.relationship(relationship_name)
                            current = table_by_name[relationship.target_table]
                        if current.name != enrichment.target_table:
                            raise ValueError(
                                "enrichment target does not match the relationship path endpoint"
                            )
                        if current.name in incoming_tables:
                            raise ValueError(
                                f"enrichment target {current.name!r} is already materialized"
                            )
                    stage_tables.update(targets)
                elif isinstance(view, FilteredView):
                    self._validate_predicate(view.predicate, stage_tables, stage_values)
                elif isinstance(view, CalculatedView):
                    names = [item.name for item in view.values]
                    if len(names) != len(set(names)) or set(names) & stage_values:
                        raise ValueError("calculated values must be new and unique")
                    if set(names) & {
                        table_by_name[name].attribute for name in stage_tables
                    }:
                        raise ValueError(
                            "calculated values cannot shadow materialized table attributes"
                        )
                    for item in view.values:
                        self._validate_value(item.value, stage_tables, stage_values)
                    stage_values.update(names)
                elif isinstance(view, ProjectedView):
                    names = [item.name for item in view.values]
                    if len(names) != len(set(names)):
                        raise ValueError("projected values must be unique")
                    for item in view.values:
                        self._validate_value(item.value, stage_tables, stage_values)
                    stage_tables = set()
                    stage_values = set(names)
                elif isinstance(view, ReducedView):
                    names = [item.name for item in view.group_by] + [
                        item.name for item in view.aggregates
                    ]
                    if len(names) != len(set(names)):
                        raise ValueError("reduced output names must be unique")
                    for item in view.group_by:
                        self._validate_value(item.value, stage_tables, stage_values)
                    for aggregate in view.aggregates:
                        if aggregate.operand is not None:
                            self._validate_value(
                                aggregate.operand, stage_tables, stage_values
                            )
                    stage_tables = set()
                    stage_values = set(names)
            emitted.add(view.name)
            previous = view.name

    def table(self, name: str) -> TableSpec:
        try:
            return next(table for table in self.tables if table.name == name)
        except StopIteration as exc:
            raise ValueError(f"unknown table: {name}") from exc

    def view_columns(self) -> dict[str, tuple[str, ...]]:
        """Return each materialized stage's stable, SQL-visible column order."""
        shapes: dict[str, tuple[str, ...]] = {}
        current: tuple[str, ...] = ()
        for view in self.views:
            if isinstance(view, CombinedView):
                current = tuple(
                    f"{table_name}__{column.name}"
                    for table_name in view.tables
                    for column in self.table(table_name).columns
                )
            elif isinstance(view, EnrichedView):
                added = []
                seen = set(current)
                for enrichment in view.enrichments:
                    for column in self.table(enrichment.target_table).columns:
                        name = f"{enrichment.target_table}__{column.name}"
                        if name not in seen:
                            seen.add(name)
                            added.append(name)
                current += tuple(added)
            elif isinstance(view, CalculatedView):
                current += tuple(value.name for value in view.values)
            elif isinstance(view, ProjectedView):
                current = tuple(value.name for value in view.values)
            elif isinstance(view, ReducedView):
                current = tuple(value.name for value in view.group_by) + tuple(
                    value.name for value in view.aggregates
                )
            shapes[view.name] = current
        return shapes

    def view_operations(self) -> dict[str, str]:
        operations = {
            CombinedView: "join",
            EnrichedView: "world_join",
            FilteredView: "filter",
            CalculatedView: "convert",
            ProjectedView: "select",
            ReducedView: "group_agg",
        }
        return {view.name: operations[type(view)] for view in self.views}

    def _validate_predicate(
        self,
        predicate: PredicateValue,
        tables: set[str],
        values: set[str],
    ) -> None:
        self._validate_value(predicate.left, tables, values)
        self._validate_value(predicate.right, tables, values)

    def _validate_value(self, value: Value, tables: set[str], values: set[str]) -> None:
        if isinstance(value, LiteralValue):
            return
        if isinstance(value, ViewValue):
            if value.name not in values:
                raise ValueError(f"view value {value.name!r} is unavailable")
            return
        if isinstance(value, ColumnValue):
            if value.table not in tables:
                raise ValueError(f"table {value.table!r} is unavailable")
            self.table(value.table).column(value.column)
            return
        if isinstance(value, BinaryValue):
            self._validate_value(value.left, tables, values)
            self._validate_value(value.right, tables, values)
            return
        raise TypeError(f"unsupported value: {type(value).__name__}")


def _require_identifier(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.isidentifier()
        or keyword.iskeyword(value)
    ):
        raise ValueError(f"{label} must be a non-keyword Python identifier")
