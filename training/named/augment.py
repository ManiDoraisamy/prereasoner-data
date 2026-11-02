"""What a sheet can do to its headers and columns: renames, masks, shuffles, added distractors, unreadable names. Training reads tables through these so a coordinate comes to rest on a column's meaning, not its name's spelling; the evaluation's survival tests read through the same four.

Each returns a new Table and where each old position went. Nothing changes a cell of a column that stays, so an answer stands.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import replace
from typing import Callable

from engine.program.catalog import Catalog, CatalogTable
from engine.sheet.formulas import column_letter
from engine.sheet.grid import read_cell
from engine.sheet.model import Column, Line, Table
from engine.sheet.names import MEASURE, V_NUMBER, V_TEXT
from training.factory.noise import SHORTHAND

_VOWELS = str.maketrans("", "", "aeiouAEIOU")
_UNRELATED = ("Ref", "Internal code", "Batch", "Seq", "Flag", "Audit", "Reviewer", "Load id", "Rev", "Source file", "Check", "Row id", "Stamp", "Grp", "Note", "Tmp", "Key2", "Old ref", "Export", "Misc")


def _rng(*parts: object) -> random.Random:
    return random.Random(int.from_bytes(hashlib.blake2b("|".join(str(part) for part in parts).encode("utf-8", "surrogatepass"), digest_size=8).digest(), "little"))


def _line(line: Line, mapping: dict[int, int], extra: dict[int, object] | None = None) -> Line:
    cells = {mapping[position]: cell for position, cell in line.cells.items() if position in mapping}
    cells.update(extra or {})
    return replace(line, cells=cells)


def rebuilt(table: Table, order: list[int], names: dict[int, str] | None = None) -> tuple[Table, dict[int, int]]:
    """The table with its columns in ``order`` and some renamed. Returns it and old position -> new."""
    mapping = {old: new for new, old in enumerate(order)}
    names = names or {}
    columns = [replace(table.columns[old], position=new, name=names.get(old, table.columns[old].name)) for new, old in enumerate(order)]
    axes = tuple(replace(axis, positions=tuple(mapping[p] for p in axis.positions if p in mapping)) for axis in table.axes)
    strays = tuple(replace(stray, position=mapping[stray.position]) for stray in table.strays if stray.position in mapping)
    return replace(table, columns=columns, lines=[_line(line, mapping) for line in table.lines], printed=[_line(line, mapping) for line in table.printed], axes=axes, strays=strays), mapping


def reorder_columns(table: Table, seed: object = 0) -> tuple[Table, dict[int, int]]:
    order = list(range(len(table.columns)))
    _rng("reorder", table.name, seed).shuffle(order)
    return rebuilt(table, order)


def renamed(name: str, rng: random.Random) -> str:
    """The same name as another desk would write it: the trade's shorthand, or its vowels dropped from a long word."""
    out = []
    for word in name.split():
        low = word.lower().strip(".,:;()")
        if low in SHORTHAND:
            out.append(SHORTHAND[low])
        elif len(low) >= 5 and rng.random() < 0.7:
            out.append(low[0] + low[1:].translate(_VOWELS))
        else:
            out.append(word)
    return " ".join(out)


def rename_columns(table: Table, seed: object = 0) -> tuple[Table, dict[int, int]]:
    rng = _rng("rename", table.name, seed)
    names = {column.position: renamed(column.name, rng) for column in table.columns}
    return rebuilt(table, list(range(len(table.columns))), names)


def unreadable_headers(table: Table, seed: object = 0) -> tuple[Table, dict[int, int]]:
    """Every header gone: what the compiler calls a column nothing names."""
    names = {column.position: f"Column {column.letter or column_letter(column.position + 1)}" for column in table.columns}
    return rebuilt(table, list(range(len(table.columns))), names)


def mask_some(table: Table, rng: random.Random, share: float = 0.3) -> tuple[Table, dict[int, int]]:
    names = {column.position: f"Column {column.letter or column_letter(column.position + 1)}" for column in table.columns if rng.random() < share}
    return rebuilt(table, list(range(len(table.columns))), names)


def rename_some(table: Table, rng: random.Random, share: float = 0.3) -> tuple[Table, dict[int, int]]:
    names = {column.position: renamed(column.name, rng) for column in table.columns if rng.random() < share}
    return rebuilt(table, list(range(len(table.columns))), names)


def shuffle(table: Table, rng: random.Random) -> tuple[Table, dict[int, int]]:
    order = list(range(len(table.columns)))
    rng.shuffle(order)
    return rebuilt(table, order)


def add_columns(table: Table, seed: object = 0, count: int = 20) -> tuple[Table, dict[int, int]]:
    """count distractor columns after the real ones, half figures, half codes, under names that mean nothing here. Old positions keep."""
    rng = _rng("add", table.name, seed)
    start = len(table.columns)
    base = max((column.col for column in table.columns), default=0)
    columns = list(table.columns)
    held: dict[int, dict[int, object]] = {}
    for offset in range(count):
        position = start + offset
        col = base + offset + 1
        name = f"{_UNRELATED[offset % len(_UNRELATED)]} {rng.randrange(10, 99)}"
        number = offset % 2 == 0
        kinds = 0
        for row_at, line in enumerate(table.lines):
            raw = float(rng.randrange(1, 100000)) if number else f"X{rng.randrange(1000, 9999)}"
            cell = read_cell(line.row, col, raw, None, False)
            if cell is not None:
                held.setdefault(row_at, {})[position] = cell
                kinds += 1
        columns.append(Column(position=position, col=col, letter=column_letter(col), name=name, role=MEASURE if number else "label", kind=V_NUMBER if number else V_TEXT, filled=kinds, distinct=kinds))
    lines = [replace(line, cells={**line.cells, **held.get(row_at, {})}) for row_at, line in enumerate(table.lines)]
    return replace(table, columns=columns, lines=lines), {position: position for position in range(start)}


def distract(table: Table, rng: random.Random, most: int = 6) -> tuple[Table, dict[int, int]]:
    return add_columns(table, rng.random(), rng.randrange(1, most + 1))


TRAINING_OPS: dict[str, Callable] = {"rename": rename_some, "mask": mask_some, "unreadable": unreadable_headers, "shuffle": shuffle, "distract": distract}
TRAINING_SHARES = {"rename": 0.3, "mask": 0.2, "unreadable": 0.05, "shuffle": 0.5, "distract": 0.3}
SURVIVAL_OPS: dict[str, Callable] = {"reorder": reorder_columns, "added_columns": add_columns, "renames": rename_columns, "unreadable_headers": unreadable_headers}


def augmented(table: Table, rng: random.Random, shares: dict[str, float] | None = None) -> tuple[Table, dict[int, int]]:
    """One table through the training ops each in turn, each by its share. Old position -> final position, where it still stands."""
    shares = TRAINING_SHARES if shares is None else shares
    mapping = {column.position: column.position for column in table.columns}
    for name, operation in TRAINING_OPS.items():
        if rng.random() < shares.get(name, 0.0):
            table, step = operation(table, rng)
            mapping = {old: step[new] for old, new in mapping.items() if new in step}
    return table, mapping


def mutate_catalog(catalog: Catalog, operation: Callable, seed: object = 0) -> Catalog:
    """A whole workbook through one op, ids kept: a question's gold answer is untouched by any of these."""
    return Catalog([CatalogTable(table.id, operation(table.table, seed)[0], table.kind, table.adds_twice) for table in catalog.tables], catalog.name)


__all__ = ["SURVIVAL_OPS", "TRAINING_OPS", "TRAINING_SHARES", "add_columns", "augmented", "mutate_catalog", "rebuilt", "rename_columns", "renamed", "reorder_columns", "unreadable_headers"]
