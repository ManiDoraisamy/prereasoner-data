"""Render the hydrated model as readable Python modules — a debug artifact, never an execution.

What this emits, given the analysis `total_amount` over `orders` and `customers`:

    deterministic/_gen/py/<conversationId>/orders.py
    deterministic/_gen/py/<conversationId>/customers.py
    deterministic/_gen/py/<conversationId>/orders_customers.py     <- the wrapper

**What it deliberately does not emit: the step bodies as generated for-loops.** A code generator
that lowered the AST into loops would be a second implementation of the semantics `evaluate.py`
already owns, and the two would drift — the one thing this repository's rules exist to prevent.
Since the oracle interprets the tree rather than executing generated source, the loops would also
be a description of the computation rather than the computation itself, which is exactly the kind
of "prettified, reconstructed" artifact `docs/SHEETS_AS_REASONING.md` rule 1 refuses for SQL.

So each wrapper method names its derivation steps and delegates to the one evaluator. The class
shapes and the graph navigation are emitted in full, because those are data, not semantics.

Writing anything to disk requires `PR_EMIT_PY_DEBUG=1`. The emitted source embeds uploaded column
names and question literals, so it must never be enabled where real tenant data lives — the same
reasoning that keeps prompts and cells out of `engine/request_timing.py`.
"""
from __future__ import annotations

import os
import pathlib
import re
from typing import Iterable, Sequence

from engine.sql_ast import Query, SelectQuery, SetQuery, SubquerySource

from deterministic.emitter.py.classes import ForeignKeySpec, TableSpec, class_name_for

GEN_ROOT = pathlib.Path("deterministic/_gen/py")
DEBUG_FLAG = "PR_EMIT_PY_DEBUG"

_PY_TYPE = {"INTEGER": "int | None", "REAL": "Decimal | None", "TEXT": "str | None"}


def dump_enabled() -> bool:
    return os.environ.get(DEBUG_FLAG, "").strip().lower() in {"1", "true", "yes"}


def _identifier(name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", str(name))
    return f"_{cleaned}" if not cleaned or cleaned[0].isdigit() else cleaned


def render_table_module(spec: TableSpec, foreign_keys: Sequence[ForeignKeySpec] = ()) -> str:
    """One class per uploaded sheet or grounded reference table."""
    lines = [
        f'"""Generated from the `{spec.name}` sheet. Do not edit — regenerate instead."""',
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "from decimal import Decimal",
        "",
        "",
        "@dataclass(frozen=True)",
        f"class {spec.class_name}:",
    ]
    for column in spec.columns:
        lines.append(f"    {_identifier(column.name)}: {_PY_TYPE.get(column.affinity, 'str | None')}")
    edges = [key for key in foreign_keys if key.from_table == spec.name]
    if edges:
        lines.append("")
        lines.append("    # Foreign keys, wired in Python from rows already in memory.")
        for key in edges:
            lines.append(
                f"    # {_identifier(key.from_column)} -> "
                f"{class_name_for(key.to_table)}.{_identifier(key.to_column)}"
            )
    return "\n".join(lines) + "\n"


def describe_steps(query: Query) -> list[str]:
    """Name a query's derivation steps in the `SHEETS_AS_REASONING` vocabulary.

    A description of the tree, not a re-implementation of it: nothing here computes a value.
    """
    if isinstance(query, SetQuery):
        return [f"{query.operator.lower()} of two branches"]
    steps: list[str] = []
    source = query.from_table
    steps.append(f"read {source.alias if isinstance(source, SubquerySource) else source}")
    for join in query.joins:
        kind = "left join" if join.kind == "LEFT" else "join"
        steps.append(f"{kind} {join.alias or join.table}")
    if query.where is not None:
        steps.append("filter")
    if query.group_by:
        steps.append("group_agg by " + ", ".join(column.name for column in query.group_by))
    elif any(type(item.expression).__name__ == "Aggregate" for item in query.select):
        steps.append("group_agg")
    if query.having is not None:
        steps.append("having")
    if query.order_by:
        steps.append("sort by " + ", ".join(term.direction.lower() for term in query.order_by))
    if query.limit is not None:
        steps.append(f"topn {query.limit}")
    return steps


def render_wrapper_module(slug: str, specs: Sequence[TableSpec], methods: dict[str, Query]) -> str:
    """The analysis class: one method per workbook slug, over the joined relation."""
    class_name = class_name_for(slug)
    lines = [
        f'"""Generated wrapper for the `{slug}` analysis. Do not edit — regenerate instead."""',
        "from __future__ import annotations",
        "",
        "from deterministic.emitter.py.evaluate import evaluate_query",
        "",
        "",
        f"class {class_name}:",
        f'    """{", ".join(spec.name for spec in specs)} as one relation."""',
        "",
        "    def __init__(self, tables, asts):",
        "        self.tables = tables       # hydrate() output: whole sheets, already in memory",
        "        self.asts = asts           # the validated AST behind each named method",
        "",
    ]
    for name, query in methods.items():
        method = _identifier(name)
        lines.append(f"    def {method}(self):")
        lines.append('        """Derivation:')
        for index, step in enumerate(describe_steps(query), start=1):
            lines.append(f"          {index}. {step}")
        lines.append('        """')
        lines.append(f"        rows, _notes = evaluate_query(self.asts[{name!r}], self.tables)")
        lines.append("        return rows")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_modules(slug: str, specs: Sequence[TableSpec], methods: dict[str, Query],
                   foreign_keys: Sequence[ForeignKeySpec] = ()) -> dict[str, str]:
    """Every module for one analysis, keyed by filename."""
    modules = {f"{spec.name}.py": render_table_module(spec, foreign_keys) for spec in specs}
    modules[f"{slug}.py"] = render_wrapper_module(slug, specs, methods)
    return modules


def dump(conversation_id: str, modules: dict[str, str], root: pathlib.Path | None = None
         ) -> pathlib.Path | None:
    """Write the modules for debugging. A no-op unless PR_EMIT_PY_DEBUG is set."""
    if not dump_enabled():
        return None
    directory = (root or GEN_ROOT) / _identifier(conversation_id)
    directory.mkdir(parents=True, exist_ok=True)
    for filename, source in modules.items():
        (directory / filename).write_text(source, encoding="utf-8")
    return directory
