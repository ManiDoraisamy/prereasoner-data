"""Public orchestration API for deterministic dual emission and execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, Engine

from engine import request_timing
from engine.deterministic.emitter import (
    GeneratedPackage,
    GeneratedSQL,
    PythonEmitter,
    SQLEmitter,
)
from engine.deterministic.plan import AnalysisPlan, AntiJoinView, SortedView
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

DEFAULT_DEBUG_ROOT = Path(__file__).with_name("_gen") / "py"


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
            sql = statement.split(" AS ", 1)[1]
            suffix = str(step).removeprefix(
                str(self.emission.python.manifest["slug"]) + "_"
            )
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
                    "label": suffix.replace("_", " "),
                    "sql": sql,
                    "python": self.emission.python.manifest["view_sources"].get(step, ""),
                    "columns": self.emission.python.manifest["view_columns"][step],
                    "rows": [
                        [
                            row.get(column)
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
    ):
        if not conversation_schema:
            raise ValueError("conversation schema must be non-empty")
        self.plan = plan
        self.conversation_schema = conversation_schema
        self.dataset_version = dataset_version
        self.knowledgebase_release = knowledgebase_release

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
        output_index = tuple(view.name for view in self.plan.views).index(
            str(self.plan.output)
        )
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
                self.plan.views,
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
        return ExecutionResult(
            selected,
            rows,
            emission,
            view_rows,
            debug_path,
            fallback_reason,
        )
