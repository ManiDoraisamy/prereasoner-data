"""Line facts: line_facts(grid) reads each filled line of a tab into named coordinates, in the signal order the evidence gives: values, formulas, style, merges. Only the total word is a word here: no domain's vocabulary."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from engine.sheet.grid import Grid
from engine.sheet.names import F_CURRENCY, FX_AGG_COLUMN, FX_AGG_ROW, FX_COLUMN_ARITH, FX_LINK_COLUMN, V_DATE, V_ERROR, V_MARKER, V_NUMBER, V_PERIOD, V_TEXT
from engine.sheet.values import year_number

TOTAL_WORD = re.compile(r"\b(?:grand\s*total|sub-?\s*total|totals?)\b", re.I)
_LABEL_VALUE = re.compile(r"^[^:]{2,40}:(\s|$)")
_STRONG_TOTAL = re.compile(r"(?:^|\s)(?:grand\s*total|sub-?\s*total)\b", re.I)
_GRAND_TOTAL = re.compile(r"\bgrand\s*total", re.I)
_LEADING_TOTAL = re.compile(r"^\W*(?:grand\s*total|sub-?\s*total|totals?)\b", re.I)
_STYLE_WINDOW = 8
_BELOW_WINDOW = 14


@dataclass(slots=True)
class LineFacts:
    """What one filled line shows: every field is a named coordinate of the line."""
    row: int
    filled: int
    text: int
    number: int
    date: int
    period: int
    error: int
    marker: int
    first_col: int
    last_col: int
    wordy: int                  # cells of six words or more
    longest: int                # characters in its longest text cell
    total_word: bool            # the word sits on the line as a mark, not inside a longer name
    agg_column: int             # cells adding their own column
    agg_row: int                # cells adding their own row
    computed: int               # cells computed from their own column, not as a running series
    formula_major: bool         # aggregates make most of its figures
    formula_line: bool          # computed cells make most of its figures
    arith_ties: int             # figures equal to the sum of the cells above them
    bold: float
    fill: float
    under: float
    bold_next: bool             # the mark is on most of its cells and on few of the lines after it
    fill_next: bool
    under_next: bool
    bold_prev: bool
    fill_prev: bool
    repeated: bool              # the same text in the same columns on another line
    real_figures: int           # numbers that are neither years nor a written index such as (1)
    sequence: bool              # 3+ numbers across, each one more than the last
    gap_above: bool
    gap_below: bool
    prev: int | None
    next: int | None
    wide_merge: int             # columns its widest merge spans
    label_value: bool           # a label: value line
    value_score: float          # how well its text names one-typed columns under it
    named: int                  # columns it names
    typed_below: float          # share of the lines under it, from the first typed line, that hold figures or dates
    overlap_below: float        # share of the columns filled under it that it covers
    over_figures: float         # share of its cells standing over a figure on the first line of figures under it
    word_col: int               # the column holding the total word, 0 when no cell holds it as a mark
    grand: bool                 # the word is a grand total
    word_ties: int              # figures equal to the sum of their column since the last line bearing the total word


def _written_index(cell) -> bool:
    """A column index written as text, as (1) or (2): a label, though it parses as a number."""
    return isinstance(cell.raw, str) and abs(cell.value) < 100 and float(cell.value).is_integer()


def _style_share(grid: Grid, row: int) -> tuple[float, float, float]:
    counts = grid.tab.row_styles.get(row)
    if not counts or not counts[0]:
        return (0.0, 0.0, 0.0)
    return (counts[1] / counts[0], counts[2] / counts[0], counts[3] / counts[0])


def is_total_mark(cells: list) -> bool:
    """The total word as a mark: the line's only text, or the end of its cell, or the start of a cell with no other text beside it. "Total Fina Elf" beside a book and a trader is a name."""
    texts = [c for c in cells if c.kind == V_TEXT]
    for cell in texts:
        text = str(cell.value)
        if not TOTAL_WORD.search(text):
            continue
        if len(texts) > 1 and (len(text) > 40 or len(text.split()) >= 6) and not _LEADING_TOTAL.match(text):
            continue        # prose beside a record's other texts ("... of the total."); a line's only text may be a long label
        if text.startswith("[") and text.endswith("]"):
            continue        # a list of words ("['maximum', 'total']") is no label
        # A total word ending a cell is the line's label ("East Total"); on a line filled like a record (5 texts or more)
        # it ends a name when other texts stand before it ("Motor Total", a seller beside the record's ids and make).
        ends = TOTAL_WORD.search(text.split()[-1]) and (len(texts) < 5 or cell is min(texts, key=lambda c: c.col))
        if len(texts) == 1 or ends or _STRONG_TOTAL.search(text):
            return True
        if text.lower().startswith("total") and not any(other is not cell and other.col < cell.col + 3 for other in texts):
            return True
    return False


def _arith_ties(grid: Grid) -> dict[int, int]:
    """For each line, how many of its figures equal the sum of two or more nonzero cells directly above them in their column, with no formula needed."""
    columns: dict[int, list[tuple[int, float]]] = {}
    for r in grid.filled:
        for c, cell in grid.rows[r].items():
            if cell.kind == V_NUMBER:
                columns.setdefault(c, []).append((r, cell.value))
    tied: Counter = Counter()
    for c in sorted(columns):
        prefix = {0: 0}
        total, nonzero = 0.0, 0
        for r, v in columns[c]:
            if v:
                want = round((total - v) * 100)
                for k in (want - 1, want, want + 1):
                    j = prefix.get(k)
                    if j is not None and nonzero - j >= 2:
                        tied[r] += 1
                        break
            total += v
            if v:
                nonzero += 1
            prefix[round(total * 100)] = nonzero
    return tied


def _word_ties(grid: Grid, worded: set[int]) -> dict[int, int]:
    """For each line bearing the total word, how many of its figures equal, to the cent and nonzero, the sum of their column on the lines since the last such line: a printed line has something to add up."""
    ties: dict[int, int] = {}
    running: dict[int, float] = {}
    for r in grid.filled:
        numbers = {c: cell.value for c, cell in grid.rows[r].items() if cell.kind == V_NUMBER}
        if r in worded:
            ties[r] = sum(1 for c, v in numbers.items() if v and c in running and abs(running[c] - v) < 0.01)
            running = {}
        else:
            for c, v in numbers.items():
                running[c] = running.get(c, 0.0) + v
    return ties


def _value_scores(grid: Grid, row: int, later: list[int]) -> tuple[float, int]:
    """How well this line names what sits under it: its text over columns the next lines fill with one kind of cell. A line of figures scores nothing."""
    below = later[:_BELOW_WINDOW]
    if not below:
        return (0.0, 0)
    score, named, numbers = 0.0, 0, 0
    for c, cell in grid.rows[row].items():
        if cell.kind in (V_NUMBER, V_ERROR):
            numbers += 1
            continue
        kinds: Counter = Counter()
        same_text = False
        present = 0
        for r in below:
            under = grid.rows[r].get(c)
            if under is None:
                continue
            present += 1
            kinds[V_NUMBER if under.kind in (V_NUMBER, V_ERROR) else under.kind if under.kind in (V_DATE, V_PERIOD) else V_TEXT] += 1
            same_text = same_text or (under.kind == V_TEXT and under.value == cell.value)
        if present < max(2, len(below) // 4):
            continue
        top_count = max(kinds.values())
        if top_count / present < 0.5:
            continue
        top = min(k for k, v in kinds.items() if v == top_count)
        weight = 1.0 if top != V_TEXT else 0.55
        if cell.kind == V_DATE:
            weight *= 0.9 if top == V_NUMBER else 0.2
        elif cell.kind == V_TEXT and len(str(cell.value)) > 60:
            weight *= 0.4
        if cell.kind == V_TEXT and top == V_TEXT and same_text:
            weight *= 0.3
        score += weight * top_count / present
        named += 1
    return (score - 1.2 * numbers, named)


def _repeats(grid: Grid) -> set[int]:
    """Lines of 2+ text cells and no figures whose text in the same columns sits on another line, exactly or at 70% for 3+ cells. A value on many lines says nothing."""
    exact: Counter = Counter()
    keys: dict[int, tuple] = {}
    pool: list[tuple[int, set]] = []
    for r in grid.filled:
        line = grid.rows[r]
        cells = [(c, str(cell.value).lower()) for c, cell in line.items() if cell.kind == V_TEXT]
        numbers = sum(1 for cell in line.values() if cell.kind == V_NUMBER)
        if len(cells) >= 2 and numbers == 0:
            keys[r] = tuple(cells)
            exact[keys[r]] += 1
        if len(cells) >= 3 and numbers * 3 <= len(line):
            pool.append((r, set(cells)))
    out = {r for r, key in keys.items() if exact[key] >= 2}
    if len(pool) > 3000:
        return out
    by_cell: dict[tuple, list[int]] = {}
    for i, (_, cells) in enumerate(pool):
        for cell in sorted(cells):
            by_cell.setdefault(cell, []).append(i)
    for i, (r, cells) in enumerate(pool):
        overlap: Counter = Counter()
        for cell in cells:
            bucket = by_cell[cell]
            if len(bucket) <= 60:
                overlap.update(j for j in bucket if j != i)
        for j, shared in overlap.items():
            if shared >= 3 and shared >= 0.7 * max(len(cells), len(pool[j][1])):
                out.add(r)
                out.add(pool[j][0])
    return out


def line_facts(grid: Grid) -> dict[int, LineFacts]:
    """The named coordinates of every filled line."""
    filled = grid.filled
    shapes = grid.shapes
    ties = _arith_ties(grid)
    repeats = _repeats(grid)
    style = {r: _style_share(grid, r) for r in filled}
    position = {r: i for i, r in enumerate(filled)}
    marks = {r: is_total_mark(list(grid.rows[r].values())) for r in filled}
    word_ties = _word_ties(grid, {r for r, mark in marks.items() if mark})

    def apart(row: int, attr: int, step: int) -> bool:
        if style[row][attr] < 0.5:
            return False
        at = position[row]
        near = filled[at + 1:at + 1 + _STYLE_WINDOW] if step > 0 else filled[max(0, at - _STYLE_WINDOW):at]
        return bool(near) and sum(style[x][attr] for x in near) / len(near) <= 0.25

    facts: dict[int, LineFacts] = {}
    for at, r in enumerate(filled):
        line = grid.rows[r]
        cells = list(line.values())
        kinds = Counter(cell.kind for cell in cells)
        texts = [str(cell.value) for cell in cells if cell.kind == V_TEXT]
        numbers = [cell for cell in cells if cell.kind == V_NUMBER]
        agg_column = agg_row = computed = 0
        if shapes:
            for c in line:
                shape = shapes.get((r, c))
                if shape is None:
                    continue
                if shape.kind == FX_AGG_COLUMN:
                    agg_column += 1
                elif shape.kind == FX_AGG_ROW:
                    agg_row += 1
                elif shape.kind in (FX_COLUMN_ARITH, FX_LINK_COLUMN):
                    computed += 1
        figures = max(1, len(numbers))
        columns = list(line)
        prev_row = filled[at - 1] if at else None
        next_row = filled[at + 1] if at + 1 < len(filled) else None
        run = [cell.value for cell in numbers]
        sequence = len(run) >= 3 and run[0] in (0.0, 1.0) and all(b - a == 1 for a, b in zip(run, run[1:]))
        real_figures = 0 if sequence else sum(1 for cell in numbers if not ((year_number(cell.value) and cell.format_kind != F_CURRENCY) or _written_index(cell)))
        wide = grid.wide_merge.get(r)
        later = filled[at + 1:at + 1 + _BELOW_WINDOW]
        score, named = (0.0, 0)
        if kinds[V_TEXT] + kinds[V_DATE] + kinds[V_PERIOD] >= 2 and len(numbers) * 3 <= len(cells):
            score, named = _value_scores(grid, r, later)
        typed = [any(cell.kind in (V_NUMBER, V_DATE, V_ERROR, V_MARKER) for cell in grid.rows[x].values()) for x in later[:8]]
        typed_share = 0.0
        if True in typed:
            tail = typed[typed.index(True):]
            typed_share = sum(tail) / len(tail)
        below_columns: set[int] = set()
        for x in later[:5]:
            below_columns.update(grid.rows[x])
        overlap = len(below_columns.intersection(line)) / len(below_columns) if below_columns else 0.0
        over_figures = 0.0
        for x in later:
            below = grid.rows[x]
            if sum(1 for cell in below.values() if cell.kind == V_NUMBER) >= 1:
                over_figures = sum(1 for c in line if c in below and below[c].kind == V_NUMBER) / len(line)
                break
        facts[r] = LineFacts(
            row=r, filled=len(cells), text=kinds[V_TEXT], number=len(numbers), date=kinds[V_DATE], period=kinds[V_PERIOD], error=kinds[V_ERROR], marker=kinds[V_MARKER],
            first_col=columns[0], last_col=columns[-1], wordy=sum(len(t.split()) >= 6 for t in texts), longest=max((len(t) for t in texts), default=0),
            total_word=marks[r], agg_column=agg_column, agg_row=agg_row, computed=computed,
            formula_major=agg_column > 0 and (agg_column + agg_row) * 2 >= figures,
            formula_line=bool(numbers) and computed + agg_column > 0 and (agg_column + agg_row + computed) * 2 >= figures,
            arith_ties=ties.get(r, 0), bold=style[r][0], fill=style[r][1], under=style[r][2],
            bold_next=apart(r, 0, 1), fill_next=apart(r, 1, 1), under_next=apart(r, 2, 1), bold_prev=apart(r, 0, -1), fill_prev=apart(r, 1, -1),
            repeated=r in repeats, real_figures=real_figures, sequence=sequence, gap_above=prev_row is None or prev_row != r - 1, gap_below=next_row is None or next_row != r + 1,
            prev=prev_row, next=next_row, wide_merge=wide[1] - wide[0] + 1 if wide else 0, label_value=any(_LABEL_VALUE.match(t) for t in texts),
            value_score=score, named=named, typed_below=typed_share, overlap_below=overlap, over_figures=over_figures,
            word_col=next((c for c, cell in line.items() if cell.kind == V_TEXT and TOTAL_WORD.search(str(cell.value))), 0) if marks[r] else 0,
            grand=marks[r] and any(_GRAND_TOTAL.search(t) for t in texts), word_ties=word_ties.get(r, 0))
    return facts
