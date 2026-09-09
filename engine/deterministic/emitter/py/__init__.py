"""Emit readable, executable SQLAlchemy models and a feed-forward Python analysis."""

from __future__ import annotations

import ast
import hashlib
import json
import keyword
import math
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
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
    LiteralValue,
    PredicateValue,
    ProjectedView,
    ReducedView,
    TableSpec,
    Value,
    ViewValue,
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
    VERSION = 1

    def emit(
        self,
        plan: AnalysisPlan,
        *,
        dataset_version: str | None = None,
        knowledgebase_release: str | None = None,
    ) -> GeneratedPackage:
        wrapper_module = _wrapper_module(plan)
        wrapper_class = _wrapper_class(plan)
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
                    "attribute": relationship.attribute,
                    "target": relationship.target_table,
                    "local_columns": list(relationship.local_columns),
                    "remote_columns": list(relationship.remote_columns),
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
            scalar_attribute = self._scalar_attribute(table, column.name)
            scalar_attrs[column.name] = scalar_attribute
            py_type, sql_type = _TYPE[column.type]
            if column.nullable:
                py_type += " | None"
            args = [repr(column.name), sql_type]
            relationship = relationships_by_column.get(column.name)
            if relationship is not None:
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
            foreign = ", ".join(
                scalar_attrs[column] for column in relationship.local_columns
            )
            lines.extend(
                [
                    f"    {relationship.attribute}: Mapped[{annotation}] = relationship(",
                    f"        {target.class_name!r},",
                    f"        foreign_keys=[{foreign}],",
                    f"        uselist={relationship.many!r},",
                    '        lazy="selectin",',
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
            "from sqlalchemy import and_, select",
            "from sqlalchemy.orm import Session",
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
            elif isinstance(view, FilteredView):
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
                "    def __init__(self, session: Session):",
                "        self.session = session",
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
        for table in tables[1:]:
            edge = self._connecting_relationship(plan, joined, table.name)
            if edge is None:
                raise ValueError(f"combined view cannot connect table {table.name}")
            source, relationship = edge
            target = plan.table(relationship.target_table)
            comparisons = []
            for local, remote in zip(
                relationship.local_columns, relationship.remote_columns
            ):
                comparisons.append(
                    f"{source.class_name}.{self._scalar_attribute(source, local)} == "
                    f"{target.class_name}.{self._scalar_attribute(target, remote)}"
                )
            on_clause = (
                comparisons[0]
                if len(comparisons) == 1
                else "and_(" + ", ".join(comparisons) + ")"
            )
            lines.append(f"            .join({table.class_name}, {on_clause})")
            joined.add(table.name)
        lines.extend(
            [
                "        )",
                f"        {view.name} = View.from_orm(",
                f"            name={view.name!r},",
                "            rows=self.session.execute(statement),",
                "            construct=lambda "
                + ", ".join(table.attribute for table in tables)
                + f": {row_class}(",
                *(
                    f"                {table.attribute}={table.attribute},"
                    for table in tables
                ),
                "            ),",
                "        )",
            ]
        )
        return lines

    def _emit_enriched(
        self,
        plan: AnalysisPlan,
        view: EnrichedView,
        row_class: str,
        source_tables: Mapping[str, str],
    ) -> list[str]:
        helper = f"emit_{view.name}"
        lines = [f"        def {helper}(row):"]
        for enrichment in view.enrichments:
            expression = "row." + source_tables[enrichment.source_table]
            for relationship in enrichment.relationship_path:
                expression += "." + relationship
            target_attribute = plan.table(enrichment.target_table).attribute
            lines.extend(
                [
                    f"            {target_attribute} = {expression}",
                    f"            if {target_attribute} is None:",
                    "                return None",
                ]
            )
        lines.extend(
            [
                f"            return {row_class}(",
                *(
                    f"                {attribute}=row.{attribute},"
                    for attribute in source_tables.values()
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
            return f"{row}.{tables[value.table]}.{self._scalar_attribute(table, value.column)}"
        if isinstance(value, BinaryValue):
            left = self._python_value(plan, value.left, row, tables, values)
            right = self._python_value(plan, value.right, row, tables, values)
            return f"{_BINARY_FUNCTION[value.operator]}({left}, {right})"
        raise TypeError(f"unsupported Python value: {type(value).__name__}")

    @staticmethod
    def _scalar_attribute(table: TableSpec, column_name: str) -> str:
        column = table.column(column_name)
        if any(
            relationship.attribute == column.attribute
            and column_name in relationship.local_columns
            for relationship in table.relationships
        ):
            return f"_{column.attribute}_value"
        return column.attribute

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
        if isinstance(value, BinaryValue):
            names.add(_BINARY_FUNCTION[value.operator])
            add_value(value.left)
            add_value(value.right)

    for view in plan.views:
        if isinstance(view, FilteredView):
            names.add(_PREDICATE_FUNCTION[view.predicate.operator])
            add_value(view.predicate.left)
            add_value(view.predicate.right)
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
    return tuple(sorted(names))
