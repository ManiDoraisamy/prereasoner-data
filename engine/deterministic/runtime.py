"""In-memory loading, execution policy, and SQL/Python parity checks."""

from __future__ import annotations

import itertools
import sys
import threading
import warnings
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, is_dataclass
from decimal import Decimal, localcontext
from enum import Enum
from pathlib import Path
from types import MappingProxyType, ModuleType

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import SAWarning
from sqlalchemy.orm import Session

from engine.deterministic.emitter.py import GeneratedPackage
from engine.deterministic.emitter.sql import GeneratedSQL
from engine.deterministic.plan import AnalysisPlan
from engine.numeric import DECIMAL_PRECISION, canonical_decimal

_MODULE_COUNTER = itertools.count()
_MODULE_LOCK = threading.Lock()


class ExecutionMode(str, Enum):
    AUTO = "auto"
    PYTHON = "python"
    SQL = "sql"
    VERIFY = "verify"


class VerificationMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedGeneratedPackage:
    package_name: str
    modules: Mapping[str, ModuleType]
    entrypoint: ModuleType

    def analysis_class(self):
        class_name = self.entrypoint.__generated_manifest__["entrypoint_class"]
        return getattr(self.entrypoint, class_name)


@contextmanager
def load_generated_package(
    package: GeneratedPackage,
) -> Iterator[LoadedGeneratedPackage]:
    """Compile and execute the exact emitted source as an ephemeral Python package."""
    with _MODULE_LOCK:
        serial = next(_MODULE_COUNTER)
    package_name = f"_prereasoner_generated_{package.source_sha256[:16]}_{serial}"
    root = ModuleType(package_name)
    root.__package__ = package_name
    root.__path__ = []
    root.__file__ = f"<{package_name}>"
    sys.modules[package_name] = root
    loaded: dict[str, ModuleType] = {}
    entrypoint_name = Path(package.entrypoint).stem
    names = ["base.py"]
    names.extend(
        name for name in package.files if name not in {"base.py", package.entrypoint}
    )
    names.append(package.entrypoint)
    try:
        for filename in names:
            module_name = Path(filename).stem
            qualified_name = f"{package_name}.{module_name}"
            module = ModuleType(qualified_name)
            module.__file__ = f"<{package_name}/{filename}>"
            module.__package__ = package_name
            module.__generated_manifest__ = dict(package.manifest)
            sys.modules[qualified_name] = module
            loaded[module_name] = module
            code = compile(
                package.files[filename], module.__file__, "exec", dont_inherit=True
            )
            exec(code, module.__dict__)  # noqa: S102 - source is produced by the closed emitter
        yield LoadedGeneratedPackage(
            package_name=package_name,
            modules=MappingProxyType(loaded),
            entrypoint=loaded[entrypoint_name],
        )
    finally:
        for module_name, module in reversed(tuple(loaded.items())):
            qualified_name = f"{package_name}.{module_name}"
            if sys.modules.get(qualified_name) is module:
                del sys.modules[qualified_name]
        if sys.modules.get(package_name) is root:
            del sys.modules[package_name]


def execute_python(
    package: GeneratedPackage,
    bind: Engine | Connection,
    *,
    schema_map: Mapping[str, str | None] | None = None,
    row_limit: int | None = None,
):
    """Execute the emitted wrapper against a SQLAlchemy bind and return AnalysisResult."""
    if row_limit is not None and (
        type(row_limit) is not int or row_limit < 0
    ):
        raise ValueError("Python execution row limit must be non-negative")
    translated = bind.execution_options(schema_translate_map=dict(schema_map or {}))
    with (
        warnings.catch_warnings(),
        localcontext() as context,
        load_generated_package(package) as loaded,
        # The service owns the transaction/savepoint boundary. Joining it in
        # rollback-only mode avoids a redundant inner SAVEPOINT around this
        # read-only ORM session while preserving outer rollback on failure.
        Session(bind=translated, join_transaction_mode="rollback_only") as session,
    ):
        warnings.filterwarnings(
            "error",
            message="Multiple rows returned with uselist=False.*",
            category=SAWarning,
        )
        context.prec = DECIMAL_PRECISION
        wrapper = loaded.analysis_class()(session, row_limit=row_limit)
        method = package.manifest.get("entrypoint_method", package.manifest["slug"])
        return getattr(wrapper, str(method))()


def execute_sql(
    program: GeneratedSQL, bind: Engine | Connection
) -> tuple[dict[str, object], ...]:
    """Execute the emitted SQL view stack and return its final materialized view."""
    views = tuple(str(name) for name in program.manifest["views"])
    output = str(program.manifest.get("output") or views[-1])
    return execute_sql_views(program, bind)[views.index(output)]


def execute_sql_views(
    program: GeneratedSQL,
    bind: Engine | Connection,
    *,
    row_limit: int | None = None,
) -> tuple[tuple[dict[str, object], ...], ...]:
    """Execute SQL and retain each named stage for trace display or parity checks."""
    if row_limit is not None and (
        type(row_limit) is not int or row_limit < 0
    ):
        raise ValueError("SQL view row limit must be non-negative")
    owns_connection = isinstance(bind, Engine)
    connection = bind.connect() if owns_connection else bind
    view_names = tuple(str(name) for name in program.manifest["views"])
    transaction = (
        connection.begin_nested() if connection.in_transaction() else connection.begin()
    )
    try:
        for statement in program.statements:
            connection.execute(text(statement))
        materialized = []
        limit = "" if row_limit is None else f" LIMIT {row_limit}"
        for view_name in view_names:
            result = connection.execute(
                text(f"SELECT * FROM {_quote_identifier(view_name)}{limit}")
            )
            materialized.append(tuple(dict(row._mapping) for row in result))
        for view_name in reversed(view_names):
            connection.execute(
                text(f"DROP VIEW IF EXISTS {_quote_identifier(view_name)}")
            )
        transaction.commit()
        return tuple(materialized)
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if owns_connection:
            connection.close()


def materialized_python_views(
    result, plan: AnalysisPlan
) -> tuple[tuple[dict[str, object], ...], ...]:
    """Flatten ORM objects into the same named columns exposed by SQL stages."""
    materialized = []
    shapes = plan.view_columns()
    table_attributes = {table.attribute for table in plan.tables}
    for view in result.views:
        expected = shapes[view.name]
        rows = []
        for row in view.rows:
            values: dict[str, object] = {}
            for table in plan.tables:
                if not hasattr(row, table.attribute):
                    continue
                instance = getattr(row, table.attribute)
                for column in table.columns:
                    key = f"{table.name}__{column.name}"
                    scalar_attribute = table.scalar_attribute(column.name)
                    values[key] = (
                        None
                        if instance is None
                        else getattr(instance, scalar_attribute)
                    )
            if is_dataclass(row):
                for name in row.__dataclass_fields__:
                    if name not in table_attributes:
                        values[name] = getattr(row, name)
            elif isinstance(row, Mapping):
                values.update(row)
            rows.append({column: values.get(column) for column in expected})
        materialized.append(tuple(rows))
    return tuple(materialized)


def assert_equivalent(
    python_rows: tuple[dict[str, object], ...],
    sql_rows: tuple[dict[str, object], ...],
    *,
    ordered: bool = False,
) -> None:
    left = _canonical_rows(python_rows, ordered=ordered)
    right = _canonical_rows(sql_rows, ordered=ordered)
    if left != right:
        raise VerificationMismatch(
            f"Python and SQL results differ: python={left!r}, sql={right!r}"
        )


def choose_execution_mode(
    requested: ExecutionMode | str,
    *,
    estimated_rows: int,
    python_row_limit: int = 10_000,
) -> ExecutionMode:
    mode = ExecutionMode(requested)
    if (
        type(estimated_rows) is not int
        or type(python_row_limit) is not int
        or estimated_rows < 0
        or python_row_limit < 0
    ):
        raise ValueError("row estimates and limits must be non-negative integers")
    if mode is not ExecutionMode.AUTO:
        return mode
    return (
        ExecutionMode.PYTHON
        if estimated_rows <= python_row_limit
        else ExecutionMode.SQL
    )


def debug_generation_enabled(app_env: str, *, explicit: bool = False) -> bool:
    """Production remains memory-only unless persistence was explicitly requested."""
    return app_env.strip().lower() == "development" or explicit


def _canonical_rows(rows: tuple[dict[str, object], ...], *, ordered=False):
    def scalar(value: object):
        if isinstance(value, Decimal):
            return ("number", canonical_decimal(value))
        if isinstance(value, int) and not isinstance(value, bool):
            return ("number", canonical_decimal(Decimal(value)))
        if isinstance(value, float):
            return ("number", canonical_decimal(Decimal(str(value))))
        if isinstance(value, dict):
            return tuple(sorted((key, scalar(item)) for key, item in value.items()))
        if isinstance(value, (list, tuple)):
            return tuple(scalar(item) for item in value)
        return value

    normalized = tuple(
        tuple(sorted((key, scalar(value)) for key, value in row.items()))
        for row in rows
    )
    return normalized if ordered else tuple(sorted(normalized, key=repr))


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'
