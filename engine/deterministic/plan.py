"""Backend-neutral, immutable view plan for dual SQL and Python emission.

The plan is deliberately smaller than either output language. A named analysis is a
topologically ordered DAG of materialized relations. Most analyses remain a readable
linear pipeline; decomposed analyses add explicit branch and merge nodes without
changing the plan consumed by the SQL and Python emitters.
"""

from __future__ import annotations

import keyword
import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
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
    condition: PredicateValue | JunctionValue | None = None
    secondary: str | None = None
    secondary_condition: PredicateValue | JunctionValue | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "local_columns", tuple(self.local_columns))
        object.__setattr__(self, "remote_columns", tuple(self.remote_columns))
        _require_identifier(self.attribute, "relationship attribute")
        if not self.local_columns or len(self.local_columns) != len(
            self.remote_columns
        ):
            raise ValueError("relationship columns must be equally sized and non-empty")
        if bool(self.secondary) != bool(self.secondary_condition):
            raise ValueError(
                "secondary relationships require a table and join condition"
            )


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
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "relationships", tuple(self.relationships))
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
        storage_attrs = [
            self.scalar_attribute(column.name) for column in self.columns
        ] + relationship_attrs
        if len(storage_attrs) != len(set(storage_attrs)) or {
            "metadata",
            "registry",
        } & set(storage_attrs):
            raise ValueError(
                "ORM attributes collide with storage or SQLAlchemy attributes"
            )
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

    def scalar_attribute(self, column_name: str) -> str:
        """Private storage name when the public FK attribute is an ORM object."""
        column = self.column(column_name)
        if any(
            edge.attribute == column.attribute and column_name in edge.local_columns
            for edge in self.relationships
        ):
            return f"_{column.attribute}_value"
        return column.attribute


@dataclass(frozen=True)
class ColumnValue:
    table: str
    column: str


@dataclass(frozen=True)
class LiteralValue:
    value: object

    def __post_init__(self) -> None:
        value = self.value
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("literal numbers must be finite")
            value = Decimal(str(value))
            object.__setattr__(self, "value", value)
        if isinstance(value, Decimal) and not value.is_finite():
            raise ValueError("literal numbers must be finite")
        if value is not None and type(value) not in {
            bool,
            int,
            str,
            date,
            Decimal,
        }:
            raise TypeError(f"unsupported literal type: {type(value).__name__}")


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
class FunctionValue:
    function: str
    operand: Value

    def __post_init__(self) -> None:
        if self.function not in {"LOWER", "TEXT"}:
            raise ValueError(f"unsupported scalar function: {self.function}")


@dataclass(frozen=True)
class JunctionValue:
    operator: str
    predicates: tuple[PredicateValue | JunctionValue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "predicates", tuple(self.predicates))
        if self.operator not in {"AND", "OR"} or not self.predicates:
            raise ValueError("a junction requires AND/OR and nonempty predicates")


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


Value: TypeAlias = ColumnValue | LiteralValue | ViewValue | BinaryValue | FunctionValue


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
    outer: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", tuple(self.tables))
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
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "relationship_path", tuple(self.relationship_path))
        if not self.relationship_path:
            raise ValueError("enrichment requires a relationship path")


@dataclass(frozen=True)
class EnrichedView:
    name: str
    source: str
    enrichments: tuple[Enrichment, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "enrichments", tuple(self.enrichments))
        _require_identifier(self.name, "view name")
        _require_identifier(self.source, "source view name")
        if not self.enrichments:
            raise ValueError("enriched view requires at least one enrichment")


@dataclass(frozen=True)
class FilteredView:
    name: str
    source: str
    predicate: PredicateValue | JunctionValue

    def __post_init__(self) -> None:
        _require_identifier(self.name, "view name")
        _require_identifier(self.source, "source view name")


@dataclass(frozen=True)
class CalculatedView:
    name: str
    source: str
    values: tuple[SelectedValue, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))
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
        object.__setattr__(self, "values", tuple(self.values))
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
        object.__setattr__(self, "aggregates", tuple(self.aggregates))
        object.__setattr__(self, "group_by", tuple(self.group_by))
        _require_identifier(self.name, "view name")
        _require_identifier(self.source, "source view name")
        if not self.aggregates:
            raise ValueError("reduced view requires at least one aggregate")


@dataclass(frozen=True)
class SortValue:
    value: Value
    descending: bool = False


@dataclass(frozen=True)
class SortedView:
    name: str
    source: str
    order: tuple[SortValue, ...]
    limit: int | None = None

    def __post_init__(self):
        _require_identifier(self.name, "view name")
        _require_identifier(self.source, "source view name")
        object.__setattr__(self, "order", tuple(self.order))
        if not self.order or (
            self.limit is not None and (type(self.limit) is not int or self.limit < 0)
        ):
            raise ValueError("sort requires keys and a nonnegative integer limit")


@dataclass(frozen=True)
class WindowView:
    """A scalar correlated aggregate/previous-period join over the preceding view."""

    name: str
    source: str
    function: str
    output: str
    measure: Value
    partition: tuple[Value, ...] = ()
    time: Value | None = None

    def __post_init__(self):
        for value in (self.name, self.source, self.output):
            _require_identifier(value, "window identifier")
        object.__setattr__(self, "partition", tuple(self.partition))
        if self.function not in {"share", "running", "yoy"}:
            raise ValueError("unsupported window operation")
        if self.function in {"running", "yoy"} and self.time is None:
            raise ValueError("a temporal operation requires an order column")


@dataclass(frozen=True)
class MergeKey:
    """One output-column equality used by a derived-relation merge."""

    left: str
    right: str

    def __post_init__(self) -> None:
        _require_identifier(self.left, "left merge key")
        _require_identifier(self.right, "right merge key")


@dataclass(frozen=True)
class CrossView:
    """The bounded Cartesian product of two already materialized relations."""

    name: str
    left: str
    right: str
    right_prefix: str = "right_"

    def __post_init__(self) -> None:
        for value in (self.name, self.left, self.right):
            _require_identifier(value, "cross-view identifier")
        _require_identifier(self.right_prefix.rstrip("_"), "cross-view prefix")
        if self.left == self.right:
            raise ValueError("a cross view requires two distinct inputs")


@dataclass(frozen=True)
class AntiJoinView:
    """Rows from ``left`` for which no SQL-equal key exists in ``right``."""

    name: str
    left: str
    right: str
    keys: tuple[MergeKey, ...]
    order: tuple[SortValue, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.name, self.left, self.right):
            _require_identifier(value, "anti-join identifier")
        object.__setattr__(self, "keys", tuple(self.keys))
        object.__setattr__(self, "order", tuple(self.order))
        if self.left == self.right or not self.keys:
            raise ValueError("an anti-join requires two distinct inputs and at least one key")


@dataclass(frozen=True)
class PlanSection:
    """One human-readable branch or merge in a decomposed analysis."""

    id: str
    label: str
    question: str
    views: tuple[str, ...]
    inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.id, "plan section id")
        object.__setattr__(self, "views", tuple(self.views))
        object.__setattr__(self, "inputs", tuple(self.inputs))
        if (
            not isinstance(self.label, str)
            or not isinstance(self.question, str)
            or not self.label.strip()
            or not self.question.strip()
            or not self.views
        ):
            raise ValueError("a plan section requires a label, question, and views")
        if len(self.views) != len(set(self.views)) or len(self.inputs) != len(set(self.inputs)):
            raise ValueError("plan section views and inputs must be unique")


ViewStep: TypeAlias = (
    CombinedView
    | EnrichedView
    | FilteredView
    | CalculatedView
    | ProjectedView
    | ReducedView
    | SortedView
    | WindowView
    | CrossView
    | AntiJoinView
)


@dataclass(frozen=True)
class AnalysisPlan:
    """One slug-named function and its topologically ordered SQL/Python view DAG."""

    slug: str
    tables: tuple[TableSpec, ...]
    views: tuple[ViewStep, ...]
    output: str | None = None
    sections: tuple[PlanSection, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", tuple(self.tables))
        object.__setattr__(self, "views", tuple(self.views))
        object.__setattr__(self, "sections", tuple(self.sections))
        # A slug is a durable SQL/view prefix, not a Python symbol. Historical
        # analyses can legitimately be named ``yield`` or ``class``; the Python
        # emitter maps those to a separate safe entrypoint method.
        _require_identifier(self.slug, "analysis slug", allow_keyword=True)
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
            raise ValueError("analysis must begin with a combined ORM view")
        output = self.output or self.views[-1].name
        _require_identifier(output, "analysis output view")
        object.__setattr__(self, "output", output)
        table_by_name = {table.name: table for table in self.tables}
        for table in self.tables:
            column_names = {column.name for column in table.columns}
            column_attributes = {column.attribute for column in table.columns}
            for relationship in table.relationships:
                if (
                    relationship.secondary
                    and relationship.secondary not in table_by_name
                ):
                    raise ValueError("unknown secondary relationship table")
                target = table_by_name.get(relationship.target_table)
                if target is None:
                    raise ValueError(
                        f"relationship {table.name}.{relationship.attribute} targets an unknown table"
                    )
                conditions = (
                    (
                        relationship.condition,
                        {
                            table.name,
                            relationship.secondary or target.name,
                        },
                    ),
                    (
                        relationship.secondary_condition,
                        {relationship.secondary, target.name},
                    ),
                )
                for condition, allowed_tables in conditions:
                    if condition is not None:
                        self._validate_predicate(
                            condition,
                            {name for name in allowed_tables if name is not None},
                            set(),
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
        tables_by_view: dict[str, set[str]] = {}
        values_by_view: dict[str, set[str]] = {}
        columns_by_view: dict[str, tuple[str, ...]] = {}
        for view in self.views:
            if len(view.name.encode("utf-8")) > 63:
                raise ValueError(
                    "view names must fit PostgreSQL's 63-byte identifier limit"
                )
            if not view.name.startswith(self.slug + "_"):
                raise ValueError("every view name must use the analysis slug prefix")
            if view.name in emitted:
                raise ValueError("view names must be unique")
            if isinstance(view, CombinedView):
                if any(table not in table_by_name for table in view.tables):
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
                    if connections[0].many:
                        raise ValueError(
                            "combined views currently require scalar relationships"
                        )
                    joined.add(table_name)
                stage_tables = set(view.tables)
                stage_values: set[str] = set()
            else:
                inputs = self.inputs(view)
                if any(source not in emitted for source in inputs):
                    raise ValueError("every view input must precede the view")
                if isinstance(view, (CrossView, AntiJoinView)):
                    if any(tables_by_view[source] for source in inputs):
                        raise ValueError(
                            "merge inputs must be projected or reduced value relations"
                        )
                    left_columns = columns_by_view[view.left]
                    right_columns = columns_by_view[view.right]
                    stage_tables = set()
                    if isinstance(view, CrossView):
                        right_output = tuple(
                            view.right_prefix + name if name in left_columns else name
                            for name in right_columns
                        )
                        if len(set(left_columns + right_output)) != len(left_columns) + len(right_output):
                            raise ValueError("cross-view output columns must be unique")
                        stage_values = set(left_columns + right_output)
                    else:
                        stage_values = set(left_columns)
                        for key in view.keys:
                            if key.left not in left_columns or key.right not in right_columns:
                                raise ValueError("anti-join keys must name input columns")
                        for item in view.order:
                            self._validate_value(item.value, stage_tables, stage_values)
                else:
                    stage_tables = set(tables_by_view[view.source])
                    stage_values = set(values_by_view[view.source])
                if isinstance(view, EnrichedView):
                    targets = [
                        enrichment.target_table for enrichment in view.enrichments
                    ]
                    if len(targets) != len(set(targets)):
                        raise ValueError(
                            "one enriched view may materialize each target table only once"
                        )
                    incoming_tables = set(stage_tables)
                    path_edges: dict[str, tuple[str, str]] = {}
                    for enrichment in view.enrichments:
                        if enrichment.source_table not in incoming_tables:
                            raise ValueError(
                                f"enrichment source {enrichment.source_table!r} is unavailable"
                            )
                        current = table_by_name[enrichment.source_table]
                        traversed = {current.name}
                        for relationship_name in enrichment.relationship_path:
                            relationship = current.relationship(relationship_name)
                            if relationship.many:
                                raise ValueError(
                                    "enrichment currently requires scalar relationships"
                                )
                            target_name = relationship.target_table
                            edge = (current.name, relationship_name)
                            if target_name in traversed or (
                                target_name in path_edges
                                and path_edges[target_name] != edge
                            ):
                                raise ValueError(
                                    "enrichment paths require aliases for repeated or conflicting tables"
                                )
                            path_edges[target_name] = edge
                            traversed.add(target_name)
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
                elif isinstance(view, SortedView):
                    for item in view.order:
                        self._validate_value(item.value, stage_tables, stage_values)
                elif isinstance(view, WindowView):
                    for value in (
                        view.measure,
                        *view.partition,
                        *((view.time,) if view.time else ()),
                    ):
                        self._validate_value(value, stage_tables, stage_values)
                    if view.output in stage_values or view.output in {
                        table_by_name[name].attribute for name in stage_tables
                    }:
                        raise ValueError("window output shadows a preceding value")
                    stage_values.add(view.output)
            emitted.add(view.name)
            tables_by_view[view.name] = set(stage_tables)
            values_by_view[view.name] = set(stage_values)
            columns_by_view[view.name] = self._view_columns(
                view, columns_by_view
            )

        if self.output not in emitted:
            raise ValueError("analysis output must name a materialized view")
        if self.sections:
            section_ids: set[str] = set()
            section_views: list[str] = []
            for section in self.sections:
                if section.id in section_ids:
                    raise ValueError("plan section ids must be unique")
                if any(source not in section_ids for source in section.inputs):
                    raise ValueError("plan section inputs must precede the section")
                if any(name not in emitted for name in section.views):
                    raise ValueError("plan sections must reference materialized views")
                section_ids.add(section.id)
                section_views.extend(section.views)
            if len(section_views) != len(set(section_views)) or set(section_views) != emitted:
                raise ValueError("plan sections must partition the materialized views")
            section_by_view = {
                view_name: section.id
                for section in self.sections
                for view_name in section.views
            }
            view_by_name = {view.name: view for view in self.views}
            for section in self.sections:
                actual_inputs = {
                    section_by_view[source]
                    for view_name in section.views
                    for source in self.inputs(view_by_name[view_name])
                    if section_by_view[source] != section.id
                }
                if actual_inputs != set(section.inputs):
                    raise ValueError(
                        "plan section inputs must match cross-section view dependencies"
                    )

    def table(self, name: str) -> TableSpec:
        try:
            return next(table for table in self.tables if table.name == name)
        except StopIteration as exc:
            raise ValueError(f"unknown table: {name}") from exc

    def connecting_relationship(self, joined: set[str], table_name: str):
        """The validated join edge, shared by both emitters."""
        for source_name in sorted(joined):
            source = self.table(source_name)
            for relationship in source.relationships:
                if relationship.target_table == table_name:
                    return source, relationship
        table = self.table(table_name)
        for relationship in table.relationships:
            if relationship.target_table in joined:
                return table, relationship
        raise ValueError(f"combined view cannot connect table {table_name}")

    @staticmethod
    def inputs(view: ViewStep) -> tuple[str, ...]:
        if isinstance(view, CombinedView):
            return ()
        if isinstance(view, (CrossView, AntiJoinView)):
            return (view.left, view.right)
        return (view.source,)

    def _view_columns(
        self,
        view: ViewStep,
        shapes: dict[str, tuple[str, ...]],
    ) -> tuple[str, ...]:
        if isinstance(view, CombinedView):
            return tuple(
                f"{table_name}__{column.name}"
                for table_name in view.tables
                for column in self.table(table_name).columns
            )
        if isinstance(view, CrossView):
            left = shapes[view.left]
            return left + tuple(
                view.right_prefix + name if name in left else name
                for name in shapes[view.right]
            )
        if isinstance(view, AntiJoinView):
            return shapes[view.left]
        current = shapes[view.source]
        if isinstance(view, EnrichedView):
            added = []
            seen = set(current)
            for enrichment in view.enrichments:
                for column in self.table(enrichment.target_table).columns:
                    name = f"{enrichment.target_table}__{column.name}"
                    if name not in seen:
                        seen.add(name)
                        added.append(name)
            return current + tuple(added)
        if isinstance(view, CalculatedView):
            return current + tuple(value.name for value in view.values)
        if isinstance(view, WindowView):
            return current + (view.output,)
        if isinstance(view, ProjectedView):
            return tuple(value.name for value in view.values)
        if isinstance(view, ReducedView):
            return tuple(value.name for value in view.group_by) + tuple(
                value.name for value in view.aggregates
            )
        return current

    def view_columns(self) -> dict[str, tuple[str, ...]]:
        """Return each materialized stage's stable, SQL-visible column order."""
        shapes: dict[str, tuple[str, ...]] = {}
        for view in self.views:
            shapes[view.name] = self._view_columns(view, shapes)
        return shapes

    def view_inputs(self) -> dict[str, tuple[str, ...]]:
        return {view.name: self.inputs(view) for view in self.views}

    def view_sections(self) -> dict[str, str | None]:
        out = {view.name: None for view in self.views}
        for section in self.sections:
            for view in section.views:
                out[view] = section.id
        return out

    def view_operations(self) -> dict[str, str]:
        operations = {
            CombinedView: "join",
            EnrichedView: "world_join",
            FilteredView: "filter",
            CalculatedView: "convert",
            ProjectedView: "select",
            ReducedView: "group_agg",
            SortedView: "sort",
            WindowView: "window",
            CrossView: "cross",
            AntiJoinView: "anti_join",
        }
        return {
            view.name: view.function
            if isinstance(view, WindowView)
            else "topn"
            if isinstance(view, SortedView) and view.limit is not None
            else operations[type(view)]
            for view in self.views
        }

    def _validate_predicate(
        self,
        predicate: PredicateValue,
        tables: set[str],
        values: set[str],
    ) -> None:
        if isinstance(predicate, JunctionValue):
            for child in predicate.predicates:
                self._validate_predicate(child, tables, values)
            return
        if predicate.operator in {"IS", "IS NOT"} and not (
            isinstance(predicate.right, LiteralValue)
            and (predicate.right.value is None or type(predicate.right.value) is bool)
        ):
            raise ValueError("IS/IS NOT supports only NULL and boolean literals")
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
        if isinstance(value, FunctionValue):
            self._validate_value(value.operand, tables, values)
            return
        raise TypeError(f"unsupported value: {type(value).__name__}")


def _require_identifier(
    value: str, label: str, *, allow_keyword: bool = False
) -> None:
    if (
        not isinstance(value, str)
        or not value.isidentifier()
        or (keyword.iskeyword(value) and not allow_keyword)
        or value.startswith("__")
    ):
        qualifier = "an identifier" if allow_keyword else "a non-keyword Python identifier"
        raise ValueError(f"{label} must be {qualifier}")
