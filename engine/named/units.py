"""A catalog table as what the encoder reads: units (a column's name, a sampled cell, the table's own context), the edges between them, and the targets a question's words can link to.

Order invariance holds by construction: columns are put in an order read from their own words and cells, rows are sampled by a hash of their own cells, and no unit carries a position. So a sheet with its columns or rows shuffled gives the encoder the same input, bit for bit, and each column's output goes back to wherever the column now stands.

A table graph: one table unit, each column's name, and the cells of the sampled rows. same_col links a name to its cells and cells of one column, same_row the cells of a line, same_cell a cell's value to its format where the format says something. A wide table is read in windows of columns, in that same order.
A value graph, for each stored value, label and band: the value, its column's name, and a few cells from a line holding it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
import torch

from engine.fk_edges import question_edges, table_edges
from engine.program.catalog import CatalogTable
from engine.program.index import index_of
from engine.program.language import Col
from engine.program.names import SLOT_FILTER, SLOT_KEY, SLOT_LINE, SLOT_MEASURE, SLOT_SECTION, SLOT_SHOWN, SLOT_TABLE, SLOT_VALUE, coordinate
from engine.sheet.names import COLUMN_ROLES, F_DATE, F_GENERAL, V_DATE, V_NUMBER, V_PERIOD, VALUE_KINDS

UNIT_TABLE = "table"
UNIT_NAME = "name"
UNIT_VALUE = "value"
UNIT_WORD = "word"
UNIT_KINDS = (UNIT_TABLE, UNIT_NAME, UNIT_VALUE, UNIT_WORD)
FEATURES = len(UNIT_KINDS) + len(COLUMN_ROLES) + 1 + len(VALUE_KINDS) + 1
_ROLE_AT = {role: at for at, role in enumerate(COLUMN_ROLES)}
_KIND_AT = {kind: at for at, kind in enumerate(VALUE_KINDS)}

T_COLUMN = "column"
T_VALUE = "value"
T_LINE = "line"
T_BAND = "band"
T_TABLE = "table"
TARGET_KINDS = (T_COLUMN, T_VALUE, T_LINE, T_BAND, T_TABLE)
_TEXT_LIMIT = 80


@dataclass(frozen=True, slots=True)
class UnitsConfig:
    """How much of a table the encoder reads."""
    sample_rows: int = 8
    window_columns: int = 48
    row_mates: int = 3
    value_cap: int = 2000


@dataclass(slots=True)
class Graph:
    """Units as parallel lists, their edge matrix, and where each column's name unit stands."""
    texts: list[str]
    features: np.ndarray
    edges: np.ndarray
    columns: dict[int, int]
    table: int


@dataclass(frozen=True, slots=True)
class Target:
    """What a question's words can link to. text is what rules and the program call it: a value's or label's fold, a column's name."""
    table: str
    kind: str
    position: int
    text: str

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (self.table, self.kind, self.position, self.text)

    def coordinates(self) -> tuple[str, ...]:
        """The program coordinates this target's link fills."""
        if self.kind == T_COLUMN:
            name = Col(self.table, self.position).name
            return (coordinate(SLOT_MEASURE, name), coordinate(SLOT_KEY, name), coordinate(SLOT_SHOWN, name), coordinate(SLOT_FILTER, name))
        if self.kind == T_VALUE:
            return (coordinate(SLOT_VALUE, Col(self.table, self.position).name, self.text),)
        if self.kind == T_LINE:
            return (coordinate(SLOT_LINE, self.table, self.text),)
        if self.kind == T_BAND:
            return (coordinate(SLOT_SECTION, self.table, self.text),)
        return (coordinate(SLOT_TABLE, self.table),)


def _digest(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def _clip(text: object) -> str:
    return " ".join(str(text).split())[:_TEXT_LIMIT]


def _features(kind: str, role: str | None = None, value_kind: str | None = None) -> list[float]:
    row = [0.0] * FEATURES
    row[UNIT_KINDS.index(kind)] = 1.0
    at = len(UNIT_KINDS)
    row[at + _ROLE_AT.get(role, len(COLUMN_ROLES))] = 1.0
    at += len(COLUMN_ROLES) + 1
    row[at + _KIND_AT.get(value_kind, len(VALUE_KINDS))] = 1.0
    return row


def cell_text(cell) -> str:
    """What a cell's unit reads: a number by its shape, since its digits name nothing; a date by its month; a text as written. So the encoder learns kinds, not one sheet's figures."""
    if cell.kind == V_NUMBER and isinstance(cell.value, (int, float)):
        value = float(cell.value)
        digits = len(str(int(abs(value)))) if abs(value) < 1e18 else 19
        return f"number {digits} digits" + (" negative" if value < 0 else "") + ("" if value == int(value) else " fraction")
    if cell.kind in (V_DATE, V_PERIOD) and isinstance(cell.value, str):
        return f"date {cell.value[:7]}"
    return _clip(cell.raw if cell.raw not in (None, "") else cell.value)


def table_context(table: CatalogTable) -> str:
    """What surrounds a table: its tab, its name, its titles."""
    titles = " ".join(text for _, text in table.table.context.titles)
    return _clip(f"{table.tab} {table.table.name if table.table.name != table.tab else ''} {titles}")


def line_digests(table: CatalogTable) -> list[str]:
    """Each line's digest of its own cells, whatever their order and whatever their columns are called."""
    held = table.__dict__.get("_line_digests")
    if held is None:
        columns = [folds for folds in table.folds]
        held = [_digest("|".join(sorted(folds[i] for folds in columns if folds[i] is not None))) for i in range(len(table.lines))]
        table.__dict__["_line_digests"] = held
    return held


def sampled_lines(table: CatalogTable, count: int) -> list[int]:
    """The data lines with the lowest digests, one per distinct line. A stable pick that needs no order."""
    digests = line_digests(table)
    seen: dict[str, int] = {}
    for i, (digest, data) in enumerate(zip(digests, table.is_data)):
        if data and digest not in seen:
            seen[digest] = i
    return [seen[digest] for digest in sorted(seen)[:count]]


def table_graphs(table: CatalogTable, config: UnitsConfig) -> list[Graph]:
    """One table's graphs. Every column stands in exactly one."""
    order = table.column_order
    lines = sampled_lines(table, config.sample_rows)
    windows = [order[start:start + config.window_columns] for start in range(0, len(order), config.window_columns)] or [[]]
    graphs = []
    context = table_context(table)
    for window in windows:
        texts, features, kinds, cols, rows = [context], [_features(UNIT_TABLE)], [UNIT_TABLE], [-1], [-1]
        columns: dict[int, int] = {}
        for col_at, position in enumerate(window):
            column = table.columns[position]
            columns[position] = len(texts)
            texts.append(_clip(column.name))
            features.append(_features(UNIT_NAME, column.role, column.kind))
            kinds.append(UNIT_NAME)
            cols.append(col_at)
            rows.append(-1)
            for row_at, i in enumerate(lines):
                cell = table.lines[i].cells.get(position)
                if cell is None:
                    continue
                texts.append(cell_text(cell))
                features.append(_features(UNIT_VALUE, column.role, cell.kind))
                kinds.append(UNIT_VALUE)
                cols.append(col_at)
                rows.append(row_at)
                if cell.format_kind != F_GENERAL:
                    texts.append(f"format {cell.format_kind}")
                    features.append(_features(UNIT_VALUE, column.role, cell.kind))
                    kinds.append(UNIT_VALUE)
                    cols.append(col_at)
                    rows.append(row_at)
        graphs.append(Graph(texts, np.asarray(features, dtype=np.float32), table_edges(kinds, cols, rows), columns, 0))
    return graphs


def targets_of(table: CatalogTable, config: UnitsConfig) -> list[Target]:
    """Everything a word can link to on one table, in an order read from the words themselves: the table, its columns by position, the values of its text columns, its printed lines' labels, its bands. Over value_cap distinct values, a column keeps those with the lowest digests; rules still reach the rest."""
    out = [Target(table.id, T_TABLE, -1, "")]
    index = index_of(table)
    for column in table.columns:
        out.append(Target(table.id, T_COLUMN, column.position, column.name))
    for column in table.columns:
        position = column.position
        if column.kind == V_NUMBER or table.is_date_column(position) or column.format_kind == F_DATE:
            continue
        values = sorted(index.value_lines[position])
        if len(values) > config.value_cap:
            values = sorted(sorted(values, key=_digest)[:config.value_cap])
        out.extend(Target(table.id, T_VALUE, position, value) for value in values)
    out.extend(Target(table.id, T_LINE, -1, label) for label in sorted(index.printed_labels) if label)
    out.extend(Target(table.id, T_BAND, -1, band) for band in sorted(band for band in index.bands if band))
    return out


def value_graph(table: CatalogTable, target: Target, config: UnitsConfig, order) -> Graph:
    """A stored value read with its column's name and cells of the line holding it; a label or band read with its table's context."""
    index = index_of(table)
    if target.kind in (T_LINE, T_BAND):
        texts = [table_context(table), _clip(target.text)]
        features = [_features(UNIT_TABLE), _features(UNIT_VALUE)]
        return Graph(texts, np.asarray(features, dtype=np.float32), table_edges([UNIT_TABLE, UNIT_VALUE], [-1, 0], [-1, 0]), {}, 0)
    position = target.position
    column = table.columns[position]
    digests = line_digests(table)
    lines = index.value_lines[position].get(target.text, ())
    line = table.lines[min(lines, key=lambda i: (digests[i], i))] if lines else None
    cell = line.cells.get(position) if line is not None else None
    texts = [_clip(column.name), _clip(cell.raw if cell is not None and cell.raw not in (None, "") else target.text)]
    features = [_features(UNIT_NAME, column.role, column.kind), _features(UNIT_VALUE, column.role, cell.kind if cell is not None else None)]
    kinds, cols, rows = [UNIT_NAME, UNIT_VALUE], [0, 0], [-1, 0]
    if line is not None:
        taken = 0
        for mate in order:
            if mate == position or taken == config.row_mates:
                continue
            held = line.cells.get(mate)
            if held is None:
                continue
            taken += 1
            texts.append(cell_text(held))
            features.append(_features(UNIT_VALUE, table.columns[mate].role, held.kind))
            kinds.append(UNIT_VALUE)
            cols.append(taken)
            rows.append(0)
    return Graph(texts, np.asarray(features, dtype=np.float32), table_edges(kinds, cols, rows), {}, 0)


def word_features(count: int) -> np.ndarray:
    return np.asarray([_features(UNIT_WORD)] * count, dtype=np.float32).reshape(count, FEATURES)


def question_graph(words: list[str]) -> Graph:
    return Graph(list(words), word_features(len(words)), question_edges(len(words)), {}, -1)


def collate(graphs: list[Graph], vectors: list[torch.Tensor]) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Graphs of one batch, padded: (input [B, S, dim + FEATURES], edges [B, S, S], key padding [B, S])."""
    device = vectors[0].device
    size = max(max(len(graph.texts) for graph in graphs), 1)
    dim = vectors[0].shape[-1]
    inputs = torch.zeros((len(graphs), size, dim + FEATURES), device=device)
    edges = torch.zeros((len(graphs), size, size), dtype=torch.long, device=device)
    pad = torch.ones((len(graphs), size), dtype=torch.bool, device=device)
    for at, (graph, vector) in enumerate(zip(graphs, vectors)):
        length = len(graph.texts)
        if not length:
            pad[at, 0] = False                     # a graph with no units still needs one to attend to
            continue
        inputs[at, :length, :dim] = vector
        inputs[at, :length, dim:] = torch.as_tensor(graph.features, device=device)
        edges[at, :length, :length] = torch.as_tensor(graph.edges, device=device)
        pad[at, :length] = False
    return inputs, edges, pad


__all__ = ["FEATURES", "Graph", "T_BAND", "T_COLUMN", "T_LINE", "T_TABLE", "T_VALUE", "TARGET_KINDS", "Target", "UnitsConfig", "cell_text", "collate", "question_graph", "sampled_lines", "table_context", "table_graphs", "targets_of", "value_graph", "word_features"]
