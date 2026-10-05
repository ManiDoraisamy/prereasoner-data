"""Public orchestration API for deterministic dual emission and execution."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Connection, Engine

from engine import request_timing
from engine.deterministic.emitter import (
    GeneratedPackage,
    GeneratedSQL,
    PythonEmitter,
    SQLEmitter,
)
from engine.deterministic.plan import (
    AnalysisPlan,
    AntiJoinView,
    BinaryValue,
    ColumnValue,
    FilteredView,
    FunctionValue,
    JunctionValue,
    LiteralValue,
    SortedView,
    SpanValue,
    ViewValue,
)
from engine.deterministic.runtime import (
    ExecutionMode,
    VerificationMismatch,
    assert_equivalent,
    choose_execution_mode,
    debug_generation_enabled,
    execute_python,
    execute_sql_views,
    materialized_python_views,
)

from engine.numeric import wire_value

DEFAULT_DEBUG_ROOT = Path(__file__).with_name("_gen") / "py"
_ENTITY = re.compile(r"Q\d+")


def _wire_cell(value):
    """A displayed cell as the wire carries it (engine.numeric): an exact JSON scalar for a decimal, ISO text
    for a date. The server and the stream used to convert these on the way out and log each as a leak."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return wire_value(value)


def _operand(value):
    # LOWER and TEXT are storage details, not the condition. MONTH is the condition: "signed in August" compares
    # month(signed), and a label without it read "where signed = 8" (2026-10-02).
    while isinstance(value, FunctionValue) and value.function in {"LOWER", "TEXT"}:
        value = value.operand
    return value


def _names_reference(value, references):
    value = _operand(value)
    if isinstance(value, ColumnValue):
        return f"{value.table}__{value.column}" in references
    return isinstance(value, ViewValue) and value.name in references


def _comparisons(predicate):
    if isinstance(predicate, JunctionValue):
        for child in predicate.predicates:
            yield from _comparisons(child)
    else:
        yield predicate


def _filter_entities(predicate, references):
    """The stored entities a filter compares with a reference column; its label shows them resolved."""
    for comparison in _comparisons(predicate):
        for side, other in ((comparison.left, comparison.right), (comparison.right, comparison.left)):
            literal = _operand(other)
            if (_names_reference(side, references) and isinstance(literal, LiteralValue)
                    and isinstance(literal.value, str) and _ENTITY.fullmatch(literal.value)):
                yield literal.value


def _term(value, labels):
    value = _operand(value)
    if isinstance(value, ColumnValue):
        return value.column
    if isinstance(value, ViewValue):
        return value.name
    if isinstance(value, LiteralValue):
        if value.value is None:
            return "NULL"
        if isinstance(value.value, str):
            return repr(labels.get(value.value, value.value))
        return str(value.value)
    if isinstance(value, BinaryValue):
        return f"({_term(value.left, labels)} {value.operator} {_term(value.right, labels)})"
    if isinstance(value, FunctionValue):
        return f"{value.function.lower()}({_term(value.operand, labels)})"
    if isinstance(value, SpanValue):
        until = value.until if value.end is None else f"{_term(value.end, labels)} or {value.until}"
        return f"{value.unit}s from {_term(value.start, labels)} to {until}"
    return str(value)


def _condition(predicate, labels, references):
    """A filter's predicate as its step label names it (docs/SHEETS_AS_REASONING.md, step 4 and rule 5):
    "country = 'France'" where the program compares the stored Q142. Only a value compared with a
    reference column is shown as a label; an uploaded 'Q1' stays 'Q1'."""
    if isinstance(predicate, JunctionValue):
        return f" {predicate.operator.lower()} ".join(
            f"({_condition(child, labels, references)})" if isinstance(child, JunctionValue)
            else _condition(child, labels, references)
            for child in predicate.predicates
        )
    shown = labels if (_names_reference(predicate.left, references)
                       or _names_reference(predicate.right, references)) else {}
    return f"{_term(predicate.left, shown)} {predicate.operator} {_term(predicate.right, shown)}"


@dataclass(frozen=True)
class DualEmission:
    sql: GeneratedSQL
    python: GeneratedPackage

    def manifest(self) -> dict[str, object]:
        return {
            "slug": self.python.manifest["slug"],
            "dataset_version": self.python.manifest["dataset_version"],
            "knowledgebase_release": self.python.manifest["knowledgebase_release"],
            "relationship_edges": self.python.manifest["relationship_edges"],
            "output": self.python.manifest["output"],
            "sections": self.python.manifest["sections"],
            "emitters": {
                "sql": {
                    "version": self.sql.manifest["emitter_version"],
                    "source_sha256": self.sql.source_sha256,
                },
                "python": {
                    "version": self.python.manifest["emitter_version"],
                    "source_sha256": self.python.source_sha256,
                    "file_sha256": self.python.record()["file_sha256"],
                },
            },
        }


@dataclass(frozen=True)
class ExecutionResult:
    mode: ExecutionMode
    rows: tuple[dict[str, object], ...]
    emission: DualEmission
    view_rows: tuple[tuple[dict[str, object], ...], ...]
    debug_path: Path | None = None
    fallback_reason: str | None = None
    reference_columns: Mapping[str, frozenset[str]] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    step_labels: Mapping[str, str] = field(default_factory=dict)

    def displayed(self, step: str, rows) -> tuple[dict[str, object], ...]:
        """A stage's rows as the user sees them: a knowledgebase entity in a reference column shows its
        label (docs/SHEETS_AS_REASONING.md rule 5). The programs and their parity keep the stored QID."""
        columns = self.reference_columns.get(step, frozenset())
        if not columns or not self.labels:
            return tuple(rows)
        return tuple(
            {
                column: self.labels.get(value, value) if column in columns and isinstance(value, str) else value
                for column, value in row.items()
            }
            for row in rows
        )

    def output_rows(self) -> tuple[dict[str, object], ...]:
        """The output stage's rows as displayed; the Result shows these."""
        return self.displayed(str(self.emission.python.manifest["output"]), self.rows)

    def record(self) -> dict[str, object]:
        sections = {
            section["id"]: section
            for section in self.emission.python.manifest["sections"]
        }
        views = []
        for step, statement, rows in zip(
            self.emission.python.manifest["views"],
            self.emission.sql.statements,
            self.view_rows,
            strict=True,
        ):
            rows = self.displayed(step, rows[:50])
            sql = statement.split(" AS ", 1)[1]
            suffix = self.emission.python.manifest["view_logical_names"][step]
            section_id = self.emission.python.manifest["view_sections"][step]
            section = sections.get(section_id, {})
            views.append(
                {
                    "name": step,
                    "is_output": step == self.emission.python.manifest["output"],
                    "logical_name": suffix,
                    "op": self.emission.python.manifest["view_operations"][step],
                    "inputs": self.emission.python.manifest["view_inputs"][step],
                    "section": section_id,
                    "section_label": section.get("label"),
                    "section_question": section.get("question"),
                    "section_inputs": section.get("inputs", []),
                    "label": self.step_labels.get(step, suffix.replace("_", " ")),
                    "sql": sql,
                    "python": self.emission.python.manifest["view_sources"].get(step, ""),
                    "columns": self.emission.python.manifest["view_columns"][step],
                    "rows": [
                        [
                            _wire_cell(row.get(column))
                            for column in self.emission.python.manifest["view_columns"][
                                step
                            ]
                        ]
                        for row in rows[:50]
                    ],
                }
            )
        return {
            "mode": self.mode.value,
            "timings": {
                key: value
                for key, value in request_timing.snapshot().items()
                if key.startswith("deterministic_")
            },
            "manifest": self.emission.manifest(),
            "sql": self.emission.sql.record(),
            "python": self.emission.python.record(),
            "views": views,
            "final_sql": next(
                view["sql"]
                for view in views
                if view["name"] == self.emission.python.manifest["output"]
            ),
            "debug_path": str(self.debug_path) if self.debug_path is not None else None,
            "fallback_reason": self.fallback_reason,
        }


class DeterministicAnalysis:
    """Emit once from a neutral plan, then run Python, SQL, or both."""

    def __init__(
        self,
        plan: AnalysisPlan,
        *,
        conversation_schema: str,
        dataset_version: str | None = None,
        knowledgebase_release: str | None = None,
        labels: Callable[[Iterable[str]], Mapping[str, str]] | None = None,
    ):
        if not conversation_schema:
            raise ValueError("conversation schema must be non-empty")
        self.plan = plan
        self.conversation_schema = conversation_schema
        self.dataset_version = dataset_version
        self.knowledgebase_release = knowledgebase_release
        # Resolves knowledgebase entities ('Q142') to their labels ('France') for display only.
        self.labels = labels

    def emit(self) -> DualEmission:
        metadata = {
            "dataset_version": self.dataset_version,
            "knowledgebase_release": self.knowledgebase_release,
        }
        return DualEmission(
            sql=SQLEmitter({"conversation": self.conversation_schema}).emit(
                self.plan, **metadata
            ),
            python=PythonEmitter().emit(self.plan, **metadata),
        )

    def run(
        self,
        bind: Engine | Connection,
        *,
        mode: ExecutionMode | str = ExecutionMode.AUTO,
        estimated_rows: int,
        python_row_limit: int = 10_000,
        app_env: str = "production",
        persist_generated: bool = False,
        conversation_id: str | None = None,
        revision: int = 1,
        debug_root: str | Path = DEFAULT_DEBUG_ROOT,
    ) -> ExecutionResult:
        if isinstance(bind, Engine):
            # Both programs and every SQL stage must observe one database snapshot.
            with bind.connect() as connection:
                if connection.dialect.name == "postgresql":
                    connection = connection.execution_options(
                        isolation_level="REPEATABLE READ"
                    )
                with connection.begin():
                    return self.run(
                        connection,
                        mode=mode,
                        estimated_rows=estimated_rows,
                        python_row_limit=python_row_limit,
                        app_env=app_env,
                        persist_generated=persist_generated,
                        conversation_id=conversation_id,
                        revision=revision,
                        debug_root=debug_root,
                    )
        if not bind.in_transaction():
            # A caller-supplied Connection gets the same snapshot/cleanup
            # contract as an Engine. Without this branch begin_nested() would
            # autobegin an outer transaction and leave it open after returning.
            connection = bind
            if connection.dialect.name == "postgresql":
                connection = connection.execution_options(
                    isolation_level="REPEATABLE READ"
                )
            with connection.begin():
                return self.run(
                    connection,
                    mode=mode,
                    estimated_rows=estimated_rows,
                    python_row_limit=python_row_limit,
                    app_env=app_env,
                    persist_generated=persist_generated,
                    conversation_id=conversation_id,
                    revision=revision,
                    debug_root=debug_root,
                )
        with request_timing.span("deterministic_emit"):
            emission = self.emit()
        requested = ExecutionMode(mode)
        selected = choose_execution_mode(
            requested,
            estimated_rows=estimated_rows,
            python_row_limit=python_row_limit,
        )
        debug_path = None
        if debug_generation_enabled(app_env, explicit=persist_generated):
            if conversation_id is None:
                raise ValueError(
                    "persisting generated source requires a conversation id"
                )
            debug_path = emission.python.write_debug(
                debug_root, conversation_id, revision
            )

        schema_map: Mapping[str, str | None] = {
            "conversation": self.conversation_schema
        }
        stages = self.plan.stages()
        output_index = tuple(view.name for view in stages).index(str(self.plan.output))
        fallback_reason = None
        if selected is ExecutionMode.PYTHON:
            try:
                # AUTO is an availability policy as well as a size policy. A
                # savepoint makes every Python failure recoverable before the
                # emitted SQL fallback uses the same outer snapshot.
                with bind.begin_nested(), request_timing.span(
                    "deterministic_python"
                ):
                    python_result = execute_python(
                        emission.python,
                        bind,
                        schema_map=schema_map,
                        row_limit=python_row_limit,
                    )
                    view_rows = materialized_python_views(
                        python_result, self.plan
                    )
                rows = view_rows[output_index]
            except Exception as exc:
                if requested is not ExecutionMode.AUTO:
                    raise
                fallback_reason = f"{type(exc).__name__}: {exc}"
                # Python-by-default is only trustworthy if a silent retreat to SQL is
                # visible. Count it and name the exception type on the request's one
                # [timing] line; the message itself may quote user data, so it stays in
                # the response envelope and out of the log.
                request_timing.count("deterministic_python_fallback")
                request_timing.count(
                    f"deterministic_python_fallback_{type(exc).__name__}"
                )
                selected = ExecutionMode.SQL
                with request_timing.span("deterministic_sql"):
                    view_rows = execute_sql_views(emission.sql, bind)
                rows = view_rows[output_index]
        elif selected is ExecutionMode.SQL:
            with request_timing.span("deterministic_sql"):
                view_rows = execute_sql_views(emission.sql, bind)
            rows = view_rows[output_index]
        elif selected is ExecutionMode.VERIFY:
            with request_timing.span("deterministic_python"):
                python_result = execute_python(
                    emission.python,
                    bind,
                    schema_map=schema_map,
                    row_limit=python_row_limit,
                )
                python_views = materialized_python_views(python_result, self.plan)
            with request_timing.span("deterministic_sql"):
                sql_views = execute_sql_views(emission.sql, bind)
            for step, python_rows, sql_rows in zip(
                stages,
                python_views,
                sql_views,
                strict=True,
            ):
                try:
                    assert_equivalent(
                        python_rows,
                        sql_rows,
                        ordered=isinstance(step, SortedView)
                        or (isinstance(step, AntiJoinView) and bool(step.order)),
                    )
                except VerificationMismatch as exc:
                    exc.add_note(f"deterministic view: {step.name}")
                    raise
            view_rows = sql_views
            rows = sql_views[output_index]
        else:  # pragma: no cover - the enum and chooser make this unreachable
            raise AssertionError(f"unexpected execution mode: {selected}")
        reference = self.plan.reference_columns()
        filters = [view for view in stages if isinstance(view, FilteredView)]
        labels: Mapping[str, str] = {}
        if self.labels is not None:
            # Only the entities a user will see: the first 50 rows each stage records, the output, and the
            # values a filter's label names.
            entities = {
                value
                for index, (view, stage_rows) in enumerate(zip(stages, view_rows, strict=True))
                for row in (stage_rows if index == output_index else stage_rows[:50])
                for column in reference[view.name]
                if isinstance(value := row.get(column), str) and _ENTITY.fullmatch(value)
            } | {qid for view in filters for qid in _filter_entities(view.predicate, reference[view.source])}
            if entities:
                with request_timing.span("deterministic_labels"):
                    labels = dict(self.labels(entities))
        step_labels = {
            view.name: "where " + _condition(view.predicate, labels, reference[view.source]) for view in filters
        }
        return ExecutionResult(
            selected,
            rows,
            emission,
            view_rows,
            debug_path,
            fallback_reason,
            reference,
            labels,
            step_labels,
        )
