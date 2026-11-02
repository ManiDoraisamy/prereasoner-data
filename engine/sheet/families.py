"""Tables of one schema merged into one table, with the member's name as a column: month tabs of one workbook (48% of tabs in the sample sit in such a family), and repeated side-by-side blocks of one tab.

Two tables are one family when their header names match at 0.8 or better with digits and month words masked, or, for tables with no usable names, when their first text column holds the same labels at 0.8 or better. Each member keeps its own lines and addresses: a merged line's cells are the members' cells.
"""
from __future__ import annotations

import re

from engine.sheet.model import Axis, Cell, Column, Context, Family, Line, Stray, TabModel, Table
from engine.sheet.names import TAB_NAME, V_DATE, V_PERIOD, V_TEXT
from engine.sheet.values import parse_period

_MONTH = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", re.I)
_MATCH = 0.8
BASIS_NAMES = "header_names"
BASIS_LABELS = "row_labels"
BASIS_BLOCKS = "repeated_blocks"


def _mask(name: str) -> str:
    return re.sub(r"\d+", "#", _MONTH.sub("@", name.lower()))


def _signature(table: Table) -> set[str]:
    return {_mask(c.name) for c in table.columns if c.path}


def _labels(table: Table) -> set[str]:
    for column in table.columns:
        if column.kind == V_TEXT:
            return {str(line.cells[column.position].value).lower() for line in table.lines if column.position in line.cells}
    return set()


def _alike(a: set[str], b: set[str]) -> bool:
    return bool(a) and bool(b) and len(a & b) / len(a | b) >= _MATCH


def _member_cell(line: Line, name: str) -> Cell:
    """The member's name as a cell of the merged table; column 0 says it comes from the tab, not from a cell on it."""
    period = parse_period(name)
    if period is not None and period.start is not None:
        return Cell(line.row, 0, name, V_DATE if period.kind == "day" else V_PERIOD, period.start if period.kind == "day" else name, period)
    return Cell(line.row, 0, name, V_TEXT, name)


def merge_tables(tables: list[Table], member_names: list[str], key_name: str, basis: str) -> Family:
    """One table from several: a first column naming the member, columns matched by name, then by masked name at the same position; a column no earlier member has is added."""
    first = tables[0]
    columns = [Column(position=0, col=0, letter="", name=key_name, role=TAB_NAME, kind=V_TEXT, filled=0, distinct=len(tables))]
    by_exact: dict[str, int] = {}
    by_mask: dict[tuple[int, str], int] = {}
    lines: list[Line] = []
    printed: list[Line] = []
    strays: list[Stray] = []
    axes: list[Axis] = []
    seen_axes: set[tuple[str, tuple[int, ...]]] = set()
    for table, member in zip(tables, member_names):
        mapping: dict[int, int] = {}
        for column in table.columns:
            exact, mask = column.name.lower(), (column.position, _mask(column.name))
            at = by_exact.get(exact, by_mask.get(mask))
            if at is None or at in mapping.values():
                at = len(columns)
                columns.append(Column(position=at, col=column.col, letter=column.letter, name=column.name, path=column.path, header_cells=column.header_cells, role=column.role, kind=column.kind,
                                      format_kind=column.format_kind, unit=column.unit, hidden=column.hidden, filled=0, distinct=0))
                by_exact[exact] = at
                by_mask[mask] = at
            mapping[column.position] = at
            columns[at].filled += column.filled
            columns[at].distinct = max(columns[at].distinct, column.distinct)
        for source, target in ((table.lines, lines), (table.printed, printed)):
            for line in source:
                cells = {0: _member_cell(line, member)}
                cells.update((mapping[p], cell) for p, cell in line.cells.items())
                target.append(Line(tab=line.tab, row=line.row, role=line.role, cells=cells, signals=line.signals, hidden=line.hidden, section=line.section, label=line.label, scope=line.scope))
        columns[0].filled += len(table.lines)
        strays.extend(Stray(s.address, s.tab, s.row, mapping[s.position], s.kind, s.raw) for s in table.strays)
        for axis in table.axes:
            key = (axis.kind, tuple(mapping[p] for p in axis.positions))
            if key not in seen_axes:
                seen_axes.add(key)
                axes.append(Axis(axis.kind, axis.name, key[1]))
    context = Context(tab=first.tab, titles=first.context.titles, unit=first.context.unit, unit_address=first.context.unit_address)
    merged = Table(tab=first.tab, index=first.index, name=first.name, columns=columns, lines=lines, printed=printed, header_rows=first.header_rows, first_row=first.first_row, last_row=first.last_row,
                   context=context, axes=tuple(axes), strays=tuple(strays))
    return Family(name=first.name, basis=basis, members=tuple((t.tab, t.index) for t in tables), table=merged)


def find_families(tabs: list[TabModel]) -> list[Family]:
    """Families across tabs and repeated blocks on a tab, in workbook order. A table joins the first family it matches; order of tabs decides, so the result does not depend on hashing."""
    families: list[Family] = []
    for tab in tabs:
        blocks: dict[tuple[str, ...], list[Table]] = {}
        for table in tab.tables:
            if table.block:
                blocks.setdefault(tuple(c.name.lower() for c in table.columns), []).append(table)
        for key in blocks:
            group = blocks[key]
            if len(group) >= 2:
                names = [f"block {t.block}" for t in group]
                families.append(merge_tables(group, names, "Block", BASIS_BLOCKS))
    groups: list[tuple[str, list[Table]]] = []
    for tab in tabs:
        for table in tab.tables:
            names = _signature(table)
            labels = _labels(table) if len(names) < 2 else set()
            if len(names) < 2 and len(labels) < 3:
                continue
            for basis, group in groups:
                if group[-1].tab != tab.name and all(t.tab != tab.name for t in group):
                    if basis == BASIS_NAMES and _alike(names, _signature(group[0])):
                        group.append(table)
                        break
                    if basis == BASIS_LABELS and _alike(labels, _labels(group[0])):
                        group.append(table)
                        break
            else:
                groups.append((BASIS_NAMES if len(names) >= 2 else BASIS_LABELS, [table]))
    for basis, group in groups:
        if len(group) >= 2:
            families.append(merge_tables(group, [t.tab for t in group], "Tab", basis))
    return families
