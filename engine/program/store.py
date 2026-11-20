"""The request's SQLite tables for a catalog: one physical table per catalog table, loaded on first use, plus the melted view a peer rank reads.

They live on the request's one database (engine/prepared.py RequestDatabase.program_tables) beside the request's own tables, and every name
starts with an underscore, which no request table's canonical name does (engine/request_validation.canonical_table_name strips it): catalog
table t3 is the SQL table _t3. It holds, per line: _line (its order), _role, _label (fold) and _label_len, _section (fold), _section_text, _section_path (fold). Per column at position p: c{p}_t the cell as shown, c{p}_f its fold, c{p}_n its number as the sheet's binary float, which SQLite computes natively for the candidates and both deterministic programs read as its
shortest decimal for a chosen one (ColumnSpec.binary_float), c{p}_d its ISO day. A blank is NULL in all four. A column of prose (CatalogTable.prose_columns) also holds c{p}_w, its cell's number of words. _t3_peers holds one row per line and column: _line, _role, _label, _section, _name, _f, _n. "_one" is the one row
a calculation selects from. Identifiers are ours, so nothing a sheet writes reaches SQL except as a bound value or a typed literal.
"""
from __future__ import annotations

import sqlite3

from engine.prepared import connect
from engine.program.catalog import Catalog, CatalogTable


def text_column(position: int) -> str:
    return f"c{position}_t"


def fold_column(position: int) -> str:
    return f"c{position}_f"


def number_column(position: int) -> str:
    return f"c{position}_n"


def day_column(position: int) -> str:
    return f"c{position}_d"


def sql_table(table_id: str) -> str:
    """The SQL table of a catalog table."""
    return f"_{table_id}"


def table_id_of(sql_name: str) -> str:
    """The catalog table a SQL table holds (sql_table's inverse)."""
    return sql_name[1:]


def words_column(position: int) -> str:
    """A prose column's number of words per line (CatalogTable.prose_columns): stored only for those columns."""
    return f"c{position}_w"


def peers_table(table_id: str) -> str:
    return f"_{table_id}_peers"


ONE_ROW = "_one"
LINE = "_line"
ROLE = "_role"
LABEL = "_label"
LABEL_LENGTH = "_label_len"
SECTION_FOLD = "_section"
SECTION_TEXT = "_section_text"
SECTION_PATH = "_section_path"
PEER_NAME = "_name"
PEER_FOLD = "_f"
PEER_NUMBER = "_n"


INTEGER, TEXT, REAL, DATE = "integer", "text", "real", "date"     # what a store column holds; a day is ISO text


def table_layout(table: CatalogTable) -> list[tuple[str, str]]:
    """(name, kind) of each column a catalog table is stored with, in its order."""
    columns = [(LINE, INTEGER), (ROLE, TEXT), (LABEL, TEXT), (LABEL_LENGTH, INTEGER), (SECTION_FOLD, TEXT), (SECTION_TEXT, TEXT), (SECTION_PATH, TEXT)]
    for position in range(len(table.columns)):
        columns.extend(((text_column(position), TEXT), (fold_column(position), TEXT), (number_column(position), REAL), (day_column(position), DATE)))
    columns.extend((words_column(position), REAL) for position in table.prose_columns)
    return columns


def table_columns(catalog: Catalog, sql_name: str) -> list[tuple[str, str]]:
    """(name, kind) of each column of a store table, by its SQL name (sql_table or peers_table), in its order: the
    columns RequestTables creates."""
    if sql_name.endswith("_peers"):
        return [(LINE, INTEGER), (ROLE, TEXT), (LABEL, TEXT), (SECTION_FOLD, TEXT), (SECTION_PATH, TEXT), (PEER_NAME, TEXT), (PEER_FOLD, TEXT), (PEER_NUMBER, REAL), ("position", INTEGER)]
    return table_layout(catalog.table(table_id_of(sql_name)))


_STORAGE = {INTEGER: "INTEGER", TEXT: "TEXT", REAL: "REAL", DATE: "TEXT"}


def column_lineage(catalog: Catalog) -> dict[tuple[str, str], tuple[str, str]]:
    """Each store column of a column that holds another column's values (Catalog.identities), as (SQL table, SQL
    column), mapped to the same store column of the column it holds them from: a plan reading either reads the same
    values (engine/decomposition.py binds merge keys by it)."""
    out = {}
    for (table_id, position), (root_id, root_position) in catalog.identities.items():
        for name_of in (text_column, fold_column, number_column, day_column):
            out[(sql_table(table_id), name_of(position))] = (sql_table(root_id), name_of(root_position))
    return out


def write_table(connection: sqlite3.Connection, name: str, table: CatalogTable, temporary: bool = False) -> None:
    """Create the SQL table ``name`` holding ``table``'s lines in its stored layout (table_layout); ``temporary``: a TEMP
    table, which the connection alone sees (engine/program/connected.py reads a table's own columns from one)."""
    columns = table_layout(table)
    definition = ", ".join(f'"{column}" {_STORAGE[kind]}' + (" PRIMARY KEY" if column == LINE else "") for column, kind in columns)
    connection.execute(f'CREATE {"TEMP " if temporary else ""}TABLE "{name}" ({definition})')
    words = [table.words(position) for position in table.prose_columns]
    rows = []
    for i, line in enumerate(table.lines):
        row = [i, line.role, table.label_folds[i], len(table.label_folds[i]), table.section_folds[i], table.section_texts[i], table.section_paths[i]]
        for position in range(len(table.columns)):
            row.extend((table.texts[position][i], table.folds[position][i], table.numbers[position][i], table.days[position][i]))
        row.extend(held[i] for held in words)
        rows.append(row)
    connection.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" * len(columns))})', rows)


class RequestTables:
    """A catalog's tables on one SQLite connection: the request's database (engine/prepared.py), else a private one. Tables load on first use, since a question
    reaches few of a workbook's tables."""

    def __init__(self, catalog: Catalog, connection: sqlite3.Connection | None = None):
        self.catalog = catalog
        self.connection = connection if connection is not None else connect()
        self.connection.execute(f'CREATE TABLE "{ONE_ROW}" ("one" INTEGER)')
        self.connection.execute(f'INSERT INTO "{ONE_ROW}" VALUES (1)')
        self._loaded: set[str] = set()
        self._peers_loaded: set[str] = set()

    def ensure(self, table_id: str) -> None:
        if table_id in self._loaded:
            return
        write_table(self.connection, sql_table(table_id), self.catalog.table(table_id))
        self.connection.commit()
        self._loaded.add(table_id)

    def ensure_peers(self, table_id: str) -> None:
        if table_id in self._peers_loaded:
            return
        self.ensure(table_id)
        table: CatalogTable = self.catalog.table(table_id)
        name = peers_table(table_id)
        definition = ", ".join(f'"{column}" {_STORAGE[kind]}' for column, kind in table_columns(self.catalog, name))
        self.connection.execute(f'CREATE TABLE "{name}" ({definition})')
        rows = []
        for i, line in enumerate(table.lines):
            for position, column in enumerate(table.columns):
                folded = table.folds[position][i]
                if folded is None:
                    continue
                rows.append((i, line.role, table.label_folds[i], table.section_folds[i], table.section_paths[i], column.name, folded, table.numbers[position][i], position))
        self.connection.executemany(f'INSERT INTO "{name}" VALUES (?,?,?,?,?,?,?,?,?)', rows)
        self.connection.commit()
        self._peers_loaded.add(table_id)

    def execute(self, sql: str) -> list[tuple]:
        return self.connection.execute(sql).fetchall()
