"""Emit readable, executable SQLAlchemy models and a feed-forward Python analysis."""

from __future__ import annotations

import ast
import hashlib
import json
import keyword
import math
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
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
    FunctionValue,
    JunctionValue,
    LiteralValue,
    PredicateValue,
    ProjectedView,
    ReducedView,
    SortedView,
    TableSpec,
    Value,
    ViewValue,
    WindowView,
)
from engine.sql_ast import SQLType

_TYPE = {
    SQLType.INTEGER: ("int", "BigInteger"),
    SQLType.REAL: ("Decimal", "Numeric(58, 20)"),
    SQLType.BOOLEAN: ("bool", "Boolean"),
    SQLType.DATE: ("date", "Date"),
    SQLType.TEXT: ("str", "Text"),
    SQLType.UNKNOWN: ("object", "Text"),
}
_BINARY_FUNCTION = {"+": "ADD", "-": "SUBTRACT", "*": "MULTIPLY", "/": "DIVIDE"}
_PREDICATE_FUNCTION = {
    "=": "EQ",
    "!=": "NE",
    "<>": "NE",
    ">": "GT",
    ">=": "GE",
    "<": "LT",
    "<=": "LE",
    "IS": "IS",
    "IS NOT": "IS_NOT",
}


@dataclass(frozen=True)
class GeneratedPackage:
    """Exact virtual files emitted for one immutable analysis revision."""

    files: Mapping[str, str]
    manifest: Mapping[str, object]
    entrypoint: str = "analysis.py"

    def __post_init__(self) -> None:
        ordered = dict(
            sorted((str(name), str(source)) for name, source in self.files.items())
        )
        if self.entrypoint not in ordered:
            raise ValueError("generated package is missing its entrypoint")
        for name, source in ordered.items():
            if Path(name).name != name or not name.endswith(".py"):
                raise ValueError("generated source names must be plain .py filenames")
            ast.parse(source, filename=name)
        object.__setattr__(self, "files", MappingProxyType(ordered))
        object.__setattr__(self, "manifest", MappingProxyType(dict(self.manifest)))

    @property
    def source_sha256(self) -> str:
        digest = hashlib.sha256()
        for name, source in self.files.items():
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(source.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def record(self) -> dict[str, object]:
        return {
            "entrypoint": self.entrypoint,
            "source_sha256": self.source_sha256,
            "file_sha256": {
                name: hashlib.sha256(source.encode("utf-8")).hexdigest()
                for name, source in self.files.items()
            },
            "files": dict(self.files),
            "manifest": dict(self.manifest),
        }

    def write_debug(
        self, root: str | Path, conversation_id: str, revision: int
    ) -> Path:
        """Write the exact virtual package beneath one validated development-only root."""
        if not _safe_id(conversation_id, "c_"):
            raise ValueError(
                "generated debug paths require a canonical conversation id"
            )
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("generated debug revision must be positive")
        slug = str(self.manifest["slug"])
        if not slug.isidentifier() or keyword.iskeyword(slug):
            raise ValueError("generated debug paths require a canonical analysis slug")
        root_path = Path(root).resolve()
        destination = (root_path / conversation_id / slug).resolve()
        if root_path not in destination.parents:
            raise ValueError("generated debug path escaped its root")
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for name, source in self.files.items():
            (destination / name).write_text(source, encoding="utf-8", newline="\n")
        payload = {
            **dict(self.manifest),
            "revision": revision,
            "source_sha256": self.source_sha256,
            "file_sha256": self.record()["file_sha256"],
        }
        (destination / "manifest.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return destination


def _safe_id(value: str, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and len(value) == 34
        and all(character in "0123456789abcdef" for character in value[2:])
    )


class PythonEmitter:
    VERSION = 4

    def emit(
        self,
        plan: AnalysisPlan,
        *,
        dataset_version: str | None = None,
        knowledgebase_release: str | None = None,
    ) -> GeneratedPackage:
        wrapper_module = _wrapper_module(plan)
        wrapper_class = _wrapper_class(plan)
        module_names = [
            "base",
            *(table.attribute for table in plan.tables),
            wrapper_module,
        ]
        if len(module_names) != len(set(module_names)):
            raise ValueError("generated module names collide")
        symbols = [
            "Base",
            "Session",
            "Decimal",
            "AnalysisResult",
            "date",
            "select",
            "and_",
            "or_",
            "cast",
            "func",
            "Text",
            "set_committed_value",
            *_operator_imports(plan),
            *(table.class_name for table in plan.tables),
            *(
                _class_name(view.name)
                for view in plan.views
                if not isinstance(view, (FilteredView, SortedView))
            ),
            wrapper_class,
        ]
        if len(symbols) != len(set(symbols)):
            raise ValueError("generated Python class or import names collide")
        files = {"base.py": self._base_source()}
        for table in plan.tables:
            files[f"{table.attribute}.py"] = self._table_source(plan, table)
        files[f"{wrapper_module}.py"] = self._analysis_source(plan)
        manifest = {
            "emitter": "python",
            "emitter_version": self.VERSION,
            "slug": plan.slug,
            "entrypoint_class": wrapper_class,
            "dataset_version": dataset_version,
            "knowledgebase_release": knowledgebase_release,
            "tables": [table.name for table in plan.tables],
            "views": [view.name for view in plan.views],
            "view_columns": {
                name: list(columns) for name, columns in plan.view_columns().items()
            },
            "view_operations": plan.view_operations(),
            "relationship_edges": [
                {
                    "source": table.name,
                    "target": relationship.target_table,
                    **asdict(relationship),
                }
                for table in plan.tables
                for relationship in table.relationships
            ],
        }
        return GeneratedPackage(files, manifest, entrypoint=f"{wrapper_module}.py")

    @staticmethod
    def _base_source() -> str:
        return (
            "from sqlalchemy.orm import DeclarativeBase\n\n\n"
            "class Base(DeclarativeBase):\n"
            "    pass\n"
        )

    def _table_source(self, plan: AnalysisPlan, table: TableSpec) -> str:
        imports = {
            "BigInteger",
            "Boolean",
            "Date",
            "ForeignKey",
            "Numeric",
            "Text",
        }
        lines = [
            "from __future__ import annotations",
            "",
            "from datetime import date",
            "from decimal import Decimal",
            "",
            f"from sqlalchemy import {', '.join(sorted(imports))}",
            "from sqlalchemy.orm import Mapped, mapped_column, relationship",
            "",
            "from .base import Base",
            "",
            "",
            f"class {table.class_name}(Base):",
            f"    __tablename__ = {table.name!r}",
            f'    __table_args__ = {{"schema": {table.schema!r}}}',
            "",
        ]
        relationships_by_column = {
            column: relationship
            for relationship in table.relationships
            for column in relationship.local_columns
        }
        scalar_attrs: dict[str, str] = {}
        for column in table.columns:
            scalar_attribute = table.scalar_attribute(column.name)
            scalar_attrs[column.name] = scalar_attribute
            py_type, sql_type = _TYPE[column.type]
            if column.nullable:
                py_type += " | None"
            args = [repr(column.name), sql_type]
            relationship = relationships_by_column.get(column.name)
            if relationship is not None and relationship.condition is None:
                target = plan.table(relationship.target_table)
                position = relationship.local_columns.index(column.name)
                remote = relationship.remote_columns[position]
                args.append(
                    f"ForeignKey({target.schema + '.' + target.name + '.' + remote!r})"
                )
            keywords = [
                f"primary_key={column.primary_key!r}",
                f"nullable={column.nullable!r}",
            ]
            lines.append(
                f"    {scalar_attribute}: Mapped[{py_type}] = mapped_column({', '.join(args + keywords)})"
            )
        if table.relationships:
            lines.append("")
        for relationship in table.relationships:
            target = plan.table(relationship.target_table)
            nullable = any(
                table.column(column).nullable for column in relationship.local_columns
            )
            if relationship.many:
                annotation = f'list["{target.class_name}"]'
            else:
                target_type = target.class_name + (" | None" if nullable else "")
                annotation = repr(target_type)
            if relationship.condition is not None:
                primaryjoin = _orm_predicate(plan, relationship.condition)
                if relationship.secondary:
                    bridge = plan.table(relationship.secondary)
                    foreign_columns = _predicate_columns(
                        relationship.condition
                    ) | _predicate_columns(relationship.secondary_condition)
                    foreign_columns = sorted(
                        (
                            value
                            for value in foreign_columns
                            if value.table == bridge.name
                        ),
                        key=lambda value: value.column,
                    )
                else:
                    foreign_columns = [
                        ColumnValue(table.name, column)
                        for column in relationship.local_columns
                    ]
                foreign = (
                    "["
                    + ", ".join(_orm_value(plan, value) for value in foreign_columns)
                    + "]"
                )
                lines.extend(
                    [
                        f"    {relationship.attribute}: Mapped[{annotation}] = relationship(",
                        f"        {target.class_name!r},",
                        f"        primaryjoin={primaryjoin!r},",
                        f"        foreign_keys={foreign!r},",
                    ]
                )
                if relationship.secondary:
                    lines.extend(
                        [
                            f"        secondary=lambda: Base.metadata.tables[{bridge.schema + '.' + bridge.name!r}],",
                            f"        secondaryjoin={_orm_predicate(plan, relationship.secondary_condition)!r},",
                        ]
                    )
                lines.extend(
                    [
                        f"        uselist={relationship.many!r},",
                        '        viewonly=True, lazy="raise",',
                        "    )",
                    ]
                )
                continue
            foreign = ", ".join(
                scalar_attrs[column] for column in relationship.local_columns
            )
            comparisons = [
                f"{table.class_name}.{scalar_attrs[local]} == "
                f"{target.class_name}.{target.scalar_attribute(remote)}"
                for local, remote in zip(
                    relationship.local_columns, relationship.remote_columns, strict=True
                )
            ]
            primaryjoin = (
                comparisons[0]
                if len(comparisons) == 1
                else "and_(" + ", ".join(comparisons) + ")"
            )
            lines.extend(
                [
                    f"    {relationship.attribute}: Mapped[{annotation}] = relationship(",
                    f"        {target.class_name!r},",
                    f"        foreign_keys=[{foreign}],",
                    f"        primaryjoin={primaryjoin!r},",
                    f"        uselist={relationship.many!r},",
                    '        lazy="raise",',
                    "    )",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _analysis_source(self, plan: AnalysisPlan) -> str:
        table_by_name = {table.name: table for table in plan.tables}
        operator_imports = ", ".join(_operator_imports(plan))
        lines = [
            "from __future__ import annotations",
            "",
            "from dataclasses import dataclass",
            "from datetime import date",
            "from decimal import Decimal",
            "",
            "from sqlalchemy import Text, and_, cast, func, or_, select",
            "from sqlalchemy.orm import Session, selectinload",
            "from sqlalchemy.orm.attributes import set_committed_value",
            "",
            f"from engine.deterministic.operators import {operator_imports}",
        ]
        for table in plan.tables:
            lines.append(f"from .{table.attribute} import {table.class_name}")
        lines.extend(["", ""])

        stage_tables: dict[str, dict[str, str]] = {}
        stage_values: dict[str, set[str]] = {}
        row_classes: dict[str, str] = {}
        previous_tables: dict[str, str] = {}
        previous_values: set[str] = set()
        for view in plan.views:
            class_name = _class_name(view.name)
            row_classes[view.name] = class_name
            if isinstance(view, CombinedView):
                current_tables = {
                    name: table_by_name[name].attribute for name in view.tables
                }
                current_values: set[str] = set()
            elif isinstance(view, EnrichedView):
                current_tables = dict(previous_tables)
                for enrichment in view.enrichments:
                    current_tables[enrichment.target_table] = table_by_name[
                        enrichment.target_table
                    ].attribute
                current_values = set(previous_values)
            elif isinstance(view, (FilteredView, SortedView)):
                stage_tables[view.name] = dict(previous_tables)
                stage_values[view.name] = set(previous_values)
                previous_tables, previous_values = (
                    dict(previous_tables),
                    set(previous_values),
                )
                continue
            elif isinstance(view, CalculatedView):
                current_tables = dict(previous_tables)
                current_values = previous_values | {value.name for value in view.values}
            elif isinstance(view, WindowView):
                current_tables = dict(previous_tables)
                current_values = previous_values | {view.output}
            elif isinstance(view, ProjectedView):
                current_tables = {}
                current_values = {value.name for value in view.values}
            else:
                current_tables = {}
                current_values = {value.name for value in view.group_by} | {
                    aggregate.name for aggregate in view.aggregates
                }
            stage_tables[view.name] = current_tables
            stage_values[view.name] = current_values
            fields = [
                (attribute, table_by_name[name].class_name)
                for name, attribute in current_tables.items()
            ] + [(name, "object") for name in sorted(current_values)]
            lines.extend(["@dataclass(frozen=True)", f"class {class_name}:"])
            if fields:
                lines.extend(f"    {name}: {type_name}" for name, type_name in fields)
            else:
                lines.append("    pass")
            lines.extend(["", ""])
            previous_tables, previous_values = current_tables, current_values

        lines.extend(
            [
                "@dataclass(frozen=True)",
                "class AnalysisResult:",
                "    result: View",
                "    views: tuple[View, ...]",
                "",
                "",
                "class " + _wrapper_class(plan) + ":",
                "    def __init__(self, session: Session, row_limit: int | None = None):",
                "        self.session = session",
                "        self.row_limit = row_limit",
                "",
                f"    def {plan.slug}(self) -> AnalysisResult:",
            ]
        )

        previous = None
        emitted_variables = []
        for view in plan.views:
            lines.append(f"        # View: {view.name}")
            if isinstance(view, CombinedView):
                lines.extend(self._emit_combined(plan, view, row_classes[view.name]))
            elif isinstance(view, EnrichedView):
                lines.extend(
                    self._emit_enriched(
                        plan,
                        view,
                        row_classes[view.name],
                        stage_tables[view.source],
                        stage_values[view.source],
                    )
                )
            elif isinstance(view, FilteredView):
                expression = self._python_predicate(
                    plan,
                    view.predicate,
                    "row",
                    stage_tables[view.source],
                    stage_values[view.source],
                )
                lines.extend(
                    [
                        f"        {view.name} = {view.source}.filter(",
                        f"            name={view.name!r},",
                        f"            where=lambda row: {expression},",
                        "        )",
                    ]
                )
            elif isinstance(view, CalculatedView):
                lines.extend(
                    self._emit_calculated(
                        plan,
                        view,
                        row_classes[view.name],
                        stage_tables[view.source],
                        stage_values[view.source],
                    )
                )
            elif isinstance(view, ProjectedView):
                lines.extend(
                    self._emit_projected(
                        plan,
                        view,
                        row_classes[view.name],
                        stage_tables[view.source],
                        stage_values[view.source],
                    )
                )
            elif isinstance(view, ReducedView):
                lines.extend(
                    self._emit_reduced(
                        plan,
                        view,
                        row_classes[view.name],
                        stage_tables[view.source],
                        stage_values[view.source],
                    )
                )
            elif isinstance(view, SortedView):
                keys = [
                    self._python_value(
                        plan,
                        item.value,
                        "row",
                        stage_tables[view.source],
                        stage_values[view.source],
                    )
                    for item in view.order
                ]
                lines.extend(
                    [
                        f"        {view.name} = {view.source}.sort(",
                        f"            name={view.name!r},",
                        f"            keys=lambda row: ({', '.join(keys)},),",
                        f"            descending={tuple(item.descending for item in view.order)!r},",
                        f"            limit={view.limit!r},",
                        "        )",
                    ]
                )
            elif isinstance(view, WindowView):
                lines.extend(
                    self._emit_window(
                        plan,
                        view,
                        row_classes[view.name],
                        stage_tables[view.source],
                        stage_values[view.source],
                    )
                )
            lines.append("")
            previous = view.name
            emitted_variables.append(view.name)
        lines.extend(
            [
                "        return AnalysisResult(",
                f"            result={previous},",
                "            views=(",
                *(f"                {name}," for name in emitted_variables),
                "            ),",
                "        )",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    def _emit_combined(
        self, plan: AnalysisPlan, view: CombinedView, row_class: str
    ) -> list[str]:
        tables = [plan.table(name) for name in view.tables]
        lines = [
            "        statement = (",
            "            select("
            + ", ".join(table.class_name for table in tables)
            + ")",
        ]
        joined = {tables[0].name}
        relationship_assignments: list[tuple[TableSpec, str, TableSpec]] = []
        for table in tables[1:]:
            source, relationship = plan.connecting_relationship(joined, table.name)
            target = plan.table(relationship.target_table)
            relationship_assignments.append(
                (source, relationship.attribute, target)
            )
            if relationship.condition is not None:
                method = "outerjoin" if view.outer else "join"
                if relationship.secondary:
                    bridge = plan.table(relationship.secondary)
                    lines.append(
                        f"            .{method}({bridge.class_name}, {_orm_predicate(plan, relationship.condition)})"
                    )
                    condition = relationship.secondary_condition
                else:
                    condition = relationship.condition
                lines.append(
                    f"            .{method}({table.class_name}, {_orm_predicate(plan, condition)})"
                )
                joined.add(table.name)
                continue
            comparisons = []
            for local, remote in zip(
                relationship.local_columns, relationship.remote_columns
            ):
                comparisons.append(
                    f"{source.class_name}.{source.scalar_attribute(local)} == "
                    f"{target.class_name}.{target.scalar_attribute(remote)}"
                )
            on_clause = (
                comparisons[0]
                if len(comparisons) == 1
                else "and_(" + ", ".join(comparisons) + ")"
            )
            lines.append(
                f"            .{'outerjoin' if view.outer else 'join'}({table.class_name}, {on_clause})"
            )
            joined.add(table.name)
        loader_options = self._loader_options(plan)
        if loader_options:
            lines.extend(
                [
                    "            .options(",
                    *(f"                {option}," for option in loader_options),
                    "            )",
                ]
            )
        lines.extend(
            [
                "        )",
                "        if self.row_limit is not None:",
                "            statement = statement.limit(self.row_limit + 1)",
                f"        def emit_{view.name}("
                + ", ".join(table.attribute for table in tables)
                + "):",
                *(
                    line
                    for source, relationship_attribute, target in relationship_assignments
                    for line in (
                        f"            if {source.attribute} is not None:",
                        f"                set_committed_value({source.attribute}, {relationship_attribute!r}, {target.attribute})",
                    )
                ),
                f"            return {row_class}(",
                *(
                    f"                {table.attribute}={table.attribute},"
                    for table in tables
                ),
                "            )",
                "",
                f"        {view.name} = View.from_orm(",
                f"            name={view.name!r},",
                "            rows=self.session.execute(statement),",
                f"            construct=emit_{view.name},",
                "            row_limit=self.row_limit,",
                "        )",
            ]
        )
        return lines

    @staticmethod
    def _loader_options(plan: AnalysisPlan) -> tuple[str, ...]:
        """Emit only the relationship paths traversed by enrichment stages.

        Mappings use ``lazy='raise'`` so an omitted path fails instead of silently
        introducing an N+1 query. Keeping only maximal paths avoids redundant
        select-in options when both ``order.city`` and ``order.city.country`` are
        materialized.
        """
        combined = plan.views[0]
        roots = {table_name: (table_name, ()) for table_name in combined.tables}
        requested = set()
        for view in plan.views:
            if not isinstance(view, EnrichedView):
                continue
            for enrichment in view.enrichments:
                source_name = enrichment.source_table
                remaining = enrichment.relationship_path
                current = plan.table(source_name)
                # If a path passes through an object already selected by the
                # combined query, continue from that root instead of loading it
                # a second time (Order.customer.country -> Customer.country).
                while remaining:
                    target_name = current.relationship(remaining[0]).target_table
                    if target_name not in roots:
                        break
                    source_name = target_name
                    remaining = remaining[1:]
                    current = plan.table(source_name)
                root, prefix = roots[source_name]
                full_path = prefix + remaining
                roots[enrichment.target_table] = (root, full_path)
                requested.add((root, full_path))
        maximal = sorted(
            (
                item
                for item in requested
                if not any(
                    item[0] == other[0]
                    and len(item[1]) < len(other[1])
                    and other[1][: len(item[1])] == item[1]
                    for other in requested
                )
            ),
            key=lambda item: (item[0], item[1]),
        )
        options = []
        for source_name, path in maximal:
            current = plan.table(source_name)
            expression = f"selectinload({current.class_name}.{path[0]})"
            current = plan.table(current.relationship(path[0]).target_table)
            for relationship_name in path[1:]:
                relationship = current.relationship(relationship_name)
                expression += (
                    f".selectinload({current.class_name}.{relationship_name})"
                )
                current = plan.table(relationship.target_table)
            options.append(expression)
        return tuple(options)

    def _emit_enriched(
        self,
        plan: AnalysisPlan,
        view: EnrichedView,
        row_class: str,
        source_tables: Mapping[str, str],
        source_values: set[str],
    ) -> list[str]:
        helper = f"emit_{view.name}"
        lines = [f"        def {helper}(row):"]
        materialized_objects = {
            table_name: f"row.{attribute}"
            for table_name, attribute in source_tables.items()
        }
        for enrichment in view.enrichments:
            expression = materialized_objects[enrichment.source_table]
            current_table = plan.table(enrichment.source_table)
            target_attribute = plan.table(enrichment.target_table).attribute
            for index, relationship in enumerate(enrichment.relationship_path):
                target_table = current_table.relationship(relationship).target_table
                # An absent intermediate reference behaves like the SQL inner join.
                variable = f"_{target_attribute}_ref_{index}"
                if target_table in materialized_objects:
                    lines.append(
                        f"            {variable} = {materialized_objects[target_table]}"
                    )
                else:
                    lines.append(
                        f"            {variable} = None if {expression} is None else {expression}.{relationship}"
                    )
                if enrichment.required:
                    lines.extend(
                        [
                            f"            if {variable} is None:",
                            "                return None",
                        ]
                    )
                expression = variable
                current_table = plan.table(target_table)
            lines.append(f"            {target_attribute} = {expression}")
            materialized_objects[enrichment.target_table] = target_attribute
        lines.extend(
            [
                f"            return {row_class}(",
                *(
                    f"                {attribute}=row.{attribute},"
                    for attribute in source_tables.values()
                ),
                *(
                    f"                {name}=row.{name},"
                    for name in sorted(source_values)
                ),
                *(
                    f"                {plan.table(item.target_table).attribute}={plan.table(item.target_table).attribute},"
                    for item in view.enrichments
                ),
                "            )",
                "",
                f"        {view.name} = {view.source}.for_each(",
                f"            name={view.name!r},",
                f"            emit={helper},",
                "        )",
            ]
        )
        return lines

    def _emit_calculated(
        self,
        plan: AnalysisPlan,
        view: CalculatedView,
        row_class: str,
        source_tables: Mapping[str, str],
        source_values: set[str],
    ) -> list[str]:
        lines = [
            f"        {view.name} = {view.source}.for_each(",
            f"            name={view.name!r},",
            f"            emit=lambda row: {row_class}(",
            *(
                f"                {attribute}=row.{attribute},"
                for attribute in source_tables.values()
            ),
            *(f"                {name}=row.{name}," for name in sorted(source_values)),
        ]
        for selected in view.values:
            expression = self._python_value(
                plan, selected.value, "row", source_tables, source_values
            )
            lines.append(f"                {selected.name}={expression},")
        lines.extend(["            ),", "        )"])
        return lines

    def _emit_window(self, plan, view, row_class, source_tables, source_values):
        def value(item, row):
            return self._python_value(plan, item, row, source_tables, source_values)

        predicates = [
            f"EQ({value(key, 'prior')}, {value(key, 'row')}) is True"
            for key in view.partition
        ]
        if view.function == "yoy":
            predicates.append(
                f"EQ({value(view.time, 'row')}, ADD({value(view.time, 'prior')}, 1)) is True"
            )
        elif view.function == "running":
            predicates.append(
                f"LE({value(view.time, 'prior')}, {value(view.time, 'row')}) is True"
            )
        condition = " and ".join(predicates) or "True"
        fields = [f"{attr}=row.{attr}" for attr in source_tables.values()] + [
            f"{name}=row.{name}" for name in sorted(source_values)
        ]
        helper = f"emit_{view.name}"
        lines = [f"        def {helper}(row):"]
        if view.function == "yoy":
            expression = f"DIVIDE(SUBTRACT({value(view.measure, 'row')}, {value(view.measure, 'prior')}), {value(view.measure, 'prior')})"
            constructor = ", ".join(fields + [f"{view.output}={expression}"])
            lines.extend(
                [
                    f"            return [{row_class}({constructor})",
                    f"                    for prior in {view.source}.rows if {condition}]",
                ]
            )
        else:
            lines.extend(
                [
                    f"            total = {view.source}.reduce(",
                    f"                name={view.name + '_sum'!r}, initial=None,",
                    f"                step=lambda total, prior: SUM(total, {value(view.measure, 'prior')}) if {condition} else total,",
                    "            ).rows[0]",
                ]
            )
            expression = (
                "total"
                if view.function == "running"
                else f"DIVIDE({value(view.measure, 'row')}, total)"
            )
            lines.append(
                f"            return {row_class}({', '.join(fields + [f'{view.output}={expression}'])})"
            )
        lines.extend(
            [
                "",
                f"        {view.name} = {view.source}.for_each(name={view.name!r}, emit={helper})",
            ]
        )
        return lines

    def _emit_reduced(
        self,
        plan: AnalysisPlan,
        view: ReducedView,
        row_class: str,
        source_tables: Mapping[str, str],
        source_values: set[str],
    ) -> list[str]:
        initial_values = [
            f"{aggregate.name}={self._aggregate_initial(aggregate)}"
            for aggregate in view.aggregates
        ]
        if not view.group_by:
            lines = [
                f"        {view.name} = {view.source}.reduce(",
                f"            name={view.name!r},",
                f"            initial={row_class}({', '.join(initial_values)}),",
                f"            step=lambda result, row: {row_class}(",
            ]
            for aggregate in view.aggregates:
                lines.append(
                    f"                {aggregate.name}={self._aggregate_step(plan, aggregate, source_tables, source_values)},"
                )
            lines.extend(["            ),"])
            lines.extend(
                self._aggregate_finalize(view, row_class, indent="            ")
            )
            lines.append("        )")
            return lines

        key_values = [
            self._python_value(
                plan, selected.value, "row", source_tables, source_values
            )
            for selected in view.group_by
        ]
        key = (
            key_values[0] if len(key_values) == 1 else "(" + ", ".join(key_values) + ")"
        )
        initial_fields = [
            f"{selected.name}={self._python_value(plan, selected.value, 'row', source_tables, source_values)}"
            for selected in view.group_by
        ] + initial_values
        step_fields = [
            f"{selected.name}=result.{selected.name}" for selected in view.group_by
        ]
        step_fields.extend(
            f"{aggregate.name}={self._aggregate_step(plan, aggregate, source_tables, source_values)}"
            for aggregate in view.aggregates
        )
        lines = [
            f"        {view.name} = {view.source}.group_reduce(",
            f"            name={view.name!r},",
            f"            key=lambda row: {key},",
            f"            initial=lambda row: {row_class}({', '.join(initial_fields)}),",
            f"            step=lambda result, row: {row_class}(",
            *(f"                {field}," for field in step_fields),
            "            ),",
        ]
        lines.extend(self._aggregate_finalize(view, row_class, indent="            "))
        lines.append("        )")
        return lines

    def _emit_projected(
        self,
        plan: AnalysisPlan,
        view: ProjectedView,
        row_class: str,
        source_tables: Mapping[str, str],
        source_values: set[str],
    ) -> list[str]:
        lines = [
            f"        {view.name} = {view.source}.for_each(",
            f"            name={view.name!r},",
            f"            emit=lambda row: {row_class}(",
        ]
        for selected in view.values:
            expression = self._python_value(
                plan, selected.value, "row", source_tables, source_values
            )
            lines.append(f"                {selected.name}={expression},")
        lines.extend(["            ),", "        )"])
        return lines

    @staticmethod
    def _aggregate_finalize(
        view: ReducedView, row_class: str, *, indent: str
    ) -> list[str]:
        if not any(aggregate.function == "AVG" for aggregate in view.aggregates):
            return []
        fields = [f"{item.name}=result.{item.name}" for item in view.group_by]
        fields.extend(
            f"{aggregate.name}="
            + (
                f"FINALIZE_AVG(result.{aggregate.name})"
                if aggregate.function == "AVG"
                else f"result.{aggregate.name}"
            )
            for aggregate in view.aggregates
        )
        return [
            f"{indent}finalize=lambda result: {row_class}(",
            *(f"{indent}    {field}," for field in fields),
            f"{indent}),",
        ]

    def _aggregate_step(
        self,
        plan: AnalysisPlan,
        aggregate: AggregateValue,
        source_tables: Mapping[str, str],
        source_values: set[str],
    ) -> str:
        if aggregate.function == "COUNT" and aggregate.operand is None:
            return f"COUNT_STAR(result.{aggregate.name})"
        operand = self._python_value(
            plan, aggregate.operand, "row", source_tables, source_values
        )
        return f"{aggregate.function}(result.{aggregate.name}, {operand})"

    @staticmethod
    def _aggregate_initial(aggregate: AggregateValue) -> str:
        if aggregate.function == "COUNT":
            return "0"
        if aggregate.function == "AVG":
            return "AverageState()"
        return "None"

    def _python_predicate(
        self,
        plan: AnalysisPlan,
        predicate: PredicateValue,
        row: str,
        tables: Mapping[str, str],
        values: set[str],
    ) -> str:
        if isinstance(predicate, JunctionValue):
            return (
                predicate.operator
                + "("
                + ", ".join(
                    self._python_predicate(plan, child, row, tables, values)
                    for child in predicate.predicates
                )
                + ")"
            )
        left = self._python_value(plan, predicate.left, row, tables, values)
        right = self._python_value(plan, predicate.right, row, tables, values)
        return f"{_PREDICATE_FUNCTION[predicate.operator]}({left}, {right})"

    def _python_value(
        self,
        plan: AnalysisPlan,
        value: Value,
        row: str,
        tables: Mapping[str, str],
        values: set[str],
    ) -> str:
        if isinstance(value, FunctionValue):
            return f"{value.function}({self._python_value(plan, value.operand, row, tables, values)})"
        if isinstance(value, LiteralValue):
            return _python_literal(value.value)
        if isinstance(value, ViewValue):
            if value.name not in values:
                raise ValueError(f"view value {value.name!r} is not available")
            return f"{row}.{value.name}"
        if isinstance(value, ColumnValue):
            table = plan.table(value.table)
            if value.table not in tables:
                raise ValueError(f"table {value.table!r} is not available in this view")
            expression = (
                f"{row}.{tables[value.table]}.{table.scalar_attribute(value.column)}"
            )
            if plan.views[0].outer or any(
                isinstance(view, EnrichedView)
                and any(not enrichment.required for enrichment in view.enrichments)
                for view in plan.views
            ):
                return (
                    f"(None if {row}.{tables[value.table]} is None else {expression})"
                )
            return expression
        if isinstance(value, BinaryValue):
            left = self._python_value(plan, value.left, row, tables, values)
            right = self._python_value(plan, value.right, row, tables, values)
            return f"{_BINARY_FUNCTION[value.operator]}({left}, {right})"
        raise TypeError(f"unsupported Python value: {type(value).__name__}")


def _class_name(value: str) -> str:
    pieces = [piece for piece in value.strip("_").split("_") if piece]
    name = "".join(piece[:1].upper() + piece[1:] for piece in pieces) or "Generated"
    if keyword.iskeyword(name.lower()) or not name.isidentifier():
        name = "Generated" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return name


def _wrapper_module(plan: AnalysisPlan) -> str:
    combined = plan.views[0]
    name = "_".join(plan.table(table_name).attribute for table_name in combined.tables)
    if name in {table.attribute for table in plan.tables}:
        return name + "_analysis"
    return name


def _wrapper_class(plan: AnalysisPlan) -> str:
    return _class_name(_wrapper_module(plan))


def _python_literal(value: object) -> str:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal literal")
        return f"Decimal({str(value)!r})"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float literal")
        return repr(value)
    if isinstance(value, date):
        return f"date({value.year}, {value.month}, {value.day})"
    if value is None or isinstance(value, (str, int, bool)):
        return repr(value)
    raise TypeError(f"unsupported Python literal: {type(value).__name__}")


def _operator_imports(plan: AnalysisPlan) -> tuple[str, ...]:
    names = {"View"}

    def add_value(value: Value) -> None:
        if isinstance(value, FunctionValue):
            names.add(value.function)
            add_value(value.operand)
        if isinstance(value, BinaryValue):
            names.add(_BINARY_FUNCTION[value.operator])
            add_value(value.left)
            add_value(value.right)

    def add_predicate(predicate):
        if isinstance(predicate, JunctionValue):
            names.add(predicate.operator)
            for child in predicate.predicates:
                add_predicate(child)
        else:
            names.add(_PREDICATE_FUNCTION[predicate.operator])
            add_value(predicate.left)
            add_value(predicate.right)

    for view in plan.views:
        if isinstance(view, FilteredView):
            add_predicate(view.predicate)
        elif isinstance(view, (CalculatedView, ProjectedView)):
            for selected in view.values:
                add_value(selected.value)
        elif isinstance(view, ReducedView):
            for selected in view.group_by:
                add_value(selected.value)
            for aggregate in view.aggregates:
                if aggregate.function == "COUNT":
                    names.add("COUNT_STAR" if aggregate.operand is None else "COUNT")
                else:
                    names.add(aggregate.function)
                if aggregate.function == "AVG":
                    names.update({"AverageState", "FINALIZE_AVG"})
                if aggregate.operand is not None:
                    add_value(aggregate.operand)
        elif isinstance(view, WindowView):
            names.update({"SUM", "EQ", "LE", "ADD", "SUBTRACT", "DIVIDE"})
    return tuple(sorted(names))


def _orm_value(plan, value):
    if isinstance(value, ColumnValue):
        table = plan.table(value.table)
        return f"{table.class_name}.{table.scalar_attribute(value.column)}"
    if isinstance(value, FunctionValue):
        operand = _orm_value(plan, value.operand)
        return (
            f"cast({operand}, Text)"
            if value.function == "TEXT"
            else f"func.lower({operand})"
        )
    if isinstance(value, LiteralValue):
        return _python_literal(value.value)
    if isinstance(value, BinaryValue):
        return (
            f"({_orm_value(plan, value.left)} {value.operator} "
            f"{_orm_value(plan, value.right)})"
        )
    raise TypeError(f"unsupported ORM join value: {type(value).__name__}")


def _orm_predicate(plan, predicate):
    if isinstance(predicate, JunctionValue):
        return (
            predicate.operator.lower()
            + "_("
            + ", ".join(_orm_predicate(plan, child) for child in predicate.predicates)
            + ")"
        )
    # SQLAlchemy evaluates declarative join strings as Python expressions. SQL's
    # IS/IS NOT therefore map to equality/inequality; leaving the SQL spelling in
    # generated model source would be invalid Python syntax.
    operator = {
        "=": "==",
        "<>": "!=",
        "IS": "==",
        "IS NOT": "!=",
    }.get(predicate.operator, predicate.operator)
    return f"{_orm_value(plan, predicate.left)} {operator} {_orm_value(plan, predicate.right)}"


def _predicate_columns(predicate):
    def columns(value):
        if isinstance(value, ColumnValue):
            return {value}
        if isinstance(value, FunctionValue):
            return columns(value.operand)
        if isinstance(value, BinaryValue):
            return columns(value.left) | columns(value.right)
        return set()

    if isinstance(predicate, JunctionValue):
        return set().union(
            *(_predicate_columns(child) for child in predicate.predicates)
        )
    return columns(predicate.left) | columns(predicate.right)
