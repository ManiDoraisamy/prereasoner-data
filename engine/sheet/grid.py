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
