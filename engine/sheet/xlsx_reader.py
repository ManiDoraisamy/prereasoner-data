"""Build the host payload (engine/sheet/payload.py) from an .xlsx file with openpyxl: the reader evaluation and tests use, and what the upload path will use.

read_workbook(path) -> the workbook JSON a host would send. Cached values come from a second, read-only load, as a formula cell's value lives apart from its text. Nothing is
printed, so no cell text leaves this module. openpyxl is imported here and nowhere else.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter

_WHITE = {"rFFFFFFFF", "r00FFFFFF", "i9", "i65", "t0"}
_REF = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?$")


def _column_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _area(ref: str) -> dict | None:
    m = _REF.match(ref.strip())
    if m is None:
        return None
    r1, c1 = int(m[2]) - 1, _column_index(m[1]) - 1
    r2, c2 = (int(m[4]) - 1, _column_index(m[3]) - 1) if m[3] else (r1, c1)
    return {"s": {"r": r1, "c": c1}, "e": {"r": r2, "c": c2}}


def _runs(indices: set[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for i in sorted(indices):
        if runs and runs[-1][1] == i - 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    return runs


def _color_key(color) -> str | None:
    if color is None:
        return None
    kind = getattr(color, "type", None)
    if kind == "rgb":
        return f"r{color.rgb}"
    if kind == "indexed":
        return f"i{color.indexed}"
    if kind == "theme":
        return f"t{color.theme}"
    return None


class _Styles:
    """(bold, fill, underline, number format) of a cell's style array, read once per distinct array."""

    def __init__(self, wb, builtin: dict):
        self.wb = wb
        self.builtin = builtin
        self.seen: dict[tuple, tuple[bool, bool, bool, str]] = {}

    def of(self, cell) -> tuple[bool, bool, bool, str]:
        array = cell._style
        key = tuple(array) if array is not None else ()
        got = self.seen.get(key)
        if got is None:
            got = self.seen[key] = self._read(array) if key else (False, False, False, "General")
        return got

    def _read(self, array) -> tuple[bool, bool, bool, str]:
        wb = self.wb
        font, fill = wb._fonts[array.fontId], wb._fills[array.fillId]
        idx = array.numFmtId
        if idx < 164:
            fmt = self.builtin.get(idx, "General")
        else:
            fmt = wb._number_formats[idx - 164] if idx - 164 < len(wb._number_formats) else "General"
        kind = getattr(fill, "fill_type", None)
        filled = bool(kind) and (kind != "solid" or _color_key(getattr(fill, "fgColor", None)) not in _WHITE)
        return (bool(font.b), filled, bool(font.u), fmt)


def _serial(value, to_excel, epoch) -> float | str:
    """A date, time or duration as the serial number a host sends."""
    if isinstance(value, dt.timedelta):
        return value.total_seconds() / 86400.0
    if isinstance(value, dt.time):
        return (value.hour * 3600 + value.minute * 60 + value.second) / 86400.0
    try:
        return float(to_excel(value, epoch))
    except (OverflowError, ValueError, TypeError):
        return ""


def _formula_text(value) -> str:
    return value if isinstance(value, str) else "=" + str(getattr(value, "text", "") or "")


def _tab(ws, styles: _Styles, cached, to_excel, epoch, date1904: bool) -> dict:
    grid: dict[int, dict[int, object]] = {}
    formulas: list[list] = []
    style_counts: dict[int, list[int]] = {}
    format_counts: dict[int, Counter] = {}
    number_formats: dict[tuple[int, int], str] = {}
    for (r, c), cell in sorted(ws._cells.items()):
        value = cell.value
        is_formula = cell.data_type == "f"
        if is_formula:
            formulas.append([r - 1, c - 1, _formula_text(value)])
            value = cached.get((r, c))
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        bold, fill, under, fmt = styles.of(cell)
        if isinstance(value, (dt.datetime, dt.date, dt.time, dt.timedelta)):
            value = _serial(value, to_excel, epoch)
            if value == "":
                continue
        grid.setdefault(r, {})[c] = value
        counts = style_counts.setdefault(r, [0, 0, 0, 0])
        counts[0] += 1
        counts[1] += bold
        counts[2] += fill
        counts[3] += under
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            format_counts.setdefault(c, Counter())[fmt] += 1
            number_formats[(r, c)] = fmt
    height = max(grid, default=0)
    width = max((max(row) for row in grid.values()), default=0)
    rows = [[grid.get(r, {}).get(c, "") for c in range(1, width + 1)] for r in range(1, height + 1)]
    column_formats = []
    common = {}
    for c in range(1, width + 1):
        counts = format_counts.get(c)
        # Commonest first; a tie goes to the format's own order, so the wire form is deterministic.
        common[c] = min(counts, key=lambda f: (-counts[f], f)) if counts else "General"
        column_formats.append({"f": common[c]})
    for (r, c), fmt in sorted(number_formats.items()):
        if fmt != common[c]:
            column_formats[c - 1].setdefault("x", []).append([r - 1, fmt])
    hidden_rows = {r - 1 for r, dim in ws.row_dimensions.items() if dim.hidden and r <= height}
    hidden_columns: set[int] = set()
    for dim in ws.column_dimensions.values():
        if dim.hidden and dim.min:
            hidden_columns.update(range(dim.min - 1, min(dim.max or dim.min, width)))
    view = ws.sheet_view
    pane = view.pane if view is not None else None
    frozen_rows = frozen_columns = 0
    if pane is not None and pane.state in ("frozen", "frozenSplit"):
        top = int(re.sub(r"\D", "", view.topLeftCell or "A1") or 1)
        frozen_rows = top - 1 + int(pane.ySplit or 0) if pane.ySplit else 0
        frozen_columns = int(pane.xSplit or 0)
    autofilter = _area(ws.auto_filter.ref) if ws.auto_filter is not None and ws.auto_filter.ref else None
    return {
        "name": ws.title,
        "sheetType": "grid",
        "hidden": ws.sheet_state != "visible",
        "rows": rows,
        "columnFormats": column_formats,
        "errorCells": [],
        "merges": [{"s": {"r": m.min_row - 1, "c": m.min_col - 1}, "e": {"r": m.max_row - 1, "c": m.max_col - 1}} for m in sorted(ws.merged_cells.ranges, key=lambda m: (m.min_row, m.min_col))],
        "date1904": date1904,
        "formulas": formulas,
        "rowStyles": [[r - 1, *counts] for r, counts in sorted(style_counts.items()) if any(counts[1:])],
        "frozenRows": frozen_rows,
        "frozenColumns": frozen_columns,
        "autofilter": autofilter,
        "hiddenRows": _runs(hidden_rows),
        "hiddenColumns": _runs(hidden_columns),
    }


def read_workbook(path: str) -> dict:
    """The workbook JSON a host would send for this file. keep_links=False: a broken external part must not fail the load."""
    import warnings

    import openpyxl
    from openpyxl.styles.numbers import BUILTIN_FORMATS
    from openpyxl.utils.datetime import to_excel

    warnings.filterwarnings("ignore", module="openpyxl")
    wb = openpyxl.load_workbook(path, data_only=False, keep_links=False)
    date1904 = wb.epoch != openpyxl.utils.datetime.WINDOWS_EPOCH
    styles = _Styles(wb, BUILTIN_FORMATS)
    wanted: dict[str, set[tuple[int, int]]] = {ws.title: {at for at, cell in ws._cells.items() if cell.data_type == "f"} for ws in wb.worksheets}
    cached: dict[str, dict[tuple[int, int], object]] = {title: {} for title in wanted}
    if any(wanted.values()):
        values = openpyxl.load_workbook(path, data_only=True, read_only=True, keep_links=False)
        try:
            for title, cells in wanted.items():
                if not cells:
                    continue
                last_row = max(r for r, _ in cells)
                for row in values[title].iter_rows(max_row=last_row):
                    for cell in row:
                        value = getattr(cell, "value", None)
                        if value is not None and (cell.row, cell.column) in cells:
                            cached[title][(cell.row, cell.column)] = value
        finally:
            values.close()
    chart_titles = {sheet.title: sheet for sheet in wb.chartsheets}
    tabs = []
    for title in wb.sheetnames:
        if title in chart_titles:
            tabs.append({"name": title, "sheetType": "chart", "hidden": chart_titles[title].sheet_state != "visible", "rows": []})
        else:
            tabs.append(_tab(wb[title], styles, cached[title], to_excel, wb.epoch, date1904))
    return {"name": path.replace("\\", "/").rsplit("/", 1)[-1], "tabs": tabs}
