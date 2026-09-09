"""Load whole tables through the ORM and wire the relational graph in Python.

**The non-circularity invariant.** SQLAlchemy may issue only a bare full-table projection — no
WHERE, no JOIN, no GROUP BY, no ORDER BY, no LIMIT, one FROM. Every join, filter, grouping and
ordering happens above this layer, in `evaluate.py`. Without that rule the "Python emitter" would
be asking the database to do the work and comparing SQL against SQL, and the oracle would confirm
nothing. `audited_session` records every statement so a test can assert it.

Values come back typed the way the evaluator expects: INTEGER as `int`, REAL as `Decimal` (the
serving path stores it as canonical decimal text), TEXT as `str`.
"""
from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Iterator, Sequence

from engine.numeric import parse_decimal

from deterministic.emitter.py.classes import ForeignKeySpec, TableSpec

_FORBIDDEN = re.compile(r"(?is)\b(where|join|group\s+by|order\s+by|limit|having|union|"
                        r"intersect|except|distinct)\b")


class CircularOracleError(AssertionError):
    """The ORM asked the database to do work the Python emitter is supposed to do itself."""


def assert_bare_select(statement: str) -> None:
    """Reject anything but `SELECT <columns> FROM <one table>`."""
    text = " ".join(str(statement).split())
    if not re.match(r"(?is)^select\b", text):
        raise CircularOracleError(f"hydration issued a non-SELECT statement: {text}")
    if _FORBIDDEN.search(text):
        raise CircularOracleError(
            f"hydration pushed relational work into SQL, so the oracle would be circular: {text}"
        )
    if len(re.findall(r"(?is)\bfrom\b", text)) != 1:
        raise CircularOracleError(f"hydration read more than one relation at once: {text}")


class _StatementLog(list):
    """Every statement the ORM emitted, checked as it goes."""

    def record(self, statement: str) -> None:
        self.append(" ".join(str(statement).split()))
        assert_bare_select(statement)


def audited_session(engine) -> tuple[Any, _StatementLog]:
    """A session whose every emitted statement is recorded and checked."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    log = _StatementLog()
    session = Session(engine, future=True)

    @event.listens_for(session.connection(), "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        log.record(statement)

    return session, log


def _typed(value: Any, affinity: str) -> Any:
    if value is None:
        return None
    if affinity == "INTEGER":
        return int(value)
    if affinity == "REAL":
        return parse_decimal(value, enforce_input_bounds=False)
    return str(value)


def hydrate(engine, specs: Sequence[TableSpec], classes: dict[str, Any]
            ) -> tuple[dict[str, list[dict]], _StatementLog]:
    """Materialize every table into evaluator rows keyed by ``(table, column)``."""
    session, log = audited_session(engine)
    tables: dict[str, list[dict]] = {}
    try:
        for spec in specs:
            mapped = classes[spec.name]
            rows = []
            for instance in session.query(mapped).all():      # one bare SELECT per table
                rows.append({
                    (spec.name, column.name): _typed(getattr(instance, column.name), column.affinity)
                    for column in spec.columns
                })
            tables[spec.name] = rows
    finally:
        session.close()
    return tables, log


def link_graph(tables: dict[str, list[dict]], foreign_keys: Sequence[ForeignKeySpec]
               ) -> dict[str, dict[Any, list[dict]]]:
    """Index each foreign-key parent so a child row reaches its parent without another query.

    This is the "graph" half of the object model: `orders` rows reach their `customers` row, which
    reaches `city`, which reaches `country`. It is built here, in Python, from rows already in
    memory — which is exactly what keeps the ORM out of the join.
    """
    index: dict[str, dict[Any, list[dict]]] = {}
    for key in foreign_keys:
        parents = index.setdefault(f"{key.from_table}.{key.from_column}", {})
        for row in tables.get(key.to_table, ()):
            parents.setdefault(row.get((key.to_table, key.to_column)), []).append(row)
    return index
