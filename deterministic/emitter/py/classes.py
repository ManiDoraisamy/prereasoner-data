"""Generate one declarative class per table, plus the in-memory database they map to.

The uploaded sheets (`orders`, `customers`) and the reference tables a question grounded against
(`city`, `country`, `currency`) become ordinary Python classes with typed attributes. A wrapper
class stands for the joined relation the analysis is about — `orders_customers` — and carries one
method per workbook slug.

Reference classes are built from the request-local rows the compose path already materialized, not
from a scan of `knowledgebase.city`: the world tables are Wikidata-scale and only the grounded
slice is ever in memory.

Column affinities mirror `engine/tables.py:TableQuery.execute` exactly — `REAL` is stored as
canonical decimal text so SQLite never rounds on insert — because the whole point is for the two
emitters to read the same bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
import keyword
import re
from typing import Any, Sequence

from engine.numeric import sqlite_numeric

@dataclass(frozen=True)
class ColumnSpec:
    name: str
    affinity: str          # INTEGER | REAL | TEXT, as the planner schema declares it


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]

    @property
    def class_name(self) -> str:
        return class_name_for(self.name)


@dataclass(frozen=True)
class ForeignKeySpec:
    from_table: str
    from_column: str
    to_table: str
    to_column: str


def class_name_for(table: str) -> str:
    """`orders_customers` -> `OrdersCustomers`; always a valid, non-keyword identifier."""
    parts = [part for part in re.split(r"[^0-9A-Za-z]+", str(table)) if part]
    name = "".join(part[:1].upper() + part[1:] for part in parts) or "Table"
    if name[0].isdigit() or keyword.iskeyword(name):
        name = "T" + name
    return name


def specs_from_schema(schema: Sequence[dict]) -> tuple[TableSpec, ...]:
    """Build table specs from the planner schema (`TableQuery.schema`'s column records)."""
    grouped: dict[str, list[ColumnSpec]] = {}
    for column in schema:
        grouped.setdefault(column["table"], []).append(
            ColumnSpec(column["name"], column.get("affinity", "TEXT"))
        )
    return tuple(TableSpec(table, tuple(columns)) for table, columns in grouped.items())


def build_database(specs: Sequence[TableSpec], tablemap: dict[str, dict]):
    """Create an in-memory SQLite database and the declarative classes mapped onto it.

    Returns `(engine, Base, {table_name: mapped_class})`. Storage mirrors the serving path: a
    synthetic `_row` primary key keeps SQLAlchemy happy without inventing a key from user columns,
    and never appears in any emitted class or evaluated row.
    """
    from sqlalchemy import Column, Integer, MetaData, Numeric, String, Table, create_engine, insert
    from sqlalchemy.orm import declarative_base

    types = {"INTEGER": Integer, "REAL": String, "TEXT": String, "NUMERIC": Numeric}
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base = declarative_base(metadata=MetaData())
    classes: dict[str, Any] = {}

    for spec in specs:
        columns = [Column("_row", Integer, primary_key=True, autoincrement=True)]
        columns += [Column(column.name, types.get(column.affinity, String))
                    for column in spec.columns]
        table = Table(spec.name, Base.metadata, *columns)
        classes[spec.name] = type(spec.class_name, (Base,), {"__table__": table})

    Base.metadata.create_all(engine)

    with engine.begin() as connection:
        for spec in specs:
            source = tablemap.get(spec.name)
            if not source:
                continue
            order = list(source["columns"])
            payload = []
            for row in source["rows"]:
                values = dict(zip(order, row))
                payload.append({
                    column.name: _storage_value(values.get(column.name), column.affinity)
                    for column in spec.columns
                })
            if payload:
                connection.execute(insert(Base.metadata.tables[spec.name]), payload)
    return engine, Base, classes


def _storage_value(value: Any, affinity: str) -> Any:
    if value is None:
        return None
    if affinity in ("INTEGER", "REAL"):
        return sqlite_numeric(value, affinity)
    return str(value)
