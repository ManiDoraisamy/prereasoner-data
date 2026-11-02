"""The sheet compiler: one tab's payload to its tables, with roles for every line and column.

Order of work on a tab: typed cells (grid.py), line roles (line_roles.py), regions (a header block and the lines under it, stacked tables split), blocks (side-by-side tables split), then one
table per block: header paths joined across rows with column letters kept, sections from bands and from sparse label columns, printed lines set apart with their label and scope, peer columns
recorded as an axis, stray cells set apart by kind, context from the title lines. A clean table comes out unchanged. Families across tabs are merged in families.py.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from engine.sheet.formulas import column_letter
from engine.sheet.grid import Grid, clean
from engine.sheet.line_facts import TOTAL_WORD, is_total_mark
from engine.sheet.line_roles import read_roles, repeats_line
from engine.sheet.model import Axis, Cell, Column, Context, Line, Note, Stray, TabModel, Table
from engine.sheet.names import (AXIS_FLAG, AXIS_GROUP, AXIS_PERIOD, AXIS_SEQUENCE, AXIS_TOTALLED, BAND, BLANK, DATE, DERIVED, F_CURRENCY, F_GENERAL, F_NUMBER, F_PERCENT, FX_AGG_COLUMN, FX_AGG_ROW, FX_ROW_ARITH, FX_VOLATILE, HEADER, KEY, LABEL, MEASURE, MIXED, NOTE,
                                P_YEAR, PEER, PERIOD, PRINTED_ROLES, RECORD, ROW_TOTAL, S_DATA_LINE, S_FILLED_DOWN, S_REPEATED_HEADER, S_RULE_LINE, S_STRAY_KEY, S_TOTAL_OTHER_BLOCK, S_TOTAL_WORD, SECTION_LABEL, SHEET_CHART, SPACER, STRAY_DATE,
                                STRAY_ERROR, STRAY_LABEL, STRAY_MARKER, STRAY_NOTE, STRAY_NUMBER_WITH_WORD, STRAY_REPEATED_HEADER, STRAY_TOTAL, SUBTOTAL, TITLE, TOTAL, V_BLANK, V_BOOL, V_DATE, V_ERROR, V_MARKER, V_NUMBER, V_PERIOD, V_TEXT)
from engine.sheet.payload import TabPayload
from engine.sheet.values import Period, number_with_word, parse_period, year_number

_UNIT_IN_TITLE = re.compile(r"\$\s?000s?|\$\s?MM\b|\(000'?s?\)|\bin\s+(?:thousands|millions|billions)\b|\bin\s+(?:[A-Z]{2,}\w*|US\$|\$\w*)")
_UNIT_IN_NAME = re.compile(r"\(([^()]{1,12})\)\s*$")
_DATE_IN_TEXT = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{1,2}-[A-Za-z]{3,9}-\d{2,4}\b|\b[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\b|\b[A-Za-z]{3,9}\.?[\s'-]+\d{4}\b|\b[A-Za-z]{3,9}-\d{2}\b|\b\d{4}-\d{2}-\d{2}\b")
_TOTAL_MEMBER = re.compile(r"^\s*(?:grand\s*|sub-?\s*)?totals?\b|\btotals?\s*$", re.I)
_DIGITS = re.compile(r"\d+")
_FILTER_RECENT = 30
_FIT_SHARE = 0.8
_ADJACENT_MOST = 0.7
_AXIS_TIE_SHARE = 0.8
_LINED_UP = 0.7
_STRAY_KEY_DATED = 0.7
_GRAND_TOTAL = re.compile(r"\bgrand\s*total", re.I)
_KEYS_ALONE = 0.6
_FLAG_VALUES = 3
_FLAG_LENGTH = 3


@dataclass(slots=True)
class _Region:
    """A header block and the filled lines under it, down to the next table."""
    header_rows: list[int] = field(default_factory=list)
    body: list[int] = field(default_factory=list)
    repeats: list[list[int]] = field(default_factory=list)     # the repeated header blocks, each in row order
    titles: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Block:
    """One table's columns of a region: a range, plus key columns left of every block that each block shares."""
    first: int
    last: int
    shared: tuple[int, ...] = ()

    def holds(self, column: int) -> bool:
        return self.first <= column <= self.last or column in self.shared

    def span(self) -> list[int]:
        return [*self.shared, *range(self.first, self.last + 1)]


def _label_of(cell: Cell) -> str:
    """A cell as the words of a name: what the sheet wrote, so a question's word still meets it."""
    if cell.kind == V_NUMBER:
        if isinstance(cell.raw, str):
            return clean(cell.raw)
        return str(int(cell.value)) if float(cell.value).is_integer() else str(cell.value)
    if cell.kind == V_DATE or (cell.kind == V_PERIOD and cell.value == cell.period.start):
        return str(cell.value) if cell.value is not None else clean(str(cell.raw))
    return clean(str(cell.value if cell.value is not None else cell.raw))


def _names_of(grid: Grid, rows: list[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    for r in rows:
        for c, cell in grid.rows[r].items():
            names[c] = _label_of(cell).lower()
    return names


def _repeats_block(grid: Grid, rows: list[int], names: dict[int, str]) -> bool:
    """Most of a later header block's cells say what the table's header says in the same columns."""
    if not names:
        return False
    here = _names_of(grid, rows)
    same = sum(1 for c, text in here.items() if names.get(c) == text)
    return same >= max(2, len(here) * 0.6)


def _repeated_lines(grid: Grid, block: list[int], region: _Region, roles: dict[int, tuple[str, tuple[str, ...]]]) -> int:
    """How many leading lines of a header block repeat the table's own header; 0 when the block opens a new table. A block whose first line repeats the header and whose later lines are lines of that table's data repeats only that far; later lines that name something else open a table of their own, however like the first line a units line is."""
    heads = [grid.rows[h] for h in region.header_rows]
    k = 0
    while k < len(block) and any(repeats_line(grid.rows[block[k]], h) for h in heads):
        k += 1
    if k:
        return k if all(_fits_body(grid, r, region, roles) for r in block[k:]) else 0
    return len(block) if _repeats_block(grid, block, _names_of(grid, region.header_rows)) else 0


def _kind_group(kind: str) -> str:
    return V_NUMBER if kind in (V_NUMBER, V_ERROR, V_MARKER) else V_DATE if kind in (V_DATE, V_PERIOD) else V_TEXT


def _fits_body(grid: Grid, r: int, region: _Region, roles: dict[int, tuple[str, tuple[str, ...]]]) -> bool:
    """A lone line called a header that holds figures and whose cells fit the columns of the table open above it: a data line. A header names columns; this one's cells are what those columns hold."""
    line = grid.rows[r]
    if not any(cell.kind == V_NUMBER and not year_number(cell.value) for cell in line.values()):
        return False
    recent = [x for x in region.body if roles[x][0] == RECORD][-_FILTER_RECENT:]
    if len(recent) < 3:
        return False
    fit = 0
    for c, cell in line.items():
        kinds = Counter(_kind_group(grid.rows[x][c].kind) for x in recent if c in grid.rows[x])
        if kinds and min(kinds, key=lambda k: (-kinds[k], k)) == _kind_group(cell.kind):
            fit += 1
    return fit >= _FIT_SHARE * len(line)


def _regions(grid: Grid, roles: dict[int, tuple[str, tuple[str, ...]]]) -> list[_Region]:
    """Walk the filled lines: a new header block closes the table above it, unless it repeats that table's own header or its one line is a line of that table; lines with no header above them form a table of their own. A header line found to be data takes the record role here."""
    regions: list[_Region] = []
    current: _Region | None = None
    pending_titles: list[int] = []
    filled = grid.filled
    i = 0
    while i < len(filled):
        r = filled[i]
        role = roles[r][0]
        if role == HEADER:
            block = [r]
            while i + 1 < len(filled) and roles[filled[i + 1]][0] == HEADER and filled[i + 1] - block[-1] <= 2:
                i += 1
                block.append(filled[i])
            repeated = _repeated_lines(grid, block, current, roles) if current is not None and current.header_rows else 0
            if repeated:
                current.repeats.append(block[:repeated])
                for x in block[repeated:]:
                    roles[x] = (RECORD, (S_DATA_LINE,))
                    current.body.append(x)
            elif current is not None and len(block) == 1 and _fits_body(grid, r, current, roles):
                roles[r] = (RECORD, (S_DATA_LINE,))
                current.body.append(r)
            else:
                # A band directly over a header names the table under it, not the end of the one above.
                while current is not None and current.body and roles[current.body[-1]][0] == BAND:
                    pending_titles.insert(0, current.body.pop())
                current = _Region(header_rows=block, titles=pending_titles)
                pending_titles = []
                regions.append(current)
        elif role == TITLE:
            pending_titles.append(r)
        elif role == NOTE:
            if current is None:
                pending_titles.append(r)
            else:
                current.body.append(r)
        else:
            if current is None:
                current = _Region(titles=pending_titles)
                pending_titles = []
                regions.append(current)
            current.body.append(r)
        i += 1
    return regions


def _closes_table(grid: Grid, r: int, printed: list[int], later_printed: list[int]) -> bool:
    """A printed line that totals every printed line above it, in a table no later printed line adds it into, closes its table: its formula reads them all, it ties to their sum, or it says grand total. One printed line closes nothing: it may be the first section's. A total some later line reads is a level of the same table."""
    for x in later_printed:
        for c in grid.rows[x]:
            shape = grid.shapes.get((x, c))
            if shape is not None and shape.kind == FX_AGG_COLUMN and shape.span and shape.span[0] <= r <= shape.span[1]:
                return False
    if any(cell.kind == V_TEXT and _GRAND_TOTAL.search(str(cell.value)) for cell in grid.rows[r].values()):
        return True
    for c, cell in grid.rows[r].items():
        shape = grid.shapes.get((r, c))
        if printed and shape is not None and shape.kind == FX_AGG_COLUMN and shape.span and all(shape.span[0] <= p <= shape.span[1] for p in printed):
            return True
        # A tie counts only as far as it can't be chance: exact, nonzero, four digits.
        if len(printed) >= 2 and cell.kind == V_NUMBER and abs(cell.value) >= 1000:
            cells = [grid.rows[p][c] for p in printed if c in grid.rows[p] and grid.rows[p][c].kind == V_NUMBER]
            if len(cells) >= 2 and abs(sum(x.value for x in cells) - cell.value) < 0.01:
                return True
    return False


def _split_at_closing_totals(grid: Grid, regions: list[_Region], roles: dict[int, tuple[str, tuple[str, ...]]]) -> list[_Region]:
    """Records that resume after a table's closing total, with no header of their own, are another table on the same header."""
    out: list[_Region] = []
    for region in regions:
        current = region
        if region.header_rows:
            printed: list[int] = []
            for at, r in enumerate(region.body):
                if roles[r][0] != TOTAL:
                    continue
                later = region.body[at + 1:]
                if _closes_table(grid, r, printed, [x for x in later if roles[x][0] == TOTAL]) and sum(1 for x in later if roles[x][0] == RECORD) >= 2:
                    keep = set(current.body) - set(later)
                    nearby = [rows for rows in current.repeats if rows[0] > r]
                    out.append(_Region(region.header_rows, [x for x in current.body if x in keep], [rows for rows in current.repeats if rows[0] < r], current.titles))
                    current = _Region(region.header_rows, later, nearby, [])
                    printed = []
                else:
                    printed.append(r)
        out.append(current)
    return out


def _title_lines(grid: Grid, region: _Region, holds=lambda column: True, header_rows: list[int] | None = None) -> list[int]:
    """The table's own header lines that title its first block: an upper line none of whose cells comes back in any repeat, while the lines under it do."""
    header_rows = region.header_rows if header_rows is None else header_rows
    names: dict[int, set[str]] = {}
    for h in header_rows:
        for c, cell in grid.rows[h].items():
            names.setdefault(c, set()).add(_label_of(cell).lower())
    titles: set[int] = set()
    for rows in region.repeats:
        for at, r in enumerate(rows):
            cells = [cell for c, cell in grid.rows[r].items() if holds(c)]
            new = [cell for cell in cells if _label_of(cell).lower() not in names.get(cell.col, ())]
            aligned = len(header_rows) - len(rows) + at
            if cells and len(new) == len(cells) and at + 1 < len(rows) and 0 <= aligned < len(header_rows) - 1:
                titles.add(header_rows[aligned])
    return sorted(titles)


def _blocks_under_titles(grid: Grid, region: _Region, columns: list[int]) -> list[_Block]:
    """Side-by-side blocks with no gutter between them: a title line with one cell per block, the names under each title repeating. COB PURCHASES over MW, Price and COB SALES over MW, Price are two tables."""
    for title in _title_lines(grid, region):
        starts = sorted(grid.rows[title])
        if len(starts) < 2:
            continue
        below = grid.rows[region.header_rows[-1]]
        ends = [nxt - 1 for nxt in starts[1:]] + [columns[-1]]
        spans = [[(c - first, _label_of(below[c]).lower()) for c in sorted(below) if first <= c <= last] for first, last in zip(starts, ends)]
        if spans[0] and all(span == spans[0] for span in spans):
            return [_Block(first, last, tuple(c for c in columns if c < starts[0])) for first, last in zip(starts, ends)]
    return []


def _column_groups(grid: Grid, region: _Region, roles: dict[int, tuple[str, tuple[str, ...]]]) -> list[_Block]:
    """Side-by-side blocks: columns cut at blank gutters, each block with its own key column and a measure of its own or lines of its own; or names repeating in order (A B | A B) with any column left of them going with every block. One block when the region is one table. Every column used lands in a block."""
    rows = region.header_rows + region.body
    used: set[int] = set()
    for r in rows:
        used.update(grid.rows[r])
    if not used:
        return []
    columns = sorted(used)
    whole = [_Block(columns[0], columns[-1])]
    if not region.header_rows:
        return whole
    groups: list[list[int]] = [[columns[0]]]
    for c in columns[1:]:
        if c == groups[-1][-1] + 1:
            groups[-1].append(c)
        else:
            groups.append([c])
    record_rows = [r for r in region.body if roles[r][0] == RECORD]

    def lines_of(group: list[int]) -> set[int]:
        return {r for r in record_rows if any(c in grid.rows[r] for c in group)}

    def has_own_key(group: list[int]) -> bool:
        if len(group) < 2:
            return False
        lines = lines_of(group)
        if len(lines) < 2:
            return False
        lead = sum(1 for r in lines if (cell := grid.rows[r].get(group[0])) is not None and cell.kind in (V_TEXT, V_DATE, V_PERIOD))
        named = sum(1 for c in group if any(c in grid.rows[h] for h in region.header_rows))
        return lead * 10 >= len(lines) * 6 and named >= 2

    def has_measure(group: list[int]) -> bool:
        for c in group:
            cells = [grid.rows[r][c] for r in record_rows if c in grid.rows[r]]
            if len(cells) >= 2 and sum(1 for cell in cells if cell.kind == V_NUMBER) * 2 >= len(cells):
                return True
        return False

    def lines_up(a: list[int], b: list[int]) -> bool:
        here, there = lines_of(a), lines_of(b)
        return bool(here) and len(here & there) >= _LINED_UP * len(here)

    merged: list[list[int]] = []
    for group in groups:
        if merged and not has_own_key(group):
            merged[-1].extend(group)
        else:
            merged.append(list(group))
    # A block with no measure on the same lines as its neighbour is that neighbour's labels, across a spacer column.
    at = 0
    while len(merged) >= 2 and at < len(merged):
        neighbour = at - 1 if at else 1
        if not has_measure(merged[at]) and lines_up(merged[at], merged[neighbour]):
            merged[neighbour] = sorted(merged[neighbour] + merged[at])
            del merged[at]
            at = 0
            continue
        at += 1
    if len(merged) >= 2 and has_own_key(merged[0]):
        return [_Block(g[0], g[-1]) for g in merged]
    if titled := _blocks_under_titles(grid, region, columns):
        return titled
    names = _names_of(grid, region.header_rows)
    named = sorted(names)
    texts = [names[c] for c in named]
    for size in range(2, len(named) // 2 + 1):
        # All names alike is a run of peers, not blocks.
        if len(named) % size == 0 and len(set(texts[:size])) >= 2 and all(texts[k:k + size] == texts[:size] for k in range(0, len(named), size)):
            starts = [named[k] for k in range(0, len(named), size)]
            ends = [nxt - 1 for nxt in starts[1:]] + [columns[-1]]
            left = tuple(c for c in columns if c < starts[0])
            return [_Block(first, last, left) for first, last in zip(starts, ends)]
    return whole


def _header_paths(grid: Grid, header_rows: list[int], span: list[int]) -> dict[int, tuple[list[str], list[str]]]:
    """Each column's name path and the addresses it came from: a merge gives its text to the columns it spans; on an upper line of a block, a label carries right over the columns the line below names, until the next label."""
    paths: dict[int, tuple[list[str], list[str]]] = {c: ([], []) for c in span}
    inside = set(span)
    for at, r in enumerate(header_rows):
        line = grid.rows[r]
        lower = set()
        for below in header_rows[at + 1:]:
            lower.update(grid.rows[below])
        held: Cell | None = None
        upper_is_sparse = bool(lower) and len([c for c in line if c in inside]) < len([c for c in lower if c in inside])
        previous = None
        for c in span:
            if previous is not None and c != previous + 1:
                held = None
            previous = c
            cell = line.get(c)
            if cell is None:
                source = grid.spans.get((r, c))
                if source is not None and source[0] == r:
                    cell = line.get(source[1])
                elif source is not None and source[0] in grid.rows:
                    cell = grid.rows[source[0]].get(source[1])
            if cell is not None:
                held = cell
            elif held is not None and upper_is_sparse and c in lower:
                cell = held
            else:
                held = None
            if cell is None:
                continue
            words = _label_of(cell)
            path, sources = paths[c]
            if not path or path[-1].lower() != words.lower():
                path.append(words)
                sources.append(cell.address)
    return paths


def _unique_names(paths: dict[int, tuple[list[str], list[str]]], columns: list[int]) -> list[str]:
    seen: Counter = Counter()
    names = []
    for c in columns:
        base = " ".join(paths[c][0]) or f"Column {column_letter(c)}"
        seen[base.lower()] += 1
        names.append(base if seen[base.lower()] == 1 else f"{base} [column {column_letter(c)}]")
    return names


def _stray_kind(cell: Cell, header_name: str) -> str | None:
    """What a cell is when it does not fit its column's type."""
    if cell.kind == V_ERROR:
        return STRAY_ERROR
    if cell.kind == V_MARKER:
        return STRAY_MARKER
    if cell.kind in (V_DATE, V_PERIOD):
        return STRAY_DATE
    if cell.kind == V_BOOL:
        return STRAY_LABEL
    if cell.kind == V_NUMBER:
        return STRAY_LABEL
    text = str(cell.value)
    if text.lower() == header_name.lower():
        return STRAY_REPEATED_HEADER
    if TOTAL_WORD.search(text):
        return STRAY_TOTAL
    if number_with_word(text) is not None:
        return STRAY_NUMBER_WITH_WORD
    if len(text) > 40 or len(text.split()) >= 6:
        return STRAY_NOTE
    return STRAY_LABEL


def _type_column(column: Column, cells: list[Cell], strays: list[Stray], tab: str) -> None:
    """A column's type by its majority, markers aside; cells of a typed column that do not fit are set apart by kind and stay on their lines."""
    counts = Counter(cell.kind for cell in cells)
    column.filled = len(cells)
    column.distinct = len({str(cell.value) for cell in cells})
    counted = len(cells) - counts[V_MARKER] - counts[V_ERROR]
    if not cells:
        column.kind, column.role = V_BLANK, SPACER
        return
    if counted <= 0:
        column.kind, column.role = V_TEXT, LABEL
        return
    top = V_NUMBER if counts[V_NUMBER] * 2 >= counted and counts[V_NUMBER] else V_DATE if counts[V_DATE] * 2 >= counted and counts[V_DATE] else V_PERIOD if counts[V_PERIOD] * 2 >= counted and counts[V_PERIOD] else None
    if top is None:
        top_share = max(counts[V_TEXT], 0) * 2 >= counted
        column.kind = V_TEXT if top_share else MIXED
    else:
        column.kind = top
    if top in (V_NUMBER, V_DATE, V_PERIOD) and len(cells) >= 3:
        for cell in cells:
            if cell.kind != top and not (top in (V_DATE, V_PERIOD) and cell.kind in (V_DATE, V_PERIOD)):
                cell.stray = _stray_kind(cell, column.name)
                strays.append(Stray(cell.address, tab, cell.row, column.position, cell.stray, cell.raw))
    if top == V_NUMBER:
        numbers = [cell for cell in cells if cell.kind == V_NUMBER]
        shapes = Counter(cell.shape for cell in numbers)
        formats = Counter(cell.format_kind for cell in numbers)
        column.format_kind = min(formats, key=lambda f: (-formats[f], f))
        if shapes[FX_AGG_ROW] * 2 >= len(numbers):
            column.role = ROW_TOTAL
        elif shapes[FX_ROW_ARITH] * 2 >= len(numbers):
            column.role = DERIVED
        else:
            column.role = MEASURE
    elif top == V_DATE:
        column.role = DATE
    elif top == V_PERIOD:
        column.role = PERIOD
    else:
        column.role = KEY if column.distinct * 10 >= len(cells) * 9 else LABEL


def _unit_of(column: Column) -> str:
    if column.format_kind == F_CURRENCY:
        return "$"
    if column.format_kind == F_PERCENT:
        return "%"
    m = _UNIT_IN_NAME.search(column.name)
    return m.group(1) if m else ""


def _ties_to_others(member: Column, others: list[Column], records: list[Line]) -> bool:
    """A member whose value is the sum of the members before it, on most lines where it holds a nonzero figure: it adds up its axis, so it is no peer of what it adds."""
    if len(others) < 2:
        return False
    nonzero = tied = 0
    for line in records:
        cell = line.cells.get(member.position)
        if cell is None or cell.kind != V_NUMBER or not cell.value:
            continue
        nonzero += 1
        total = sum(c.value for o in others if (c := line.cells.get(o.position)) is not None and c.kind == V_NUMBER)
        tied += abs(total - cell.value) < 0.01
    return nonzero >= 3 and tied >= _AXIS_TIE_SHARE * nonzero


def _true_members(members: list[Column], records: list[Line]) -> list[Column]:
    """An axis's members, without those that print a total of the others: a member whose own name says total, or one that ties to those before it. It is marked as what it is."""
    kept: list[Column] = []
    for column in members:
        word = bool(column.path) and bool(_TOTAL_MEMBER.search(column.path[-1]))
        if word or _ties_to_others(column, kept, records):
            column.role = ROW_TOTAL
        else:
            kept.append(column)
    return kept


def _format_family(column: Column) -> str:
    return F_NUMBER if column.format_kind in (F_GENERAL, F_NUMBER) else column.format_kind


def _flag_axes(columns: list[Column], records: list[Line], taken: set[int]) -> list[Axis]:
    """Runs of 3+ text columns marking the same few short values, Y and N across: peers a question counts over. Spacers do not break a run."""
    axes: list[Axis] = []
    run: list[Column] = []
    seen: set[str] = set()

    def close() -> None:
        if len(run) >= 3:
            axes.append(Axis(AXIS_FLAG, "", tuple(c.position for c in run)))

    for column in columns:
        if column.role == SPACER:
            continue
        values = {str(line.cells[column.position].value) for line in records if column.position in line.cells}
        flag = column.position not in taken and column.kind == V_TEXT and 1 <= len(values) <= _FLAG_VALUES and all(len(v) <= _FLAG_LENGTH for v in values)
        if flag and run and values & seen and len(values | seen) <= _FLAG_VALUES:
            run.append(column)
            seen |= values
        else:
            close()
            run, seen = ([column], set(values)) if flag else ([], set())
    close()
    return axes


def _axes(columns: list[Column], header_grain: dict[int, str | None], sequence_numbers: dict[int, float], records: list[Line]) -> tuple[Axis, ...]:
    """Peer columns: 3+ number columns of one grain in a run named by periods (a month with nothing in it yet is still a month), or a sequence 1, 2, 3; else 3+ number columns of one format under one group label, or beside a column named total; else text columns marking the same few values. A member that prints a total of the others is no peer."""
    axes: list[Axis] = []
    taken: set[int] = set()
    numeric = [c for c in columns if c.kind == V_NUMBER]
    runs: list[list[Column]] = []
    for column in columns:
        if column.kind not in (V_NUMBER, V_BLANK) or not header_grain.get(column.position):
            continue
        if runs and all(c.role == SPACER or c.position in {x.position for x in runs[-1]} or not c.filled for c in columns[runs[-1][-1].position + 1:column.position]):
            runs[-1].append(column)
        else:
            runs.append([column])
    for run in runs:
        grains = Counter(header_grain[c.position] for c in run)
        for grain in sorted(grains, key=lambda g: min(c.position for c in run if header_grain[c.position] == g)):
            members = _true_members([c for c in run if header_grain[c.position] == grain], records)
            if len(members) >= 3:
                shared = {tuple(c.path[:-1]) for c in members}
                axes.append(Axis(AXIS_PERIOD, " ".join(shared.pop()) if len(shared) == 1 else "", tuple(c.position for c in members)))
                taken.update(c.position for c in members)
    run: list[Column] = []
    for column in numeric:
        value = sequence_numbers.get(column.position)
        if value is not None and column.position not in taken and (not run or value == sequence_numbers[run[-1].position] + 1):
            run.append(column)
        else:
            if len(run) >= 3:
                axes.append(Axis(AXIS_SEQUENCE, "", tuple(c.position for c in run)))
                taken.update(c.position for c in run)
            run = [column] if value is not None and column.position not in taken else []
    if len(run) >= 3:
        axes.append(Axis(AXIS_SEQUENCE, "", tuple(c.position for c in run)))
        taken.update(c.position for c in run)
    groups: dict[str, list[Column]] = {}
    for column in numeric:
        if len(column.path) >= 2 and column.position not in taken:
            groups.setdefault(column.path[0].lower(), []).append(column)
    for key in sorted(groups, key=lambda k: groups[k][0].position):
        members = _true_members(groups[key], records)
        if len(members) >= 3 and len({_format_family(c) for c in members}) == 1:
            axes.append(Axis(AXIS_GROUP, members[0].path[0], tuple(c.position for c in members)))
            taken.update(c.position for c in members)
    totals = [c for c in numeric if c.position not in taken and any(_TOTAL_MEMBER.search(part) for part in c.path)]
    if totals:
        for column in totals:
            column.role = ROW_TOTAL
        beside = [c for c in numeric if c.position not in taken and c.path and c.position < totals[-1].position and c.role != ROW_TOTAL]
        formats = Counter(_format_family(c) for c in beside)
        if formats:
            usual = min(formats, key=lambda f: (-formats[f], f))
            members = [c for c in beside if _format_family(c) == usual]
            if len(members) >= 3:
                axes.append(Axis(AXIS_TOTALLED, "", tuple(c.position for c in members)))
                taken.update(c.position for c in members)
    flags = _flag_axes(columns, records, taken)
    axes.extend(flags)
    taken.update(p for axis in flags for p in axis.positions)
    for column in columns:
        if column.position in taken and column.role != ROW_TOTAL:
            column.role = PEER
    return tuple(sorted(axes, key=lambda a: a.positions[0]))


def _as_of_in(text: str) -> Period | None:
    for m in _DATE_IN_TEXT.finditer(text):
        period = parse_period(m.group(0))
        if period is not None and period.start is not None:
            return period
    return None


def _context(grid: Grid, tab: str, title_rows: list[int]) -> Context:
    """Tab name, title lines, as-of date, unit: a date or unit of a nearer line wins, then the tab's name gives the date. A cell that reads the clock says when the file was last recalculated, not what the table is as of: it is no context."""
    titles = []
    as_of, as_of_address, unit, unit_address = None, "", "", ""
    for r in title_rows:
        for cell in grid.rows[r].values():
            if cell.shape == FX_VOLATILE:
                continue
            text = _label_of(cell)
            titles.append((cell.address, text))
            if cell.kind in (V_DATE, V_PERIOD) and cell.period is not None and cell.period.start is not None:
                as_of, as_of_address = cell.period, cell.address
            elif cell.kind == V_TEXT:
                if (found := _as_of_in(text)) is not None:
                    as_of, as_of_address = found, cell.address
                if m := _UNIT_IN_TITLE.search(text):
                    unit, unit_address = m.group(0), cell.address
    if as_of is None:
        as_of = parse_period(tab) or _as_of_in(tab)
        if as_of is not None and as_of.start is None:
            as_of = None
    return Context(tab=tab, titles=tuple(titles), as_of=as_of, as_of_address=as_of_address, unit=unit, unit_address=unit_address)


def _sparse_label_columns(columns: list[Column], records: list[Line]) -> list[int]:
    """Positions of label columns filled only now and then, the blanks under each label being the same thing: among the first three columns with no full column to their left, label cells on under half the lines, the first record labelled, own cells mostly apart (a solid block ending early is no sparse label), and no last run far past the runs before it (lines after a list's last value are not of that value)."""
    out: list[int] = []
    n = len(records)
    if n < 3:
        return out
    for column in columns[:3]:
        cells = [line.cells.get(column.position) for line in records]
        own = [at for at, cell in enumerate(cells) if cell is not None]
        if len(own) * 2 >= n:
            break
        if len(own) < 2 or cells[0] is None or not all(cells[at].kind in (V_TEXT, V_DATE, V_PERIOD) for at in own):
            continue
        gaps = [b - a for a, b in zip(own, own[1:])]
        tail = n - own[-1]
        if sum(1 for g in gaps if g == 1) > _ADJACENT_MOST * len(gaps) or tail * 2 > n or tail > 2 * max(gaps):
            continue
        out.append(column.position)
    return out


def _fill_down(position: int, records: list[Line], printed: list[Line]) -> int:
    """A sparse label fills down inside its section and up to the next printed line; the filled cell is the source cell, so it still traces to where it was written. Where labelled lines hold figures, a line with none is a continuation of the line above and takes no label. Returns how many lines were filled."""
    labelled = [line for line in records if position in line.cells]
    figures = sum(1 for line in labelled if any(cell.kind == V_NUMBER for cell in line.cells.values()))
    need_figure = figures * 2 >= len(labelled)
    held = None
    held_section = None
    rows_of_printed = sorted(line.row for line in printed)
    pointer = 0
    filled = 0
    for line in records:
        while pointer < len(rows_of_printed) and rows_of_printed[pointer] < line.row:
            held = None
            pointer += 1
        if line.section != held_section:
            held, held_section = None, line.section
        cell = line.cells.get(position)
        if cell is not None:
            held = cell
        elif held is not None and (not need_figure or any(c.kind == V_NUMBER for c in line.cells.values())):
            line.cells[position] = held
            line.signals = (*line.signals, S_FILLED_DOWN)
            filled += 1
    return filled


def _printed_kinds(printed: list[Line], last_record_row: int) -> None:
    """Totals from subtotals: the word says, else the last printed line under every record, or one whose formula reads other printed lines, is the total."""
    rows = {line.row for line in printed}
    for at, line in enumerate(printed):
        text = line.label.lower()
        reads_printed = line.scope is not None and any(line.scope[0] <= r <= line.scope[1] for r in rows if r != line.row)
        if re.search(r"sub-?\s*total", text):
            line.role = SUBTOTAL
        elif re.search(r"grand\s*total", text) or reads_printed or (at == len(printed) - 1 and line.row > last_record_row):
            line.role = TOTAL
        else:
            line.role = SUBTOTAL


def _stray_key_lines(columns: list[Column], records: list[Line]) -> set[int]:
    """Lines whose key cell is text where the key column otherwise holds dates, beside figures: statistics printed under the data, whatever their word. The first filled column is the key."""
    for column in columns:
        cells = [line.cells[column.position] for line in records if column.position in line.cells]
        if not cells:
            continue
        dated = sum(1 for cell in cells if cell.kind in (V_DATE, V_PERIOD))
        if dated < 5 or dated < _STRAY_KEY_DATED * len(cells):
            return set()
        return {line.row for line in records if (cell := line.cells.get(column.position)) is not None and cell.kind == V_TEXT and any(other.kind == V_NUMBER for other in line.cells.values())}
    return set()


def _text_of(cells: list[Cell]) -> str:
    return " ".join(dict.fromkeys(_label_of(cell) for cell in cells))


def _style_of(grid: Grid, row: int) -> tuple[bool, bool, bool]:
    counts = grid.tab.row_styles.get(row)
    if not counts or not counts[0]:
        return (False, False, False)
    return (counts[1] * 2 >= counts[0], counts[2] * 2 >= counts[0], counts[3] * 2 >= counts[0])


def _first_band(grid: Grid, region: _Region, block: _Block, roles: dict) -> int | None:
    """When a table has bands, the lone title directly over its header, styled like them, is its first band: the first section sits above the header, where it reads as a title."""
    bands = [r for r in region.body if roles[r][0] == BAND]
    if not bands or not region.titles or not region.header_rows:
        return None
    title = region.titles[-1]
    cells = [cell for c, cell in grid.rows[title].items() if block.holds(c)]
    if len(cells) != 1 or len(grid.rows[title]) != 1 or cells[0].kind != V_TEXT or region.header_rows[0] - title > 3 or len(str(cells[0].value)) > 60:
        return None
    return title if _style_of(grid, title) == _style_of(grid, bands[0]) else None


def _repeat_sections(grid: Grid, region: _Region, block: _Block, header_rows: list[int]) -> tuple[dict[int, str], list[int]]:
    """What a repeated header block says that the table's header does not. Returns {first row of the block: the words that are that block's section} and the table's own header lines that title its first block."""
    names: dict[int, set[str]] = {}
    for h in header_rows:
        for c, cell in grid.rows[h].items():
            names.setdefault(c, set()).add(_label_of(cell).lower())
    def own(c: int) -> bool:
        return block.holds(c) and c not in block.shared

    sections: dict[int, str] = {}
    for rows in region.repeats:
        differing = [cell for r in rows for c, cell in grid.rows[r].items() if own(c) and _label_of(cell).lower() not in names.get(c, ())]
        if differing:
            sections[rows[0]] = _text_of(differing)
    return sections, _title_lines(grid, region, own, header_rows)


def _keys_over_keys(grid: Grid, region: _Region, block: _Block, roles: dict) -> set[int]:
    """In a side-by-side list of keys, few of them with a figure beside them, those few head the keys under them: a person and an extension over the accounts they cover. Bands belong to a block: each list has its own."""
    span = [c for c in block.span() if c not in block.shared]
    if not span or len(span) > 3:
        return set()
    key = span[0]
    with_figure: set[int] = set()
    alone = 0
    for r in region.body:
        cells = grid.rows[r]
        if roles[r][0] != RECORD or key not in cells or cells[key].kind != V_TEXT:
            continue
        if any(cells[c].kind == V_NUMBER for c in span[1:] if c in cells):
            with_figure.add(r)
        elif not any(c in cells for c in span[1:]):
            alone += 1
    total = len(with_figure) + alone
    return with_figure if len(with_figure) >= 2 and total and alone >= _KEYS_ALONE * total else set()


def _table_of(grid: Grid, tab: str, index: int, block_number: int, region: _Region, block: _Block, roles: dict, tab_titles: list[int]) -> Table | None:
    """One table: the region's lines cut to one block's columns."""
    holds = block.holds
    header_rows = [r for r in region.header_rows if any(holds(c) for c in grid.rows[r])]
    columns_used = {c for r in header_rows + region.body for c in grid.rows[r] if holds(c)}
    if not columns_used:
        return None
    repeat_sections, title_rows = _repeat_sections(grid, region, block, header_rows)
    name_rows = [r for r in header_rows if r not in title_rows]
    span = block.span()
    paths = _header_paths(grid, name_rows, span)
    ordered = sorted(c for c in span if c in columns_used or paths[c][0])
    names = _unique_names(paths, ordered)
    hidden_columns = grid.tab.hidden_columns
    columns = [Column(position=i, col=c, letter=column_letter(c), name=names[i], path=tuple(paths[c][0]), header_cells=tuple(paths[c][1]), hidden=c in hidden_columns) for i, c in enumerate(ordered)]
    position = {c: i for i, c in enumerate(ordered)}
    tall: dict[tuple[int, int], Cell] = {}
    for r1, c1, r2, c2 in grid.tab.merges:
        # A tall merge names the lines it covers. A number under one is one figure: repeating it would add it once per line.
        if r2 > r1 and holds(c1) and (source := grid.rows.get(r1, {}).get(c1)) is not None and source.kind != V_NUMBER and r2 - r1 <= 4096:
            for r in range(r1 + 1, r2 + 1):
                tall[(r, c1)] = source
    tall_rows: dict[int, list[tuple[int, Cell]]] = {}
    for (rr, c), source in tall.items():
        tall_rows.setdefault(rr, []).append((c, source))
    hidden_rows = grid.tab.hidden_rows
    records: list[Line] = []
    printed: list[Line] = []
    bands: list[tuple[int, str]] = []
    section: tuple[str, ...] = ()
    previous_band = False
    if title_rows:
        # The first block's title is its section; the repeats name the blocks after it.
        cells = [cell for r in title_rows for c, cell in grid.rows[r].items() if holds(c) and c not in block.shared]
        section = (_text_of(cells),)
        bands.append((title_rows[0], section[0]))
    elif (first_band := _first_band(grid, region, block, roles)) is not None:
        section = (_text_of([cell for c, cell in grid.rows[first_band].items() if holds(c)]),)
        bands.append((first_band, section[0]))
    repeat_rows = {r for rows in region.repeats for r in rows}
    key_heads = _keys_over_keys(grid, region, block, roles) if block_number else set()
    for r in sorted({*region.body, *repeat_rows}):
        if r in repeat_rows:
            # A repeated block's differing words open its section, unless a band directly above it already did: its words are then only a column's other name.
            if r in repeat_sections and not previous_band:
                section = (repeat_sections[r],)
                bands.append((r, section[0]))
            previous_band = False
            continue
        role, signals = roles[r]
        cut = {position[c]: cell for c, cell in grid.rows[r].items() if holds(c)}
        over = tall_rows.get(r, ())
        if not cut and not any(holds(c) for c, _ in over):
            continue
        for c, source in over:
            if c in position and position[c] not in cut:
                cut[position[c]] = source
        if role == TOTAL and block_number:
            # In a block of side-by-side tables the line's role follows the block's own cells.
            own = list(cut.values())
            if not is_total_mark(own) and not any(cell.shape == FX_AGG_COLUMN for cell in own):
                role, signals = RECORD, (S_TOTAL_OTHER_BLOCK,)
        elif role == RECORD and block_number and is_total_mark(list(cut.values())) and any(cell.kind == V_NUMBER for cell in cut.values()):
            role, signals = TOTAL, (S_TOTAL_WORD,)
        hidden = r in hidden_rows
        if r in key_heads:
            text = _text_of([cell for _, cell in sorted(cut.items()) if cell.kind == V_TEXT and cell.col not in block.shared])
            section = (text,)
            bands.append((r, text))
            previous_band = False
            continue
        if role == BAND:
            text = " ".join(_label_of(cell) for _, cell in sorted(cut.items()))
            section = section + (text,) if previous_band else (text,)
            bands.append((r, text))
            previous_band = True
            continue
        previous_band = False
        if role == NOTE:
            continue
        line = Line(tab=tab, row=r, role=role, cells=cut, signals=signals, hidden=hidden, section=section)
        line.label = " ".join(_label_of(cell) for _, cell in sorted(cut.items()) if cell.kind in (V_TEXT, V_DATE, V_PERIOD))
        if role in PRINTED_ROLES:
            spans = [grid.shapes[(r, cell.col)].span for cell in cut.values() if (r, cell.col) in grid.shapes and grid.shapes[(r, cell.col)].kind == FX_AGG_COLUMN and grid.shapes[(r, cell.col)].span]
            if spans:
                line.scope = (min(s[0] for s in spans), max(s[1] for s in spans))
            printed.append(line)
            if section and re.search(rf"\b{re.escape(section[-1])}\b", line.label, re.I):
                section = section[:-1]
        else:
            records.append(line)
    stray_keys = _stray_key_lines(columns, records)
    if stray_keys:
        moved = [line for line in records if line.row in stray_keys]
        records = [line for line in records if line.row not in stray_keys]
        for line in moved:
            line.role, line.signals = TOTAL, (*line.signals, S_STRAY_KEY)
        printed = sorted(printed + moved, key=lambda line: line.row)
    fill = [at for at in _sparse_label_columns(columns, records) if _fill_down(at, records, printed)]
    strays: list[Stray] = []
    for column in columns:
        cells = [line.cells[column.position] for line in records if column.position in line.cells and line.cells[column.position].row == line.row]
        _type_column(column, cells, strays, tab)
        if column.position in fill:
            column.role = SECTION_LABEL
        column.unit = _unit_of(column)
    strays.sort(key=lambda s: (s.row, s.position))
    header_grain: dict[int, str | None] = {}
    sequence: dict[int, float] = {}
    for column in columns:
        cell = grid.rows[name_rows[-1]].get(column.col) if name_rows else None
        if cell is not None:
            period = cell.period or (parse_period(str(cell.value)) if cell.kind == V_TEXT else None)
            header_grain[column.position] = period.kind if period is not None else P_YEAR if cell.kind == V_NUMBER and year_number(cell.value) else None
            if cell.kind == V_NUMBER and float(cell.value).is_integer():
                sequence[column.position] = cell.value
    axes = _axes(columns, header_grain, sequence, records)
    last_record = records[-1].row if records else 0
    _printed_kinds(printed, last_record)
    if not records and not printed and not header_rows:
        return None
    rows_seen = [line.row for line in records] + [line.row for line in printed]
    title_lines = list(dict.fromkeys(tab_titles + region.titles))
    context = _context(grid, tab, title_lines)
    near_titles = [text for address, text in context.titles if region.titles and int(_DIGITS.search(address).group()) == region.titles[-1]]
    name = near_titles[0] if near_titles and len(near_titles[0]) <= 80 else tab
    repeated = tuple(r for rows in region.repeats for r in rows)
    return Table(tab=tab, index=index, name=name, columns=columns, lines=records, printed=printed, header_rows=tuple(header_rows),
                 first_row=min(rows_seen) if rows_seen else (header_rows[-1] if header_rows else 0), last_row=max(rows_seen) if rows_seen else (header_rows[-1] if header_rows else 0),
                 context=context, bands=tuple(sorted(bands)), axes=axes, strays=tuple(strays), repeated_header_rows=repeated, block=block_number)


def _notes_of(grid: Grid, tab: str, rows: list[int]) -> list[Note]:
    """Label: value lines outside tables: "Label: value" in one cell, "Label:" and the next cell, or a two-cell line of a short label and a value."""
    notes = []
    for r in rows:
        cells = list(grid.rows[r].values())
        for at, cell in enumerate(cells):
            if cell.kind != V_TEXT:
                continue
            text = str(cell.value)
            label, colon, rest = text.partition(":")
            if colon and 2 <= len(label) <= 40 and rest.strip():
                value = Cell(cell.row, cell.col, cell.raw, V_TEXT, rest.strip())
                notes.append(Note(tab, r, label.strip(), value, cell.address))
            elif colon and not rest.strip() and 2 <= len(label) <= 40 and at + 1 < len(cells):
                notes.append(Note(tab, r, label.strip(), cells[at + 1], cell.address))
        if len(cells) == 2 and cells[0].kind == V_TEXT and ":" not in str(cells[0].value) and len(str(cells[0].value)) <= 40 and cells[1].kind in (V_NUMBER, V_DATE, V_PERIOD):
            notes.append(Note(tab, r, str(cells[0].value), cells[1], cells[0].address))
    return notes


def compile_tab(tab: TabPayload) -> TabModel:
    """One tab's model."""
    if tab.sheet_type == SHEET_CHART:
        return TabModel(tab.name, tab.sheet_type, tab.hidden, [], [], [])
    grid = Grid(tab)
    roles = read_roles(grid)
    regions = _split_at_closing_totals(grid, _regions(grid, roles), roles)
    tab_titles = regions[0].titles if regions else []
    tables: list[Table] = []
    for region in regions:
        blocks = _column_groups(grid, region, roles)
        for number, block in enumerate(blocks, 1 if len(blocks) > 1 else 0):
            table = _table_of(grid, tab.name, len(tables), number, region, block, roles, tab_titles)
            if table is not None:
                tables.append(table)
    repeated = {r for region in regions for rows in region.repeats for r in rows}
    lines: list[Line] = []
    for r in range(1, grid.last_row + 1):
        cells = grid.rows.get(r)
        if cells is None:
            lines.append(Line(tab.name, r, BLANK, hidden=r in tab.hidden_rows))
        elif r in grid.rule_lines:
            lines.append(Line(tab.name, r, BLANK, dict(cells), (S_RULE_LINE,), hidden=r in tab.hidden_rows))
        else:
            role, signals = roles[r]
            signals = (*signals, S_REPEATED_HEADER) if r in repeated else signals
            lines.append(Line(tab.name, r, role, dict(cells), signals, hidden=r in tab.hidden_rows))
    note_rows = [r for r in grid.filled if roles[r][0] in (TITLE, NOTE)]
    # A tab line takes the role its table gave it, so a side-by-side block's own reading does not hide; rows in no table keep theirs.
    by_row: dict[int, Line] = {}
    for table in tables:
        for printed in table.printed:
            by_row.setdefault(printed.row, printed)
    for line in lines:
        held = by_row.get(line.row)
        if line.role in (SUBTOTAL, TOTAL) and held is None:
            line.role = TOTAL
        elif held is not None and (line.role in (SUBTOTAL, TOTAL) or S_STRAY_KEY in held.signals):
            line.role = held.role
    return TabModel(tab.name, tab.sheet_type, tab.hidden, lines, tables, _notes_of(grid, tab.name, note_rows), tuple(sorted(tab.hidden_columns)), tab.style_read)
