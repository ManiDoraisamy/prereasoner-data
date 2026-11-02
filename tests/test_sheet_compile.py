"""The sheet compiler on small synthetic grids, one per shape. Hermetic: no model training, no network, no Postgres; the xlsx reader's test skips where openpyxl is absent."""
from __future__ import annotations

import os
import sys
import tempfile

from engine.sheet import compile_tab, compile_workbook
from engine.sheet.formulas import classify
from engine.sheet.grid import Grid
from engine.sheet.line_facts import line_facts
from engine.sheet.line_model import FEATURES, contributions, header_score, line_features, load_model
from engine.sheet.names import (AXIS_GROUP, AXIS_PERIOD, AXIS_SEQUENCE, BAND, COLUMN_ROLES, DATE, F_CURRENCY, F_DATE, F_GENERAL, F_PERCENT, FX_AGG_COLUMN, FX_AGG_ROW, FX_LINK_COLUMN, FX_ROW_ARITH, HEADER, KEY, LABEL, LINE_ROLES,
                                MEASURE, NOTE, P_DAY, P_MONTH, P_QUARTER, P_RANGE, P_RELATIVE, P_SEASON, P_YEAR, PEER, ROW_TOTAL, S_AUTOFILTER_TOP, S_FORMULA_TOTAL, S_FROZEN_LAST, SECTION_LABEL, SHEET_CHART, STRAY_ERROR,
                                STRAY_LABEL, STRAY_MARKER, STRAY_NUMBER_WITH_WORD, STRAY_REPEATED_HEADER, STRAY_TOTAL, SUBTOTAL, TAB_NAME, TITLE, TOTAL, V_NUMBER, V_TEXT)
from engine.sheet.payload import tab_from_json, workbook_from_json
from engine.sheet.values import format_kind, parse_number, parse_period, period_of_serial


def _tab(rows, name="Sheet1", **more):
    return compile_tab(tab_from_json({"name": name, "rows": rows, **more}))


def _roles(model):
    return {line.row: line.role for line in model.lines}


CLEAN = [["Region", "Rep", "Date", "Amount"], ["East", "Ann", 36892, 10.0], ["West", "Bob", 36893, 20.5], ["East", "Cal", 36894, 30.0], ["North", "Dee", 36895, 40.0]]
CLEAN_FORMATS = [{"f": "General"}, {"f": "General"}, {"f": "mm/dd/yy"}, {"f": "#,##0.00"}]


def test_number_parser():
    assert parse_number("(1,234.50)").value == -1234.5
    assert parse_number("$1,234").value == 1234 and parse_number("$1,234").currency == "$"
    assert parse_number("45%").value == 0.45 and parse_number("45%").percent
    assert parse_number("(45%)").value == -0.45
    assert parse_number("1,234-").value == -1234
    assert parse_number("-$5.5").value == -5.5
    assert parse_number("30M").value == 30e6 and parse_number("5K").value == 5e3
    assert parse_number("1.5e3").value == 1500
    for text in ("1,23", "12,34,5", "abc", "12 apples", "(5", "", "3-4", "1/2"):
        assert parse_number(text) is None, text


def test_period_parser():
    day = parse_period("04-MAY-2000")
    assert (day.kind, day.start) == (P_DAY, "2000-05-04")
    assert parse_period("Mar-22-01").start == "2001-03-22"
    assert parse_period("3/4/01").start == "2001-03-04"
    assert parse_period("13/4/2001").start == "2001-04-13"
    assert parse_period("2001-03-04").start == "2001-03-04"
    assert parse_period("March 4, 2001").start == "2001-03-04"
    assert parse_period("Q2").kind == P_QUARTER and parse_period("Q2").quarter == 2 and parse_period("Q2").start is None
    assert (parse_period("2Q01").start, parse_period("2Q01").end) == ("2001-04-01", "2001-06-30")
    assert parse_period("Q2 01").start == "2001-04-01"
    assert parse_period("2nd Quarter").quarter == 2
    range_a = parse_period("Jul-Sep 02")
    assert (range_a.kind, range_a.start, range_a.end) == (P_RANGE, "2002-07-01", "2002-09-30")
    range_b = parse_period("Nov02-Mar03")
    assert (range_b.start, range_b.end) == ("2002-11-01", "2003-03-31")
    assert parse_period("Nov-Mar 03").start == "2002-11-01"
    assert parse_period("January").kind == P_MONTH and parse_period("January").month == 1 and parse_period("January").start is None
    assert parse_period("Jan-02").start == "2002-01-01" and parse_period("Jan 2002").end == "2002-01-31"
    assert parse_period("Mar 22").day == 22
    assert parse_period("2001").kind == P_YEAR and parse_period("Cal-03").start == "2003-01-01"
    assert parse_period("Sum00").kind == P_SEASON and parse_period("Summer").word == "summer"
    assert parse_period("YTD").kind == P_RELATIVE
    assert parse_period("HE12").word == "hour 12"
    assert parse_period("1/1/01 - 1/31/01").end == "2001-01-31"
    for text in ("H28282", "Sum", "Win", "total sales", "Mayor", "13/45/01", "March madness", "Q5", "12345"):
        assert parse_period(text) is None, text
    assert period_of_serial(36892, "mm/dd/yy").start == "2001-01-01"
    assert period_of_serial(36892, "mmm-yy").kind == P_MONTH
    assert period_of_serial(0, "mm/dd/yy", date1904=True).start == "1904-01-01"


def test_format_kinds():
    for fmt, kind in (("General", F_GENERAL), ("0.00%", F_PERCENT), ('_($* #,##0_)', F_CURRENCY), ("[$-409]mmmm\\-yy", F_DATE), ("m/d/yy h:mm", F_DATE), ("#,##0_);(#,##0)", "number"), ("h:mm AM/PM", "time"), ("[h]:mm", "duration"), ("@", "text")):
        assert format_kind(fmt) == kind, fmt


def test_formula_shapes():
    assert classify("=SUM(B2:B9)", 10, 2).kind == FX_AGG_COLUMN and classify("=SUM(B2:B9)", 10, 2).span == (2, 9)
    assert classify("=B2+B3+B4", 5, 2).kind == FX_AGG_COLUMN
    assert classify("=SUM(B2:D2)", 2, 5).kind == FX_AGG_ROW
    assert classify("=C5-D5", 5, 6).kind == FX_ROW_ARITH
    assert classify("=B7", 9, 2).kind == FX_LINK_COLUMN


def test_clean_table_comes_out_unchanged():
    model = _tab(CLEAN, columnFormats=CLEAN_FORMATS)
    assert len(model.tables) == 1
    table = model.tables[0]
    assert [c.name for c in table.columns] == ["Region", "Rep", "Date", "Amount"]
    assert [c.letter for c in table.columns] == ["A", "B", "C", "D"]
    assert [line.row for line in table.lines] == [2, 3, 4, 5]
    assert table.header_rows == (1,)
    assert not table.printed and not table.bands and not table.strays and not table.axes
    assert [table.columns[i].role for i in (0, 2, 3)] == [LABEL, DATE, MEASURE]
    assert table.lines[1].cells[3].value == 20.5 and table.lines[1].cells[3].address == "D3"
    assert table.lines[1].cells[2].value == "2001-01-02"
    assert all(role in LINE_ROLES for role in _roles(model).values()) and all(c.role in COLUMN_ROLES for c in table.columns)


def test_title_lines_give_context():
    rows = [["Gas Report, in thousands"], ["as of 4/20/01"], [], ["Pipe", "Volume", "Price"], ["A", 1, 2], ["B", 3, 4], ["C", 5, 6]]
    model = _tab(rows, name="West")
    table = model.tables[0]
    assert _roles(model)[1] == TITLE and _roles(model)[4] == HEADER
    assert table.context.tab == "West"
    assert table.context.as_of.start == "2001-04-20" and table.context.as_of_address == "A2"
    assert "thousands" in table.context.unit and table.context.unit_address == "A1"
    assert [t[0] for t in table.context.titles] == ["A1", "A2"]
    assert _tab(CLEAN, name="Jan 2001", columnFormats=CLEAN_FORMATS).tables[0].context.as_of.start == "2001-01-01"


def test_multi_row_header_joins_with_merges():
    rows = [["", "Volume", "", "Price", ""], ["Pipe", "In", "Out", "Bid", "Ask"], ["A", 4, 2, 3, 9], ["B", 5, 7, 6, 8], ["C", 9, 1, 2, 3]]
    table = _tab(rows, merges=[{"s": {"r": 0, "c": 1}, "e": {"r": 0, "c": 2}}, {"s": {"r": 0, "c": 3}, "e": {"r": 0, "c": 4}}]).tables[0]
    assert table.header_rows == (1, 2)
    assert [c.name for c in table.columns] == ["Pipe", "Volume In", "Volume Out", "Price Bid", "Price Ask"]
    assert table.columns[2].path == ("Volume", "Out") and table.columns[2].header_cells == ("B1", "C2")
    assert table.columns[2].letter == "C"


def test_total_lines_are_printed_and_never_records():
    rows = [["Book", "Amt", "Qty"], ["a", 1, 2], ["b", 3, 4], ["Subtotal", 4, 6], ["c", 5, 6], ["d", 7, 8], ["", 16, 20]]
    formulas = [[3, 1, "=SUM(B2:B3)"], [3, 2, "=SUM(C2:C3)"], [6, 1, "=B4+SUM(B5:B6)"], [6, 2, "=C4+SUM(C5:C6)"]]
    model = _tab(rows, formulas=formulas)
    table = model.tables[0]
    assert [line.row for line in table.lines] == [2, 3, 5, 6]
    assert [(line.row, line.role) for line in table.printed] == [(4, SUBTOTAL), (7, TOTAL)]
    assert table.printed[0].label == "Subtotal" and table.printed[0].scope == (2, 3)
    assert S_FORMULA_TOTAL in table.printed[1].signals
    assert sum(line.cells[1].value for line in table.lines) == 16


def test_total_word_inside_a_name_stays_a_record():
    rows = [["Company", "Book", "Amt"], ["Total Fina Elf", "West", 1], ["Enron", "East", 2], ["Duke", "East", 3], ["Total", "", 6]]
    table = _tab(rows).tables[0]
    assert [line.row for line in table.lines] == [2, 3, 4]
    assert [line.row for line in table.printed] == [5]


def test_total_word_on_most_lines_is_not_a_mark():
    rows = [["Customer", "Amt"]] + [[f"C{i} Total", i] for i in range(1, 11)]
    table = _tab(rows).tables[0]
    assert len(table.lines) == 10 and not table.printed


def test_running_balance_is_not_a_total():
    rows = [["Day", "In", "Balance"], ["a", 1, 1], ["b", 2, 3], ["c", 3, 6], ["d", 4, 10]]
    formulas = [[r, 2, f"=SUM(C{r}:C{r})+B{r + 1}"] for r in ()] + [[2, 2, "=C2+B3"], [3, 2, "=C3+B4"], [4, 2, "=C4+B5"]]
    table = _tab(rows, formulas=formulas).tables[0]
    assert len(table.lines) == 4 and not table.printed


def test_bands_give_sections():
    rows = [["Item", "Amt"], ["East"], ["a", 1], ["b", 2], ["West"], ["c", 3], ["d", 4]]
    styles = [[1, 1, 1, 0, 0], [4, 1, 1, 0, 0]]
    table = _tab(rows, rowStyles=styles).tables[0]
    assert table.bands == ((2, "East"), (5, "West"))
    assert [line.section for line in table.lines] == [("East",), ("East",), ("West",), ("West",)]
    assert _roles(_tab(rows, rowStyles=styles))[2] == BAND


def test_sparse_label_column_fills_down_and_traces_to_its_source():
    rows = [["Region", "Item", "Amt"], ["East", "a", 1], ["", "b", 2], ["", "c", 3], ["West", "d", 4], ["", "e", 5], ["", "f", 6]]
    table = _tab(rows).tables[0]
    assert table.columns[0].role == SECTION_LABEL
    assert [line.cells[0].value for line in table.lines] == ["East", "East", "East", "West", "West", "West"]
    assert table.lines[2].cells[0].address == "A2" and table.lines[4].cells[0].address == "A5"


def test_stacked_tables_split():
    rows = [["Pipe", "Vol"], ["a", 1], ["b", 2], ["c", 3], [], ["Trader", "Desk", "Pnl"], ["x", "d1", 1], ["y", "d2", 2], ["z", "d3", 3]]
    model = _tab(rows)
    assert [t.header_rows for t in model.tables] == [(1,), (6,)]
    assert [c.name for c in model.tables[1].columns] == ["Trader", "Desk", "Pnl"]
    assert [line.row for line in model.tables[0].lines] == [2, 3, 4]


def test_repeated_header_continues_its_table():
    rows = [["Pipe", "Vol", "Price"], ["a", 1, 2], ["b", 3, 4], [], ["Pipe", "Vol", "Price"], ["c", 5, 6], ["d", 7, 8]]
    model = _tab(rows)
    assert len(model.tables) == 1
    assert [line.row for line in model.tables[0].lines] == [2, 3, 6, 7]
    assert model.tables[0].repeated_header_rows == (5,)


def test_side_by_side_blocks_split():
    rows = [["Pipe", "Vol", "", "Trader", "Pnl"], ["a", 1, "", "x", 5], ["b", 2, "", "y", 6], ["c", 3, "", "z", 7]]
    model = _tab(rows)
    assert [[c.name for c in t.columns] for t in model.tables] == [["Pipe", "Vol"], ["Trader", "Pnl"]]
    assert model.tables[1].columns[1].letter == "E"
    assert model.tables[1].lines[0].cells[1].address == "E2"


def test_spacer_columns_do_not_split_one_table():
    rows = [["Pipe", "", "Jan", "Feb", "", "Mar"], ["a", "", 4, 2, "", 3], ["b", "", 4, 9, "", 6], ["c", "", 7, 3, "", 9]]
    model = _tab(rows)
    assert len(model.tables) == 1 and [c.name for c in model.tables[0].columns] == ["Pipe", "Jan", "Feb", "Mar"]


def test_repeated_blocks_merge_into_one_family():
    rows = [["Pipe", "Vol", "", "Pipe", "Vol"], ["a", 1, "", "x", 5], ["b", 2, "", "y", 6], ["c", 3, "", "z", 7]]
    book = compile_workbook(workbook_from_json({"name": "w", "tabs": [{"name": "S", "rows": rows}]}))
    assert len(book.tabs[0].tables) == 2
    family = book.families[0]
    assert family.basis == "repeated_blocks" and len(family.table.lines) == 6
    assert family.table.lines[3].cells[1].address == "D2"


def test_months_across_are_an_axis():
    rows = [["Pipe", "Jan-01", "Feb-01", "Mar-01", "Apr-01", "Total"], ["a", 1, 2, 3, 4, 10], ["b", 5, 6, 7, 8, 26], ["c", 1, 1, 1, 1, 4]]
    formulas = [[1, 5, "=SUM(B2:E2)"], [2, 5, "=SUM(B3:E3)"], [3, 5, "=SUM(B4:E4)"]]
    table = _tab(rows, formulas=formulas).tables[0]
    assert [(a.kind, a.positions) for a in table.axes] == [(AXIS_PERIOD, (1, 2, 3, 4))]
    assert table.columns[1].role == PEER and table.columns[5].role == ROW_TOTAL and table.columns[0].role in (KEY, LABEL)


def test_sequence_and_group_axes():
    rows = [["Unit", 1, 2, 3, 4], ["a", 9, 8, 7, 6], ["b", 5, 4, 3, 2], ["c", 6, 2, 3, 4]]
    table = _tab(rows).tables[0]
    assert table.header_rows == (1,) and [a.kind for a in table.axes] == [AXIS_SEQUENCE]
    rows = [["", "Actual", "", ""], ["Pipe", "Gas", "Oil", "Power"], ["a", 2, 1, 3], ["b", 4, 9, 6], ["c", 7, 3, 9]]
    table = _tab(rows, merges=[{"s": {"r": 0, "c": 1}, "e": {"r": 0, "c": 3}}]).tables[0]
    assert [(a.kind, a.name) for a in table.axes] == [(AXIS_GROUP, "Actual")]


def test_stray_cells_are_set_apart_by_kind():
    rows = [["Pipe", "Vol"], ["a", 1], ["b", "#REF!"], ["c", "On Peak"], ["d", "n/a"], ["e", 5], ["f", "Vol"], ["g", "total due"], ["h", "300,000 (MAQ)"], ["i", 9], ["j", 10], ["k", 11], ["l", 12], ["m", 13], ["n", 14]]
    table = _tab(rows).tables[0]
    assert table.columns[1].kind == V_NUMBER and table.columns[0].kind == V_TEXT
    kinds = {s.address: s.kind for s in table.strays}
    assert kinds == {"B3": STRAY_ERROR, "B4": STRAY_LABEL, "B5": STRAY_MARKER, "B7": STRAY_REPEATED_HEADER, "B8": STRAY_TOTAL, "B9": STRAY_NUMBER_WITH_WORD}
    assert table.lines[1].cells[1].stray == STRAY_ERROR and table.lines[1].cells[1].raw == "#REF!"
    assert sum(line.cells[1].value for line in table.lines if line.cells[1].stray is None) == 1 + 5 + 9 + 10 + 11 + 12 + 13 + 14


def test_hidden_lines_and_columns_are_kept_and_flagged():
    model = _tab(CLEAN, columnFormats=CLEAN_FORMATS, hiddenRows=[[2, 2]], hiddenColumns=[[1, 1]])
    table = model.tables[0]
    assert [line.row for line in table.lines] == [2, 3, 4, 5]
    assert [line.hidden for line in table.lines] == [False, True, False, False]
    assert table.columns[1].hidden and not table.columns[0].hidden
    assert model.lines[2].hidden and model.hidden_columns == (2,)


def test_label_value_notes():
    rows = [["Report: Daily"], ["Book:", "West"], ["As of", 36892], [], ["Pipe", "Vol"], ["a", 1], ["b", 2], ["c", 3], [], ["Prepared by: Ann"]]
    model = _tab(rows, columnFormats=[{"f": "General"}, {"f": "General", "x": [[2, "mm/dd/yy"]]}])
    notes = {n.label: n for n in model.notes}
    assert notes["Report"].value.value == "Daily" and notes["Report"].address == "A1"
    assert notes["Book"].value.value == "West"
    assert notes["As of"].value.value == "2001-01-01"
    assert notes["Prepared by"].value.value == "Ann"
    assert _roles(model)[10] == NOTE and model.tables[0].header_rows == (5,)


def test_tab_family_merges_with_the_tab_name_as_a_column():
    def month(name, base):
        return {"name": name, "rows": [["Pipe", "Vol"], ["a", base], ["b", base + 1], ["c", base + 2]]}

    book = compile_workbook(workbook_from_json({"name": "w", "tabs": [month("Jan 01", 1), month("Feb 01", 5), {"name": "Other", "rows": [["X", "Y", "Z"], ["p", "q", 1], ["r", "s", 2]]}]}))
    assert len(book.families) == 1
    family = book.families[0]
    assert family.members == (("Jan 01", 0), ("Feb 01", 0))
    table = family.table
    assert table.columns[0].role == TAB_NAME and [c.name for c in table.columns] == ["Tab", "Pipe", "Vol"]
    assert len(table.lines) == 6
    last = table.lines[5]
    assert last.tab == "Feb 01" and last.cells[0].value == "Feb 01" and last.cells[0].period.start == "2001-02-01"
    assert last.cells[2].value == 7 and last.cells[2].address == "B4"


def test_direct_markers_name_the_header():
    rows = [["Title line", "x"], ["Sub", "y"], ["Pipe", "Vol"], ["a", 1], ["b", 2], ["c", 3]]
    by_filter = _tab(rows, autofilter={"s": {"r": 2, "c": 0}, "e": {"r": 5, "c": 1}})
    assert S_AUTOFILTER_TOP in by_filter.lines[2].signals and by_filter.lines[2].role == HEADER
    by_frozen = _tab(rows, frozenRows=3)
    assert S_FROZEN_LAST in by_frozen.lines[2].signals


def test_style_alone_never_moves_the_first_header():
    rows = [["Pipe", "Vol"], ["a", 1], ["b", 2], ["c", 3], ["d", 4], ["e", 5], ["f", 6], ["g", 7], ["h", 8], ["i", 9], ["j", 10], ["k", 11]]
    model = _tab(rows, rowStyles=[[4, 2, 2, 0, 0]])
    assert model.tables[0].header_rows == (1,)


def test_a_chart_tab_and_empty_tabs_do_not_crash():
    book = compile_workbook(workbook_from_json({"name": "w", "tabs": [{"name": "Chart1", "sheetType": SHEET_CHART}, {"name": "Empty", "rows": []}, {"name": "Blank", "rows": [["", ""], ["", ""]]}, {"name": "One", "rows": [["only"]]}]}))
    assert [t.tables for t in book.tabs[:3]] == [[], [], []]
    assert book.tabs[0].sheet_type == SHEET_CHART and not book.families


def test_origin_shifts_addresses():
    table = _tab([["Pipe", "Vol"], ["a", 1], ["b", 2], ["c", 3]], origin={"r": 4, "c": 2}, formulas=[]).tables[0]
    assert table.header_rows == (5,) and table.columns[0].letter == "C"
    assert table.lines[0].cells[1].address == "D6"


def test_compile_is_deterministic():
    rows = [["Report"], [], ["Pipe", "Jan-01", "Feb-01", "Mar-01"], ["East"], ["a", 1, 2, 3], ["b", "#N/A", 5, 6], ["Total", 5, 7, 9], [], ["Trader", "Desk", "Pnl"], ["x", "d", 1], ["y", "d", 2], ["z", "e", 3]]
    first = repr(_tab(rows, formulas=[[6, 2, "=SUM(C5:C6)"]]))
    assert all(first == repr(_tab(rows, formulas=[[6, 2, "=SUM(C5:C6)"]])) for _ in range(3))


def test_every_fact_traces_to_a_cell():
    rows = [["Pipe", "Vol"], ["East"], ["a", 1], ["b", 2], ["Total", 3]]
    table = _tab(rows, rowStyles=[[1, 1, 1, 0, 0]]).tables[0]
    for line in table.lines + table.printed:
        assert all(cell.address.endswith(str(cell.row)) for cell in line.cells.values())
    assert table.columns[1].header_cells == ("B1",)


def test_header_decision_is_a_sum_over_named_features():
    grid = Grid(tab_from_json({"name": "S", "rows": CLEAN, "columnFormats": CLEAN_FORMATS}))
    features = line_features(line_facts(grid), grid)[1]
    model = load_model()
    assert set(model["steps"]) <= set(FEATURES) and set(features) == set(FEATURES)
    parts = contributions(features)
    assert abs(model["base"] + sum(parts.values()) - header_score(features)) < 1e-9


def test_xlsx_reader_builds_the_wire_payload():
    try:
        import openpyxl
        from openpyxl.styles import Font
    except ImportError:
        print("  SKIP  test_xlsx_reader_builds_the_wire_payload: openpyxl absent")
        return
    from engine.sheet.xlsx_reader import read_workbook

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Pipe", "Date", "Vol"])
    ws["A1"].font = Font(bold=True)
    ws.append(["a", __import__("datetime").datetime(2001, 1, 1), 5])
    ws.append(["b", __import__("datetime").datetime(2001, 1, 2), 6])
    ws.append(["c", __import__("datetime").datetime(2001, 1, 3), 7])
    ws["D4"] = "=SUM(C2:C4)"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = "A1:C4"
    ws.row_dimensions[3].hidden = True
    ws.column_dimensions["B"].hidden = True
    ws.merge_cells("F1:G1")
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "t.xlsx")
        wb.save(path)
        raw = read_workbook(path)
    tab = raw["tabs"][0]
    assert raw["name"] == "t.xlsx" and tab["name"] == "Data" and tab["sheetType"] == "grid"
    assert tab["rows"][1][1] == 36892 and tab["columnFormats"][1]["f"].lower().startswith("yy") and tab["formulas"] == [[3, 3, "=SUM(C2:C4)"]]
    assert tab["frozenRows"] == 1 and tab["autofilter"] == {"s": {"r": 0, "c": 0}, "e": {"r": 3, "c": 2}}
    assert tab["hiddenRows"] == [[2, 2]] and tab["hiddenColumns"] == [[1, 1]]
    assert tab["rowStyles"][0] == [0, 3, 1, 0, 0]
    assert tab["merges"] == [{"s": {"r": 0, "c": 5}, "e": {"r": 0, "c": 6}}]
    book = compile_workbook(workbook_from_json(raw))
    assert book.tabs[0].tables[0].columns[1].role == DATE and book.tabs[0].lines[0].role == HEADER


TESTS = [
    test_number_parser, test_period_parser, test_format_kinds, test_formula_shapes, test_clean_table_comes_out_unchanged, test_title_lines_give_context, test_multi_row_header_joins_with_merges,
    test_total_lines_are_printed_and_never_records, test_total_word_inside_a_name_stays_a_record, test_total_word_on_most_lines_is_not_a_mark, test_running_balance_is_not_a_total, test_bands_give_sections,
    test_sparse_label_column_fills_down_and_traces_to_its_source, test_stacked_tables_split, test_repeated_header_continues_its_table, test_side_by_side_blocks_split, test_spacer_columns_do_not_split_one_table,
    test_repeated_blocks_merge_into_one_family, test_months_across_are_an_axis, test_sequence_and_group_axes, test_stray_cells_are_set_apart_by_kind, test_hidden_lines_and_columns_are_kept_and_flagged,
    test_label_value_notes, test_tab_family_merges_with_the_tab_name_as_a_column, test_direct_markers_name_the_header, test_style_alone_never_moves_the_first_header, test_a_chart_tab_and_empty_tabs_do_not_crash,
    test_origin_shifts_addresses, test_compile_is_deterministic, test_every_fact_traces_to_a_cell, test_header_decision_is_a_sum_over_named_features, test_xlsx_reader_builds_the_wire_payload,
]

if __name__ == "__main__":
    print("=== sheet compiler: one synthetic grid per shape ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as exc:                                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"test_sheet_compile: {len(TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
