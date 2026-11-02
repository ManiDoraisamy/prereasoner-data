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
from engine.sheet.line_roles import read_roles
from engine.sheet.model import Axis, Cell, Column, Context, Line, Note, Stray, TabModel, Table
from engine.sheet.names import (AXIS_GROUP, AXIS_PERIOD, AXIS_SEQUENCE, BAND, BLANK, DATE, DERIVED, F_CURRENCY, F_PERCENT, FX_AGG_COLUMN, FX_AGG_ROW, FX_ROW_ARITH, HEADER, KEY, LABEL, MEASURE, MIXED, NOTE,
                                PEER, PERIOD, PRINTED_ROLES, RECORD, ROW_TOTAL, S_TOTAL_WORD, SECTION_LABEL, SHEET_CHART, SPACER, STRAY_DATE, STRAY_ERROR, STRAY_LABEL, STRAY_MARKER, STRAY_NOTE,
                                STRAY_NUMBER_WITH_WORD, STRAY_REPEATED_HEADER, STRAY_TOTAL, SUBTOTAL, TITLE, TOTAL, V_BLANK, V_BOOL, V_DATE, V_ERROR, V_MARKER, V_NUMBER, V_PERIOD, V_TEXT)
from engine.sheet.payload import TabPayload
from engine.sheet.values import Period, number_with_word, parse_period, year_number

_UNIT_IN_TITLE = re.compile(r"\$\s?000s?|\$\s?MM\b|\(000'?s?\)|\bin\s+(?:thousands|millions|billions)\b|\bin\s+(?:[A-Z]{2,}\w*|US\$|\$\w*)")
_UNIT_IN_NAME = re.compile(r"\(([^()]{1,12})\)\s*$")
_DATE_IN_TEXT = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{1,2}-[A-Za-z]{3,9}-\d{2,4}\b|\b[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\b|\b[A-Za-z]{3,9}\.?[\s'-]+\d{4}\b|\b[A-Za-z]{3,9}-\d{2}\b|\b\d{4}-\d{2}-\d{2}\b")


@dataclass(slots=True)
class _Region:
    """A header block and the filled lines under it, down to the next table."""
    header_rows: list[int] = field(default_factory=list)
    body: list[int] = field(default_factory=list)
    repeats: list[int] = field(default_factory=list)
    titles: list[int] = field(default_factory=list)


def _label_of(cell: Cell) -> str:
    """A cell as the words of a name."""
    if cell.kind == V_NUMBER:
        return f"{cell.value:g}" if isinstance(cell.value, float) else str(cell.value)
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


def _regions(grid: Grid, roles: dict[int, tuple[str, tuple[str, ...]]]) -> list[_Region]:
    """Walk the filled lines: a new header block closes the table above it, unless it repeats that table's own header; lines with no header above them form a table of their own."""
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
            if current is not None and current.header_rows and _repeats_block(grid, block, _names_of(grid, current.header_rows)):
                current.repeats.extend(block)
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


def _column_groups(grid: Grid, region: _Region, roles: dict[int, tuple[str, tuple[str, ...]]]) -> list[tuple[int, int]]:
    """Side-by-side blocks: columns cut at blank gutters, each block with its own key column and something beside it; or names repeating in order (A B | A B). One range when the region is one table."""
    rows = region.header_rows + region.body
    used: set[int] = set()
    for r in rows:
        used.update(grid.rows[r])
    if not used:
        return []
    columns = sorted(used)
    whole = [(columns[0], columns[-1])]
    if not region.header_rows:
        return whole
    groups: list[list[int]] = [[columns[0]]]
    for c in columns[1:]:
        if c == groups[-1][-1] + 1:
            groups[-1].append(c)
        else:
            groups.append([c])
    record_rows = [r for r in region.body if roles[r][0] == RECORD]

    def has_own_key(group: list[int]) -> bool:
        if len(group) < 2:
            return False
        lines = [r for r in record_rows if any(c in grid.rows[r] for c in group)]
        if len(lines) < 2:
            return False
        lead = sum(1 for r in lines if (cell := grid.rows[r].get(group[0])) is not None and cell.kind in (V_TEXT, V_DATE, V_PERIOD))
        named = sum(1 for c in group if any(c in grid.rows[h] for h in region.header_rows))
        return lead * 10 >= len(lines) * 6 and named >= 2

    merged: list[list[int]] = []
    for group in groups:
        if merged and not has_own_key(group):
            merged[-1].extend(group)
        else:
            merged.append(list(group))
    if len(merged) >= 2 and has_own_key(merged[0]):
        return [(g[0], g[-1]) for g in merged]
    names = _names_of(grid, region.header_rows)
    named = sorted(names)
    texts = [names[c] for c in named]
    for size in range(2, len(named) // 2 + 1):
        if len(named) % size == 0 and all(texts[k:k + size] == texts[:size] for k in range(0, len(named), size)):
            return [(named[k], named[k + size - 1]) for k in range(0, len(named), size)]
    return whole


def _header_paths(grid: Grid, header_rows: list[int], first: int, last: int) -> dict[int, tuple[list[str], list[str]]]:
    """Each column's name path and the addresses it came from: a merge gives its text to the columns it spans; on an upper line of a block, a label carries right over the columns the line below names, until the next label."""
    paths: dict[int, tuple[list[str], list[str]]] = {c: ([], []) for c in range(first, last + 1)}
    for at, r in enumerate(header_rows):
        line = grid.rows[r]
        lower = set()
        for below in header_rows[at + 1:]:
            lower.update(grid.rows[below])
        held: Cell | None = None
        upper_is_sparse = bool(lower) and len([c for c in line if first <= c <= last]) < len([c for c in lower if first <= c <= last])
        for c in range(first, last + 1):
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


def _type_column(column: Column, cells: list[Cell], strays: list[Stray], tab: str, row_of) -> None:
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


def _axes(columns: list[Column], header_period: dict[int, bool], sequence_numbers: dict[int, float]) -> tuple[Axis, ...]:
    """Peer columns: a run of 3+ number columns named by periods, or by a sequence 1, 2, 3; else 3+ number columns under one group label."""
    axes: list[Axis] = []
    taken: set[int] = set()
    numeric = [c for c in columns if c.kind == V_NUMBER]
    runs: list[list[Column]] = []
    for column in numeric:
        if not header_period.get(column.position):
            continue
        if runs and all(c.role == SPACER or c.position in {x.position for x in runs[-1]} or not c.filled for c in columns[runs[-1][-1].position + 1:column.position]):
            runs[-1].append(column)
        else:
            runs.append([column])
    for run in runs:
        if len(run) >= 3:
            shared = {tuple(c.path[:-1]) for c in run}
            axes.append(Axis(AXIS_PERIOD, " ".join(shared.pop()) if len(shared) == 1 else "", tuple(c.position for c in run)))
            taken.update(c.position for c in run)
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
        members = groups[key]
        if len(members) >= 3:
            axes.append(Axis(AXIS_GROUP, members[0].path[0], tuple(c.position for c in members)))
            taken.update(c.position for c in members)
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
    """Tab name, title lines, as-of date, unit: a date or unit of a nearer line wins, then the tab's name gives the date."""
    titles = []
    as_of, as_of_address, unit, unit_address = None, "", "", ""
    for r in title_rows:
        for cell in grid.rows[r].values():
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


def _sparse_label_columns(columns: list[Column], records: list[dict[int, Cell]]) -> list[int]:
    """Positions of label columns filled only now and then, the blanks under each label being the same thing: among the first three columns, text, under half filled, the first record filled."""
    out = []
    if len(records) < 3:
        return out
    for column in columns[:3]:
        cells = [line.get(column.position) for line in records]
        present = [cell for cell in cells if cell is not None]
        if len(present) >= 2 and len(present) * 2 < len(cells) and cells[0] is not None and all(cell.kind == V_TEXT and len(str(cell.value)) <= 60 for cell in present):
            out.append(column.position)
    return out


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


def _table_of(grid: Grid, tab: str, index: int, block: int, region: _Region, first: int, last: int, roles: dict, tab_titles: list[int]) -> Table | None:
    """One table: the region's lines cut to one column range."""
    header_rows = [r for r in region.header_rows if any(first <= c <= last for c in grid.rows[r])]
    columns_used = {c for r in header_rows + region.body for c in grid.rows[r] if first <= c <= last}
    if not columns_used:
        return None
    paths = _header_paths(grid, header_rows, first, last)
    ordered = sorted(c for c in range(first, last + 1) if c in columns_used or paths[c][0])
    names = _unique_names(paths, ordered)
    hidden_columns = grid.tab.hidden_columns
    columns = [Column(position=i, col=c, letter=column_letter(c), name=names[i], path=tuple(paths[c][0]), header_cells=tuple(paths[c][1]), hidden=c in hidden_columns) for i, c in enumerate(ordered)]
    position = {c: i for i, c in enumerate(ordered)}
    tall: dict[tuple[int, int], Cell] = {}
    for r1, c1, r2, c2 in grid.tab.merges:
        if r2 > r1 and first <= c1 <= last and (source := grid.rows.get(r1, {}).get(c1)) is not None and r2 - r1 <= 4096:
            for r in range(r1 + 1, r2 + 1):
                tall[(r, c1)] = source
    hidden_rows = grid.tab.hidden_rows
    records: list[Line] = []
    printed: list[Line] = []
    bands: list[tuple[int, str]] = []
    section: tuple[str, ...] = ()
    previous_band = False
    body_rows = sorted(region.body)
    for r in body_rows:
        role, signals = roles[r]
        cut = {position[c]: cell for c, cell in grid.rows[r].items() if first <= c <= last}
        if not cut and not any(first <= c <= last for rr, c in tall if rr == r):
            continue
        for (rr, c), source in ((key, value) for key, value in tall.items() if key[0] == r):
            if c in position and position[c] not in cut:
                cut[position[c]] = source
        if role == TOTAL and block:
            # In a block of side-by-side tables the line's role follows the block's own cells.
            own = [cell for cell in cut.values()]
            if not is_total_mark(own) and not any(cell.shape == FX_AGG_COLUMN for cell in own):
                role = RECORD
        elif role == RECORD and block and is_total_mark(list(cut.values())) and any(cell.kind == V_NUMBER for cell in cut.values()):
            role, signals = TOTAL, (S_TOTAL_WORD,)
        hidden = r in hidden_rows
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
            if section and section[-1].lower() in line.label.lower():
                section = section[:-1]
        else:
            records.append(line)
    # A sparse label column fills down, inside its section: the filled cell is the source cell, so it still traces to where it was written.
    fill = _sparse_label_columns(columns, [line.cells for line in records])
    for at in fill:
        held = None
        held_section = None
        rows_of_printed = sorted(line.row for line in printed)
        pointer = 0
        for line in records:
            while pointer < len(rows_of_printed) and rows_of_printed[pointer] < line.row:
                held = None
                pointer += 1
            if line.section != held_section:
                held, held_section = None, line.section
            cell = line.cells.get(at)
            if cell is not None:
                held = cell
            elif held is not None:
                line.cells[at] = held
    strays: list[Stray] = []
    for column in columns:
        cells = [line.cells[column.position] for line in records if column.position in line.cells and line.cells[column.position].row == line.row]
        _type_column(column, cells, strays, tab, None)
        if column.position in fill:
            column.role = SECTION_LABEL
        column.unit = _unit_of(column)
    strays.sort(key=lambda s: (s.row, s.position))
    header_period = {}
    sequence: dict[int, float] = {}
    for column in columns:
        cell = None
        if header_rows:
            cell = grid.rows[header_rows[-1]].get(column.col)
        if cell is not None:
            header_period[column.position] = cell.period is not None or (cell.kind == V_NUMBER and year_number(cell.value)) or (cell.kind == V_TEXT and parse_period(str(cell.value)) is not None)
            if cell.kind == V_NUMBER and float(cell.value).is_integer():
                sequence[column.position] = cell.value
    axes = _axes(columns, header_period, sequence)
    last_record = records[-1].row if records else 0
    _printed_kinds(printed, last_record)
    if not records and not printed and not header_rows:
        return None
    rows_seen = [line.row for line in records] + [line.row for line in printed]
    title_rows = list(dict.fromkeys(tab_titles + region.titles))
    context = _context(grid, tab, title_rows)
    near_titles = [text for address, text in context.titles if any(address.endswith(str(r)) for r in region.titles[-1:])]
    name = near_titles[0] if near_titles and len(near_titles[0]) <= 80 else tab
    return Table(tab=tab, index=index, name=name, columns=columns, lines=records, printed=printed, header_rows=tuple(header_rows),
                 first_row=min(rows_seen) if rows_seen else (header_rows[-1] if header_rows else 0), last_row=max(rows_seen) if rows_seen else (header_rows[-1] if header_rows else 0),
                 context=context, bands=tuple(bands), axes=axes, strays=tuple(strays), repeated_header_rows=tuple(region.repeats), block=block)


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
    lines: list[Line] = []
    for r in range(1, grid.last_row + 1):
        cells = grid.rows.get(r)
        if cells is None:
            lines.append(Line(tab.name, r, BLANK, hidden=r in tab.hidden_rows))
        else:
            role, signals = roles[r]
            lines.append(Line(tab.name, r, role, dict(cells), signals, hidden=r in tab.hidden_rows))
    regions = _regions(grid, roles)
    tab_titles = regions[0].titles if regions else []
    tables: list[Table] = []
    for region in regions:
        groups = _column_groups(grid, region, roles)
        for block, (first, last) in enumerate(groups, 1 if len(groups) > 1 else 0):
            table = _table_of(grid, tab.name, len(tables), block, region, first, last, roles, tab_titles)
            if table is not None:
                tables.append(table)
    note_rows = [r for r in grid.filled if roles[r][0] in (TITLE, NOTE)]
    for line in lines:
        if line.role in (SUBTOTAL, TOTAL):
            line.role = TOTAL
    # A tab line takes the role its table gave it, so a side-by-side block's own reading does not hide; rows in no table keep theirs.
    by_row = {}
    for table in tables:
        for printed in table.printed:
            by_row.setdefault(printed.row, printed.role)
    for line in lines:
        if line.role == TOTAL and line.row in by_row:
            line.role = by_row[line.row]
    return TabModel(tab.name, tab.sheet_type, tab.hidden, lines, tables, _notes_of(grid, tab.name, note_rows), tuple(sorted(tab.hidden_columns)))

