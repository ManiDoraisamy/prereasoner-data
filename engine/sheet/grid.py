"""One tab's payload read into typed cells, with the formula shapes and merges a line reader needs. Each cell's kind comes from the one number parser and the one period parser."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from functools import lru_cache

from engine.sheet.formulas import FormulaShape, classify, mark_runs
from engine.sheet.model import Cell
from engine.sheet.names import F_DATE, F_GENERAL, F_PERCENT, P_DAY, V_BOOL, V_DATE, V_ERROR, V_MARKER, V_NUMBER, V_PERIOD, V_TEXT
from engine.sheet.payload import TabPayload
from engine.sheet.values import aside_number, format_kind, is_error, is_marker, parse_number, parse_period, period_of_serial


_RULE = re.compile(r"^(?:[-=_~]\s*){2,}$|^(?:[*.]\s*){3,}$")
# The leader a printed table runs from a label to its figures ("Alabama...............", "  United States......."):
# four dots or more ending a text that holds something else. An ellipsis is three.
_LEADER = re.compile(r"(?<=\S)\s*(?:\.\s*){4,}$")
_LEADERS_FEWEST = 3


def clean(text: str) -> str:
    return " ".join(text.split())


@lru_cache(maxsize=131072)
def _read_text(text: str, typed: str | None = None) -> tuple:
    """(kind, value, period, format kind) of a text cell. typed: the day or month (ISO text) the workbook's loading read
    this text as (engine/program/dates.py), parsed in the text's place; the cell keeps its own text."""
    stripped = clean(text)
    if is_error(stripped):
        return (V_ERROR, stripped, None, F_GENERAL)
    if is_marker(stripped):
        return (V_MARKER, stripped, None, F_GENERAL)
    reading = parse_number(stripped) or aside_number(stripped)     # "41(11)", "38 (100)": a number and its bracketed aside
    if reading is not None:
        return (V_NUMBER, reading.value, None, F_PERCENT if reading.percent else F_GENERAL)
    period = parse_period(stripped if typed is None else typed)
    if period is not None:
        return ((V_DATE, period.start, period, F_DATE) if period.kind == P_DAY else (V_PERIOD, stripped, period, F_GENERAL))
    return (V_TEXT, stripped, None, F_GENERAL)


def read_cell(row: int, col: int, raw: object, number_format: str | None, date1904: bool, typed: Mapping[str, str] | None = None) -> Cell | None:
    """The typed cell, or None when the cell is blank. typed: text cells read as days or months when the workbook was
    loaded, by their text (engine/program/dates.py)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return Cell(row, col, raw, V_BOOL, raw)
    if isinstance(raw, (int, float)):
        value = float(raw)
        if not math.isfinite(value):
            return None
        fmt = format_kind(number_format)
        if fmt == F_DATE:
            period = period_of_serial(value, number_format, date1904)
            if period is not None:
                return Cell(row, col, raw, V_DATE if period.kind == P_DAY else V_PERIOD, period.start, period, F_DATE)
        return Cell(row, col, raw, V_NUMBER, value, None, fmt)
    text = str(raw)
    if not text.strip():
        return None
    kind, value, period, fmt = _read_text(text, typed.get(clean(text)) if typed else None)
    return Cell(row, col, raw, kind, value, period, fmt)


class Grid:
    """A tab's filled cells by row, 1-based, each row's cells in column order."""

    def _drop_leaders(self, typed: Mapping[str, str] | None) -> None:
        """A column of labels a printed table fills with leader dots is read without them: the dots are the page's, as
        a rule line is, and no part of a name ("California.........." is California: a question finds it, an answer
        prints it). Only where at least _LEADERS_FEWEST text cells of a grid column end in a leader and they are at
        least half of its text cells; a cell alone that ends in dots (a sentence trailing off) keeps them."""
        columns: dict[int, list[Cell]] = {}
        for line in self.rows.values():
            for c, cell in line.items():
                if cell.kind == V_TEXT and isinstance(cell.raw, str):
                    columns.setdefault(c, []).append(cell)
        for cells in columns.values():
            led = [cell for cell in cells if _LEADER.search(cell.raw.rstrip())]
            if len(led) < _LEADERS_FEWEST or len(led) * 2 < len(cells):
                continue
            for cell in led:
                read = read_cell(cell.row, cell.col, _LEADER.sub("", cell.raw.rstrip()), None, self.tab.date1904, typed)
                if read is not None:
                    cell.raw, cell.kind, cell.value, cell.period, cell.format_kind = read.raw, read.kind, read.value, read.period, read.format_kind

    def __init__(self, tab: TabPayload, typed: Mapping[str, str] | None = None):
        self.tab = tab
        self.rows: dict[int, dict[int, Cell]] = {}
        formats, exceptions = tab.formats, tab.format_exceptions
        date1904 = tab.date1904
        shapes: dict[tuple[int, int], FormulaShape] = {}
        for at, text in sorted(tab.formulas.items()):
            shapes[at] = classify(text, *at)
        self.shapes = mark_runs(shapes, tab.formulas)
        for r, values in enumerate(tab.rows, 1):
            line = None
            for c, raw in enumerate(values, 1):
                if raw is None or raw == "":
                    continue
                fmt = exceptions.get((r, c), formats.get(c)) if isinstance(raw, (int, float)) else None
                cell = read_cell(r, c, raw, fmt, date1904, typed)
                if cell is None:
                    continue
                if (r, c) in tab.error_cells:
                    cell.kind = V_ERROR
                shape = self.shapes.get((r, c))
                if shape is not None:
                    cell.formula = tab.formulas[(r, c)]
                    cell.shape = shape.kind
                if line is None:
                    line = self.rows[r] = {}
                line[c] = cell
        self._drop_leaders(typed)
        # A line of rule characters is a spacer, decided before anything reads the lines: it names nothing, holds nothing, and its neighbours see a gap.
        self.rule_lines = frozenset(r for r, line in self.rows.items() if all(isinstance(cell.raw, str) and _RULE.match(clean(cell.raw)) for cell in line.values()))
        self.filled = sorted(r for r in self.rows if r not in self.rule_lines)
        self.last_row = max(self.rows, default=0)
        self.width = max((max(line) for line in self.rows.values()), default=0)
        # A merge's covered cells point at its top-left cell.
        self.spans: dict[tuple[int, int], tuple[int, int]] = {}
        self.wide_merge: dict[int, tuple[int, int]] = {}          # row -> (first column, last column) of its widest merge
        for r1, c1, r2, c2 in tab.merges:
            if (r2 - r1 + 1) * (c2 - c1 + 1) <= 4096:
                for r in range(r1, r2 + 1):
                    for c in range(c1, c2 + 1):
                        self.spans[(r, c)] = (r1, c1)
            if c2 > c1 and (r1 not in self.wide_merge or c2 - c1 > self.wide_merge[r1][1] - self.wide_merge[r1][0]):
                self.wide_merge[r1] = (c1, c2)
