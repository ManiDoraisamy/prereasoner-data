"""The request's SQLite tables for a catalog: one physical table per catalog table, loaded on first use, plus the melted view a peer rank reads.

A table t3 holds, per line: _line (its order), _role, _label (fold) and _label_len, _section (fold), _section_text, _section_path (fold). Per column at position p: c{p}_t the cell as shown, c{p}_f its fold, c{p}_n its number as the sheet's binary float, which SQLite computes natively for the candidates and both deterministic programs read as its
shortest decimal for a chosen one (ColumnSpec.binary_float), c{p}_d its ISO day. A blank is NULL in all four. t3_peers holds one row per line and column: _line, _role, _label, _section, _name, _f, _n. "_one" is the one row
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


def peers_table(table_id: str) -> str:
    return f"{table_id}_peers"


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


class RequestTables:
    """One private SQLite database over a catalog. Tables load on first use, since a question reaches few of a workbook's tables."""

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
        table = self.catalog.table(table_id)
        columns = [f'"{LINE}" INTEGER PRIMARY KEY', f'"{ROLE}" TEXT', f'"{LABEL}" TEXT', f'"{LABEL_LENGTH}" INTEGER', f'"{SECTION_FOLD}" TEXT', f'"{SECTION_TEXT}" TEXT', f'"{SECTION_PATH}" TEXT']
        for position in range(len(table.columns)):
            columns.extend((f'"{text_column(position)}" TEXT', f'"{fold_column(position)}" TEXT', f'"{number_column(position)}" REAL', f'"{day_column(position)}" TEXT'))
        self.connection.execute(f'CREATE TABLE "{table_id}" ({", ".join(columns)})')
        width = len(columns)
        rows = []
        for i, line in enumerate(table.lines):
            row = [i, line.role, table.label_folds[i], len(table.label_folds[i]), table.section_folds[i], table.section_texts[i], table.section_paths[i]]
            for position in range(len(table.columns)):
                row.extend((table.texts[position][i], table.folds[position][i], table.numbers[position][i], table.days[position][i]))
            rows.append(row)
        self.connection.executemany(f'INSERT INTO "{table_id}" VALUES ({",".join("?" * width)})', rows)
        self.connection.commit()
        self._loaded.add(table_id)

    def ensure_peers(self, table_id: str) -> None:
        if table_id in self._peers_loaded:
            return
        self.ensure(table_id)
        table: CatalogTable = self.catalog.table(table_id)
        name = peers_table(table_id)
        self.connection.execute(f'CREATE TABLE "{name}" ("{LINE}" INTEGER, "{ROLE}" TEXT, "{LABEL}" TEXT, "{SECTION_FOLD}" TEXT, "{SECTION_PATH}" TEXT, "{PEER_NAME}" TEXT, "{PEER_FOLD}" TEXT, "{PEER_NUMBER}" REAL, "position" INTEGER)')
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
