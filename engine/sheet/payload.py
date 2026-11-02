"""What a host sends for one workbook: the wire contract of the sheet compiler.

Workbook JSON:  {"name": "<file or spreadsheet name>", "tabs": [<tab>, ...]}, tabs in workbook order, hidden and chart tabs included.

Tab JSON, every row and column index 0-based and relative to the sheet's A1 unless `origin` says otherwise:
  name             string   the tab's name
  sheetType        string   "grid" or "chart"; a chart tab sends no cells
  hidden           bool     the tab is hidden
  rows             array    the used range, a row of cell values each: "" blank; number; boolean; string. A date, time or duration is its serial day number, with its
                            format below. An error is its text ("#REF!"). A formula sends its calculated value here and its text in `formulas`.
  origin           object   {"r","c"} of rows[0][0] in the sheet; omitted means {"r":0,"c":0}. Excel's used range need not start at A1.
  columnFormats    array    one per column: {"f": "<the column's commonest number format over its number cells>", "x": [[row, "<format>"], ...]} for the number cells that differ
  errorCells       array    [[row, col], ...] cells holding a formula error (a host whose error text is not reliable may rely on this; the text in `rows` is read too)
  merges           array    [{"s":{"r","c"}, "e":{"r","c"}}, ...]
  date1904         bool     the workbook counts days from 1904
  formulas         array    [[row, col, "=SUM(B2:B9)"], ...] sparse, in A1 notation, one entry per formula cell
  rowStyles        array    [[row, filled, bold, fill, underline], ...] sparse, only rows where any count is nonzero: of the row's filled cells, how many there are, how many are bold,
                            have a fill other than white or none, are underlined
  frozenRows       int      rows frozen from the top; 0 if none
  frozenColumns    int      columns frozen from the left; 0 if none
  autofilter       object   {"s":{"r","c"}, "e":{"r","c"}} or null
  hiddenRows       array    [[first, last], ...] inclusive runs hidden by the user or a filter
  hiddenColumns    array    [[first, last], ...] inclusive runs

What each host must add to what it reads today (nothing here changes an add-on yet; Phase 6 does):
  Google Sheets (sheets-addon/Code.js readTab_; all with scope spreadsheets.currentonly). Already sends name, rows, columnFormats, errorCells, merges, date1904.
    formulas        Range.getFormulas, kept sparse.
    rowStyles       Range.getFontWeights, getBackgrounds, getFontLines, compacted to these per-row counts (read style only on a time budget: a missing row counts as no mark).
    frozenRows      Sheet.getFrozenRows; frozenColumns Sheet.getFrozenColumns.
    autofilter      Sheet.getFilter()?.getRange().
    hiddenRows      Sheet.isRowHiddenByUser and isRowHiddenByFilter are per-row calls: batch through the Sheets API or skip on long tabs; absent means none known.
    hiddenColumns   Sheet.isColumnHiddenByUser, per column.
    hidden          Sheet.isSheetHidden. sheetType: Sheet.getType (OBJECT is "chart").
  Excel (web/public/office/excel/host.js). Already sends rows, merges, date1904; sends `formats` and `errors` matrices, which must compact to columnFormats and errorCells as Sheets does.
    origin          used.rowIndex, used.columnIndex.
    formulas        range.load('formulas'), kept sparse.
    rowStyles       getCellProperties({format:{font:{bold,underline},fill:{color}}}), ExcelApi 1.9.
    frozenRows      worksheet.freezePanes.getLocationOrNullObject, 1.7.
    autofilter      worksheet.autoFilter.getRangeOrNullObject, 1.9.
    hiddenRows      getRowProperties and getColumnProperties, 1.9. hidden: worksheet.visibility. Chart sheets are not in worksheets: sheetType stays "grid".
Absent optional fields mean "not read", which the compiler treats as no signal, never as an error. Only name and rows are required (a chart tab needs only name and sheetType).

Parsing a tab normalizes indices to 1-based sheet rows and columns; everything after this module speaks those.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from engine.sheet.names import SHEET_CHART, SHEET_GRID


@dataclass(slots=True)
class TabPayload:
    """One tab, normalized: rows[i] is sheet row i + 1 with leading blanks for the origin; formulas and styles keyed by 1-based sheet address."""
    name: str
    sheet_type: str = SHEET_GRID
    hidden: bool = False
    rows: list[list] = field(default_factory=list)
    formats: dict[int, str] = field(default_factory=dict)             # 1-based column -> its commonest format
    format_exceptions: dict[tuple[int, int], str] = field(default_factory=dict)   # (row, column) -> a differing format
    error_cells: frozenset[tuple[int, int]] = frozenset()
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)   # (row1, col1, row2, col2)
    date1904: bool = False
    formulas: dict[tuple[int, int], str] = field(default_factory=dict)
    row_styles: dict[int, tuple[int, int, int, int]] = field(default_factory=dict)   # row -> (filled, bold, fill, underline)
    frozen_rows: int = 0
    frozen_columns: int = 0
    autofilter: tuple[int, int, int, int] | None = None
    hidden_rows: frozenset[int] = frozenset()
    hidden_columns: frozenset[int] = frozenset()


@dataclass(slots=True)
class WorkbookPayload:
    name: str
    tabs: list[TabPayload]


def _area(area: dict, r0: int, c0: int) -> tuple[int, int, int, int]:
    s, e = area["s"], area["e"]
    return (int(s["r"]) + r0 + 1, int(s["c"]) + c0 + 1, int(e["r"]) + r0 + 1, int(e["c"]) + c0 + 1)


def _runs(runs: list, offset: int) -> frozenset[int]:
    out: set[int] = set()
    for first, last in runs or ():
        out.update(range(int(first) + offset + 1, int(last) + offset + 2))
    return frozenset(out)


def tab_from_json(raw: dict) -> TabPayload:
    """The wire form of one tab as a TabPayload."""
    origin = raw.get("origin") or {}
    r0, c0 = int(origin.get("r", 0)), int(origin.get("c", 0))
    rows = [[""] * c0 + list(row) for row in ([[]] * r0 + list(raw.get("rows") or []))]
    formats: dict[int, str] = {}
    exceptions: dict[tuple[int, int], str] = {}
    for c, column in enumerate(raw.get("columnFormats") or ()):
        if column.get("f"):
            formats[c + c0 + 1] = column["f"]
        for row, fmt in column.get("x") or ():
            exceptions[(int(row) + r0 + 1, c + c0 + 1)] = fmt
    return TabPayload(
        name=str(raw["name"]),
        sheet_type=raw.get("sheetType") or SHEET_GRID,
        hidden=bool(raw.get("hidden")),
        rows=rows,
        formats=formats,
        format_exceptions=exceptions,
        error_cells=frozenset((int(r) + r0 + 1, int(c) + c0 + 1) for r, c in raw.get("errorCells") or ()),
        merges=[_area(m, r0, c0) for m in raw.get("merges") or ()],
        date1904=bool(raw.get("date1904")),
        formulas={(int(r) + r0 + 1, int(c) + c0 + 1): str(text) for r, c, text in raw.get("formulas") or ()},
        row_styles={int(row[0]) + r0 + 1: (int(row[1]), int(row[2]), int(row[3]), int(row[4])) for row in raw.get("rowStyles") or ()},
        frozen_rows=int(raw.get("frozenRows") or 0),
        frozen_columns=int(raw.get("frozenColumns") or 0),
        autofilter=_area(raw["autofilter"], r0, c0) if raw.get("autofilter") else None,
        hidden_rows=_runs(raw.get("hiddenRows"), r0),
        hidden_columns=_runs(raw.get("hiddenColumns"), c0),
    )


def workbook_from_json(raw: dict) -> WorkbookPayload:
    return WorkbookPayload(str(raw.get("name") or ""), [tab_from_json(tab) for tab in raw.get("tabs") or ()])


__all__ = ["SHEET_CHART", "SHEET_GRID", "TabPayload", "WorkbookPayload", "tab_from_json", "workbook_from_json"]
