"""Tables of one schema merged into one table, with the member's name as a column: month tabs of one workbook (48% of tabs in the sample sit in such a family), and repeated side-by-side blocks of one tab.

Two tables are one family when their header names match at 0.8 or better with digits and month words masked, or, for tables with no usable names, when their first text column holds the same labels at 0.8 or better. Each member keeps its own lines and addresses: a merged line's cells are the members' cells. Column names alone cannot tell month tabs from one list sorted two ways, so a family carries how much its members repeat each other (MemberOverlap); the merge stays, and adding across it asks.
"""
from __future__ import annotations

import re
from collections import Counter

from engine.sheet.model import Axis, Cell, Column, Context, Family, Line, MemberOverlap, Stray, TabModel, Table
from engine.sheet.names import P_DAY, TAB_NAME, V_DATE, V_PERIOD, V_TEXT
from engine.sheet.values import parse_period

_MATCH = 0.8
_REPEATED = 0.9
_REPEATED_MEMBER_LINES = 10
BASIS_NAMES = "header_names"
BASIS_LABELS = "row_labels"
BASIS_BLOCKS = "repeated_blocks"


def _mask(name: str) -> str:
    """A header name with its digits masked, so "Volume 2001" and "Volume 2002" read as one member's column."""
    return re.sub(r"\d+", "#", name.casefold())


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
        return Cell(line.row, 0, name, V_DATE if period.kind == P_DAY else V_PERIOD, period.start if period.kind == P_DAY else name, period)
    return Cell(line.row, 0, name, V_TEXT, name)


def _overlaps(member_names: list[str], member_lines: list[Counter]) -> tuple[tuple[MemberOverlap, ...], bool]:
    """Each member's lines against the others': the same cell values in the same merged columns, counted with their repeats."""
    out: list[MemberOverlap] = []
    twice = False
    for at, (name, lines) in enumerate(zip(member_names, member_lines)):
        count = sum(lines.values())
        others: Counter = Counter()
        inside: list[str] = []
        for other_at, (other_name, other) in enumerate(zip(member_names, member_lines)):
            if other_at == at:
                continue
            others.update(other)
            if count and sum((lines & other).values()) >= _REPEATED * count:
                inside.append(other_name)
        share = sum((lines & others).values()) / count if count else 0.0
        out.append(MemberOverlap(name, count, round(share, 4), tuple(inside)))
        twice = twice or (count >= _REPEATED_MEMBER_LINES and share >= _REPEATED)
    return tuple(out), twice


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
    member_lines: list[Counter] = []
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
        member_lines.append(Counter(tuple(sorted((mapping[p], str(cell.value)) for p, cell in line.cells.items())) for line in table.lines))
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
    overlap, twice = _overlaps(member_names, member_lines)
    return Family(name=first.name, basis=basis, members=tuple((t.tab, t.index) for t in tables), table=merged, overlap=overlap, adds_twice=twice)


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
                names = [str(t.block) for t in group]
                families.append(merge_tables(group, names, "", BASIS_BLOCKS))
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
            families.append(merge_tables(group, [t.tab for t in group], "", basis))
    return families
