"""The sheet compiler on small synthetic grids, one per shape. Hermetic: no model training, no network, no Postgres; the xlsx reader's test skips where openpyxl is absent."""
from __future__ import annotations

import os
import sys
import tempfile

from engine.sheet import compile_tab, compile_workbook, line_roles, names
from engine.sheet.formulas import classify
from engine.sheet.grid import Grid
from engine.sheet.line_facts import line_facts
from engine.sheet.line_model import FEATURES, SECOND_FEATURES, contributions, final_scores, header_score, line_features, load_model, second_features
from engine.sheet.names import (AXIS_GROUP, AXIS_PERIOD, AXIS_SEQUENCE, BAND, COLUMN_ROLES, DATE, F_CURRENCY, F_DATE, F_GENERAL, F_PERCENT, FX_AGG_COLUMN, FX_AGG_ROW, FX_LINK_COLUMN, FX_ROW_ARITH, HEADER, KEY, LABEL, LINE_ROLES,
                                MEASURE, NOTE, P_DAY, P_MONTH, P_QUARTER, P_RANGE, P_RELATIVE, P_SEASON, P_YEAR, PEER, ROW_TOTAL, S_AUTOFILTER_TOP, S_FORMULA_TOTAL, S_FROZEN_LAST, SECTION_LABEL, SHEET_CHART, STRAY_ERROR,
                                STRAY_LABEL, STRAY_MARKER, STRAY_NUMBER_WITH_WORD, STRAY_REPEATED_HEADER, STRAY_TOTAL, SUBTOTAL, TAB_NAME, TITLE, TOTAL, V_NUMBER, V_TEXT)
from engine.sheet.payload import tab_from_json, workbook_from_json
from engine.sheet.values import format_kind, parse_number, parse_period, period_of_serial


def _tab(rows, name="Sheet1", **more):
    return compile_tab(tab_from_json({"name": name, "rows": rows, **more}))


def _roles(model):
    return {line.row: line.role for line in model.lines}


def _tab_heads(rows, heads, **more):
    """A tab compiled with exactly these lines called headers, whatever the line model says: a rule after the model is tested on its own."""
    held, line_roles.header_lines = line_roles.header_lines, lambda grid, facts, printed: {r: [] for r in heads}
    try:
        return _tab(rows, **more)
    finally:
        line_roles.header_lines = held


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


def test_total_word_inside_every_key_is_no_mark():
    rows = [["Customer", "Amt"]] + [[f"C{i} Total", i] for i in range(1, 11)]
    model = _tab(rows)
    table = model.tables[0]
    assert len(table.lines) == 10 and not table.printed
    assert names.S_TOTAL_WORD_IN_KEY in table.lines[0].signals


def test_total_word_beside_a_blank_key_stays_printed():
    # F2: an invoice's word sits in another column with the key blank, on every second line: the records must not add each ticket twice.
    rows = [["Ticket", "Item", "Note", "Amt"]]
    for i in range(1, 10):
        rows += [[f"T{i}", "x", "", i * 10], ["", "", "Ticket Total", i * 10]]
    rows += [["", "", "Total for Dept", 450]]
    table = _tab(rows).tables[0]
    assert sum(line.cells[3].value for line in table.lines) == 450
    assert len(table.printed) == 10 and table.printed[-1].role == TOTAL


def test_grand_total_and_subtotals_that_tie_never_leave():
    # F2: the guard took a grand total with the rest; a key's own word that ties to what is above it prints that sum.
    rows = [["Customer", "Item", "Amt"]]
    for i in range(1, 10):
        rows += [[f"C{i}", "x", i], [f"C{i} Total", "", i]]
    rows += [["Grand Total", "", 45]]
    table = _tab(rows).tables[0]
    assert sum(line.cells[2].value for line in table.lines) == 45 and [line.label for line in table.printed][-1] == "Grand Total"
    rows = [["Customer", "Amt"]] + [[f"C{i} Total", i] for i in range(1, 11)] + [["Grand Total", 55]]
    table = _tab(rows).tables[0]
    assert sum(line.cells[1].value for line in table.lines) == 55 and [line.label for line in table.printed] == ["Grand Total"]


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


def test_a_column_of_one_unit_reads_as_numbers():
    """"1,370 tons", "20 knots" (WikiTableQuestions: "which name has the largest displacement?" could not rank a text
    column): a column whose cells nearly all state a number with one unit reads as numbers of it, each cell showing what
    the sheet wrote. Different words after numbers (addresses) or several units stay text."""
    from engine.program.catalog import Catalog
    from engine.program.semantics import cell_text
    rows = [["Name", "Displacement", "Address", "Result"], ["River", "1,370 tons", "10 Main St", "3 wins"], ["Kaibokan", "870 tons", "22 Oak Ave", "2 losses"],
            ["FMR", "1,200 tons", "5 Pine Rd", "4 draws"], ["Flower", "1 ton", "7 Elm Ct", "1 win"]]
    model = _tab(rows)
    table = model.tables[0]
    shipped, address, result = table.columns[1], table.columns[2], table.columns[3]
    assert (shipped.kind, shipped.unit) == (V_NUMBER, "ton"), (shipped.kind, shipped.unit)
    assert address.kind == V_TEXT and result.kind == V_TEXT
    assert [line.cells[1].value for line in table.lines] == [1370.0, 870.0, 1200.0, 1.0]
    assert [cell_text(line.cells[1]) for line in table.lines] == ["1,370 tons", "870 tons", "1,200 tons", "1 ton"]
    catalog = Catalog.from_model(compile_workbook(workbook_from_json({"name": "ships", "tabs": [{"name": "Sheet1", "rows": rows}]})))
    assert catalog.tables[0].numbers[1] == [1370.0, 870.0, 1200.0, 1.0] and catalog.tables[0].texts[1][0] == "1,370 tons"
    from engine.program.index import index_of        # its texts are still values a question names ("the 870 tons one")
    index = index_of(catalog.tables[0])
    assert index.quantity_columns == {1} and (1, "870 tons") in index.value_words["870"]
    # A scale is not a unit: "$48 million" keeps its column as it was.
    assert _tab([["Film", "Budget"], ["a", "$48 million"], ["b", "$12 million"], ["c", "$7 million"]]).tables[0].columns[1].kind == V_TEXT
    # Fewer than 4 in 5 cells with the unit: the column stays text, the stated numbers its strays as before.
    mixed = _tab([["Name", "Speed"], ["a", "20 knots"], ["b", "fast"], ["c", "21 knots"], ["d", "slow"], ["e", "19 knots"]]).tables[0]
    assert mixed.columns[1].kind == V_TEXT


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


def _every_data_cell_is_held(model):
    """The invariant behind F4: every filled cell on a data line belongs to a table."""
    held = {(cell.row, cell.col) for table in model.tables for line in table.lines + table.printed for cell in line.cells.values()}
    lost = [(line.row, c) for line in model.lines if line.role in (names.RECORD, SUBTOTAL, TOTAL) for c in line.cells if (line.row, c) not in held]
    assert not lost, lost


def test_a_key_left_of_repeated_names_goes_with_every_block():
    # F4: Long Short Long Short make two blocks; the dates left of them fell out of both.
    rows = [["", "Long", "Short", "Long", "Short"]] + [[36892 + i, 5.5 + i, 2.5 * i, 7.25 - i, 9.5 * i] for i in range(6)]
    model = _tab_heads(rows, {1}, columnFormats=[{"f": "mm/dd/yy"}])
    assert len(model.tables) == 2
    assert all(table.columns[0].letter == "A" and table.columns[0].role == DATE for table in model.tables)
    assert [c.letter for c in model.tables[1].columns] == ["A", "D", "E"]
    _every_data_cell_is_held(model)


def test_all_names_alike_are_a_peer_run_not_blocks():
    # F4: 24 hour columns all named HB became 12 two-column tables and the zone, unnamed, fell out.
    rows = [[""] + ["HB"] * 8] + [[f"Z{i}"] + [1.5 * (i + 1) + 3.25 * k for k in range(8)] for i in range(5)]
    model = _tab_heads(rows, {1})
    assert len(model.tables) == 1 and model.tables[0].columns[0].letter == "A" and len(model.tables[0].columns) == 9
    _every_data_cell_is_held(model)


def test_a_column_beyond_the_named_blocks_stays_in_a_table():
    # F4: an unnamed computed column after A B A B, in no table.
    rows = [["Hub", "Vol", "Hub", "Vol", ""]] + [[f"h{i}", i, f"k{i}", i + 1, 1] for i in range(1, 6)]
    model = _tab(rows, formulas=[[r, 4, f"=D{r + 1}-B{r + 1}"] for r in range(1, 6)])
    assert model.tables[-1].columns[-1].letter == "E"
    _every_data_cell_is_held(model)


def test_rule_lines_are_spacers_and_never_names():
    # F6c: lines of dashes were headers, so columns were named "--" and a table split at each one.
    rows = [["Cnterparty", "Deal", "Volume"], ["--------", "-----", "------"], ["a", "x1", 5], ["------", "---", "---"], ["Deal Total", "", 5], ["b", "x2", 6], ["------", "---", "---"], ["Deal Total", "", 6], ["c", "x3", 7], ["d", "x4", 8], ["e", "x5", 9]]
    model = _tab(rows)
    assert len(model.tables) == 1
    table = model.tables[0]
    assert [c.name for c in table.columns] == ["Cnterparty", "Deal", "Volume"]
    assert [line.row for line in table.lines] == [3, 6, 9, 10, 11] and [line.row for line in table.printed] == [5, 8]
    assert model.lines[1].role == names.BLANK and names.S_RULE_LINE in model.lines[1].signals


def test_no_header_inside_the_filter_range():
    # F6a: lines inside the autofilter that read like names opened new tables and named columns from data.
    body = [[f"Plant {i}", f"Tank {i}", 100 + i] for i in range(1, 12)]
    odd = [["Legal description", "Tank number", "Location"]]
    rows = [["Plant", "Tank", "Vol"]] + body[:6] + odd + body[6:]
    held, line_roles.final_scores = line_roles.final_scores, lambda facts, grid, features, model=None: {r: 1.0 if v["text"] == 3 else -1.0 for r, v in features.items()}
    try:
        model = _tab(rows, autofilter={"s": {"r": 0, "c": 0}, "e": {"r": len(rows) - 1, "c": 2}})
        repeat = _tab(rows[:9] + [["Plant", "Tank", "Vol"]] + rows[9:], autofilter={"s": {"r": 0, "c": 0}, "e": {"r": len(rows), "c": 2}})
    finally:
        line_roles.final_scores = held
    assert len(model.tables) == 1 and model.tables[0].header_rows == (1,)
    assert model.lines[7].role != HEADER and [c.name for c in model.tables[0].columns] == ["Plant", "Tank", "Vol"]
    assert len(repeat.tables) == 1 and repeat.tables[0].repeated_header_rows == (10,), "a page's header kept inside the range stays a repeat"


def test_a_data_line_joined_to_a_repeated_header_stays_data():
    # F6b: the block's first line repeats the header, the next is data: the whole block was compared, so a new table opened and read its data line as names.
    head = ["Company", "Plant", "Total MW", "Unit", "City"]
    rows = [head] + [[f"Co{i}", f"P{i}", 100 + i, i, f"C{i}"] for i in range(1, 8)] + [head, ["Tva", "Haywood", 680, 1, "Brown"]] + [[f"Co{i}", f"P{i}", 200 + i, i, f"C{i}"] for i in range(8, 12)]
    model = _tab_heads(rows, {1, 9, 10})
    assert len(model.tables) == 1 and model.tables[0].repeated_header_rows == (9,)
    assert 10 in [line.row for line in model.tables[0].lines] and model.lines[9].role == names.RECORD and names.S_DATA_LINE in model.lines[9].signals


def test_a_lone_data_line_called_a_header_stays_in_its_table():
    # F6b: a data line among records named columns "550" and "340" and split the table.
    head = ["Company", "Plant", "Total MW", "Unit", "City"]
    rows = [head] + [[f"Co{i}", f"P{i}", 100 + i, i, f"C{i}"] for i in range(1, 8)] + [["Dearborn", "Rouge", 550, 2, "Detroit"]] + [[f"Co{i}", f"P{i}", 200 + i, i, f"C{i}"] for i in range(8, 12)]
    model = _tab_heads(rows, {1, 9})
    assert len(model.tables) == 1 and [c.name for c in model.tables[0].columns] == head
    assert 9 in [line.row for line in model.tables[0].lines] and model.lines[8].role == names.RECORD and names.S_DATA_LINE in model.lines[8].signals
    # Styled apart from the records, its cells may fit and it is still a header: a schedule's bold line of a slot number and names.
    styled = _tab_heads(rows, {1, 9}, rowStyles=[[8, 5, 5, 0, 0]])
    assert [t.header_rows for t in styled.tables] == [(1,), (9,)]
    # A real stacked header beneath a table still opens a new table.
    rows = [head] + [[f"Co{i}", f"P{i}", 100 + i, i, f"C{i}"] for i in range(1, 8)] + [["Trader", "Desk", "Pnl"], ["x", "d1", 1], ["y", "d2", 2], ["z", "d3", 3]]
    assert [t.header_rows for t in _tab_heads(rows, {1, 9}).tables] == [(1,), (9,)]


def test_a_later_line_styled_as_the_header_is_a_header():
    # A stacked table's header reads short when its words are as odd as data: the same marks, set off, on the same columns.
    rows = [["5", "John", "Martin", "Jim"]] + [[f"2:{i}0", f"p{i}", f"q{i}", f"r{i}"] for i in range(5)] + [["6", "Keith", "Mike", "Jeff"]] + [[f"3:{i}0", f"s{i}", f"t{i}", f"u{i}"] for i in range(5)]
    styles = [[0, 4, 4, 0, 0], [6, 4, 4, 0, 0]]
    heads = {r: (1.0 if r == 1 else -1.0) for r in range(1, 13)}
    held, line_roles.final_scores = line_roles.final_scores, lambda facts, grid, features, model=None: heads
    try:
        model = _tab(rows, rowStyles=styles)
        plain = _tab(rows)
    finally:
        line_roles.final_scores = held
    assert [t.header_rows for t in model.tables] == [(1,), (7,)]
    assert [t.header_rows for t in plain.tables] == [(1,)]


def test_a_units_line_repeating_over_new_names_opens_a_table():
    # A repeated units line over names of its own is the next table's header, not a repeat with data joined to it.
    rows = [["$/Gj", "$/Gj", "$/Gj"], ["Price A", "Price B", "Price C"]] + [[1.5 + i, 2.5 * i, 3.5] for i in range(5)] + [[], ["$/Gj", "$/Gj", "$/Gj"], ["Risk A", "Risk B", "Risk C"]] + [[4.5 + i, 5.5 * i, 6.5] for i in range(5)]
    model = _tab_heads(rows, {1, 2, 9, 10})
    assert [t.header_rows for t in model.tables] == [(1, 2), (9, 10)]
    assert model.tables[1].columns[0].path == ("$/Gj", "Risk A")


def test_a_total_word_naming_a_column_can_be_a_header():
    # "Date, Total, Grand Total" over the columns it names: printed lines took it, and its columns lost their names.
    rows = [["Date", "Total", 2001, "Grand Total"]] + [[36892 + i, 1.5 + i, 2.5 * i, 3.5] for i in range(6)]
    held, line_roles.final_scores = line_roles.final_scores, lambda facts, grid, features, model=None: {r: 3.0 if r == 1 else -3.0 for r in features}
    try:
        model = _tab(rows, columnFormats=[{"f": "mm/dd/yy"}])
    finally:
        line_roles.final_scores = held
    assert model.tables[0].header_rows == (1,) and model.tables[0].columns[3].name == "Grand Total" and not model.tables[0].printed


def test_a_closing_total_ends_its_table_when_records_resume():
    # Phase 2, 40.2: closed deals came after TOTAL DEALS on the same header, and a sum ran across both.
    rows = [["Deal", "Region", "Value"], ["a", "East", 10], ["b", "West", 20], ["1Q TOTAL", "", 30], ["c", "East", 5], ["d", "West", 7], ["2Q TOTAL", "", 12], ["TOTAL DEALS", "", 42], ["x", "East", 100], ["y", "West", 200], ["z", "East", 300]]
    model = _tab_heads(rows, {1}, formulas=[[3, 2, "=SUM(C2:C3)"], [6, 2, "=SUM(C5:C6)"], [7, 2, "=C4+C7"]])
    assert [t.header_rows for t in model.tables] == [(1,), (1,)]
    assert sum(line.cells[2].value for line in model.tables[0].lines) == 42 and [line.row for line in model.tables[1].lines] == [9, 10, 11]
    assert model.tables[0].printed[-1].label == "TOTAL DEALS" and not model.tables[1].printed
    # A section's own total closes nothing: the sections are one table, down to the grand total.
    rows = [["Deal", "Value"], ["a", 10], ["b", 20], ["A TOTAL", 30], ["c", 5], ["d", 7], ["B TOTAL", 12], ["GRAND TOTAL", 42]]
    model = _tab_heads(rows, {1}, formulas=[[3, 1, "=SUM(B2:B3)"], [6, 1, "=SUM(B5:B6)"], [7, 1, "=B4+B7"]])
    assert len(model.tables) == 1 and len(model.tables[0].lines) == 4


def test_titles_with_one_cell_a_block_part_side_by_side_blocks():
    # Phase 2, 18.2: a sparse title above a repeated header is a band of its own block, with no gutter between the blocks.
    def block(hub, base):
        return [[f"{hub} PURCHASES", "", "", f"{hub} SALES", "", ""], ["", "MW", "PRICE", "", "MW", "PRICE"]] + [[f"c{i}", base + i, 1.5, f"s{i}", base - i, 2.5] for i in range(4)]

    rows = block("COB", 10) + [[]] + block("MIDC", 20) + [[]] + block("PV", 30)
    model = _tab_heads(rows, {1, 2, 8, 9, 15, 16})
    assert len(model.tables) == 2
    left, right = model.tables
    assert [c.letter for c in left.columns] == ["A", "B", "C"] and [c.letter for c in right.columns] == ["D", "E", "F"]
    assert [text for _, text in left.bands] == ["COB PURCHASES", "MIDC PURCHASES", "PV PURCHASES"]
    assert [text for _, text in right.bands] == ["COB SALES", "MIDC SALES", "PV SALES"]
    assert left.lines[-1].section == ("PV PURCHASES",) and right.lines[0].section == ("COB SALES",) and left.columns[1].path == ("MW",)


def test_each_side_by_side_list_has_its_own_bands():
    # Phase 2, 13.1: in lists of accounts under people, the person with an extension heads the accounts under them, per list.
    rows = [["Settlements", "Ext.", "", "Upstream", "Ext."]]
    for i in range(1, 4):
        rows += [[f"Team {i}", 3000 + i, "", f"Person {i}", 4000 + i], ["", "", "", f"Account {i}a", ""], ["", "", "", f"Account {i}b", ""], ["", "", "", f"Account {i}c", ""]]
    model = _tab_heads(rows, {1})
    upstream = model.tables[1]
    assert [text for _, text in upstream.bands] == ["Person 1", "Person 2", "Person 3"]
    assert {line.row: line.section for line in upstream.lines}[7] == ("Person 2",)
    assert not model.tables[0].bands


def test_a_lone_label_between_a_total_and_figures_is_a_band():
    # Phase 2, 31.1: a month alone on its line in the key column, after the last block's total and over the next block's figures, stayed a record.
    rows = [["Item", "Amt", "Net"], [], ["July '00"], ["run", 2.5, 3.5], ["more", 4.5, 5.5], ["", 7.0, 9.0], ["Aug"], ["run", 1.5, 2.5], ["more", 3.5, 4.5], ["", 5.0, 7.0], ["Sep"], ["run", 6.5, 7.5], ["more", 8.5, 9.5]]
    formulas = [[5, 1, "=SUM(B4:B5)"], [5, 2, "=SUM(C4:C5)"], [9, 1, "=SUM(B8:B9)"], [9, 2, "=SUM(C8:C9)"]]
    table = _tab_heads(rows, {1}, formulas=formulas).tables[0]
    assert [text for _, text in table.bands] == ["July '00", "Aug", "Sep"]
    assert {line.row: line.section for line in table.lines}[8] == ("Aug",)


def test_axes_cross_spacers_and_totals_and_reach_flag_columns():
    # Phase 2, 6.1: books under no shared label, a total between them and spacers around: peers no axis stated.
    rows = [["Desk", "Phys", "", "Total", "", "Store", "", "Bridge", "Swap", "Opt", "", "Total"], ["a", 2.5, "", 2.5, "", 1.5, "", 3.5, 4.5, 5.5, "", 15.0], ["b", 3.5, "", 3.5, "", 2.5, "", 4.5, 5.5, 6.5, "", 19.0], ["c", 4.5, "", 4.5, "", 3.5, "", 5.5, 6.5, 7.5, "", 23.0]]
    table = _tab_heads(rows, {1}).tables[0]
    axis = next(a for a in table.axes if a.kind == names.AXIS_TOTALLED)
    assert [table.columns[p].letter for p in axis.positions] == ["B", "F", "H", "I", "J"]
    assert {c.letter: c.role for c in table.columns}["D"] == ROW_TOTAL and {c.letter: c.role for c in table.columns}["L"] == ROW_TOTAL
    # Phase 2, 1.1: agreement columns marked Y and N.
    rows = [["Deal", "Entity", "Master", "Omnibus", "Netting", "Letter", "Date"], ["a", "ENA", "N", "Y", "N", "N", "n/a"], ["b", "EPMI", "N", "Y", "N", "Y", "n/a"], ["c", "ENA", "Y", "Y", "N", "N", "n/a"]]
    table = _tab_heads(rows, {1}).tables[0]
    assert [(a.kind, a.positions) for a in table.axes] == [(names.AXIS_FLAG, (2, 3, 4, 5))]
    # Phase 2, 53.3: a month with nothing in it yet is still a month of the axis.
    rows = [["Day", "JAN", "FEB", "MAR", "APR"], [1, "", "", 8.5, 7.5], [2, "", "", 6.5, 5.5], [3, "", "", 4.5, 3.5]]
    table = _tab_heads(rows, {1}).tables[0]
    assert [(a.kind, a.positions) for a in table.axes] == [(AXIS_PERIOD, (1, 2, 3, 4))]


def test_a_spacer_column_does_not_part_labels_from_their_figures():
    # F7: labels A:C and figures E:H across a spacer, on the same lines, became two tables, so no question could filter by quarter and sum value; line labels were parted from their figures.
    rows = [["Deal", "", "Quarter", "", "Team", "Region", "", "Value"]]
    rows += [[f"D{i}", "", "Q1", "", f"T{i}", "East", "", i * 10] for i in range(1, 7)]
    rows += [["TOTAL DEALS", "", "", "", "", "", "", 210]]
    model = _tab(rows, formulas=[[7, 7, "=SUM(H2:H7)"]])
    assert len(model.tables) == 1
    table = model.tables[0]
    assert [c.name for c in table.columns] == ["Deal", "Quarter", "Team", "Region", "Value"]
    assert table.printed[0].label == "TOTAL DEALS" and table.printed[0].cells[4].value == 210


def test_statistic_lines_under_a_date_key_are_printed():
    # F5: Sum and Average under a date column stayed records: top-row answered the Sum line and a sum doubled.
    rows = [["Dates", "Price", "Volume"]] + [[36892 + i, 2.0 + i, 10.0] for i in range(10)] + [["Sum", 65.0, 100.0], ["Average", 6.5, 10.0], ["Maximum", 11.0, 10.0]]
    model = _tab_heads(rows, {1}, columnFormats=[{"f": "mm/dd/yy"}])
    table = model.tables[0]
    assert len(table.lines) == 10 and sum(line.cells[1].value for line in table.lines) == 65.0
    assert [line.label for line in table.printed] == ["Sum", "Average", "Maximum"]
    assert names.S_STRAY_KEY in table.printed[0].signals and model.lines[11].role in (SUBTOTAL, TOTAL)
    # Under a text key the same lines are records until a word or a formula says otherwise: nothing here reads the words.
    rows = [["Hub", "Price"]] + [[f"h{i}", 2.0 + i] for i in range(10)] + [["Sum", 65.0]]
    assert len(_tab(rows).tables[0].lines) == 11


def test_a_repeated_block_that_differs_opens_a_section():
    # F8: hubs named on repeated header lines went to repeated rows with no section, so every record sat under the first hub's name.
    def block(hub, base):
        return [[f"{hub} PURCHASES", "", ""], ["", "MW", "PRICE"]] + [[f"c{i}", base + i, 1.5] for i in range(4)]

    rows = block("COB", 10) + [[]] + block("MIDC", 20) + [[]] + block("PV", 30)
    table = _tab_heads(rows, {1, 2, 8, 9, 15, 16}).tables[0]
    assert table.repeated_header_rows == (8, 9, 15, 16)
    assert [line.section for line in table.lines if line.row in (3, 10, 17)] == [("COB PURCHASES",), ("MIDC PURCHASES",), ("PV PURCHASES",)]
    assert [c.path for c in table.columns if c.letter in ("B", "C")] == [("MW",), ("PRICE",)], "the first hub is the first block's section, not part of a column's name"
    assert [text for _, text in table.bands] == ["COB PURCHASES", "MIDC PURCHASES", "PV PURCHASES"]


def test_a_repeat_after_a_band_keeps_the_bands_section():
    rows = [["ALPHA"], [], ["Area", "Device", "Amt"]] + [[f"a{i}", f"d{i}", i] for i in range(1, 6)] + [[], ["ESTATE"], [], ["Area", "Device name", "Amt"]] + [[f"e{i}", f"f{i}", i] for i in range(1, 6)]
    styles = [[0, 1, 1, 0, 0], [9, 1, 1, 0, 0]]
    table = _tab_heads(rows, {3, 12}, rowStyles=styles).tables[0]
    assert table.repeated_header_rows == (12,) and {line.section for line in table.lines if line.row > 12} == {("ESTATE",)}


def test_the_lone_title_over_a_header_is_the_first_band():
    # F9: the first section sits above the header and reads as a title, so "the NEWCO section" found nothing.
    rows = [["NEWCO"], [], ["Area", "Amt"]] + [[f"a{i}", i] for i in range(1, 5)] + [["ESTATE"]] + [[f"e{i}", i] for i in range(1, 5)]
    styles = [[0, 1, 1, 0, 0], [7, 1, 1, 0, 0]]
    model = _tab_heads(rows, {3}, rowStyles=styles)
    table = model.tables[0]
    assert [line.section for line in table.lines][:1] == [("NEWCO",)] and table.lines[-1].section == ("ESTATE",)
    assert [text for _, text in table.bands] == ["NEWCO", "ESTATE"]
    assert [t[1] for t in table.context.titles] == ["NEWCO"]
    # Read as the header's upper line by the model, it is still the first band, and names no column.
    both = _tab_heads(rows, {1, 3}, rowStyles=styles).tables[0]
    both_upper = _tab_heads(rows[:1] + rows[2:], {1, 2}, rowStyles=[[0, 1, 1, 0, 0], [6, 1, 1, 0, 0]]).tables[0]
    assert both.lines[0].section == ("NEWCO",) and both_upper.lines[0].section == ("NEWCO",) and both_upper.header_rows == (2,) and [c.name for c in both_upper.columns] == ["Area", "Amt"]
    # A title that looks nothing like the bands is only a title.
    table = _tab_heads(rows, {3}, rowStyles=[[7, 1, 1, 0, 0]]).tables[0]
    assert table.lines[0].section == () and table.lines[-1].section == ("ESTATE",)


def test_text_records_after_a_dated_title_stay_records():
    # F10: a date in a title counted as the table's last figure, so every wordy record under the header became a note in no table.
    rows = [["Report", 36892], [], ["Company", "City"], ["Enron Power Marketing Incorporated of Houston Texas", "Houston"], ["Duke", "Charlotte"], ["Calpine Corporation of San Jose in California", "San Jose"], ["Dynegy", "Houston"], ["Reliant", "Houston"]]
    model = _tab_heads(rows, {3}, columnFormats=[{"f": "General"}, {"f": "mm/dd/yy"}])
    assert model.tables[0].header_rows == (3,) and [line.row for line in model.tables[0].lines] == [4, 5, 6, 7, 8]
    rows = [["Name", "City", "Amt"], ["a", "x", 1], ["b", "y", 2], ["c", "z", 3], ["d", "x", ""], ["e", "y", ""], ["Prepared on the last day of the month by the desk"]]
    model = _tab(rows)
    assert [line.row for line in model.tables[0].lines] == [2, 3, 4, 5, 6] and _roles(model)[7] == NOTE


def test_a_solid_block_ending_early_does_not_fill_down():
    # F3: a column filled on the first lines and blank after gave thousands of lines a value they never had.
    rows = [["ID", "Book", "Desk", "Amt"]] + [[f"i{k}", "P", "D", k] for k in range(1, 6)] + [[f"i{k}", "", "", k] for k in range(6, 30)]
    table = _tab(rows).tables[0]
    assert not any(c.role == SECTION_LABEL for c in table.columns)
    assert sum(1 for line in table.lines if 1 in line.cells) == 5
    rows = [["Book", "ID", "Amt"]] + [["P", f"i{k}", k] for k in range(1, 6)] + [["", f"i{k}", k] for k in range(6, 30)]
    table = _tab(rows).tables[0]
    assert not any(c.role == SECTION_LABEL for c in table.columns) and sum(1 for line in table.lines if 0 in line.cells) == 5


def test_a_sparse_comment_right_of_a_full_key_does_not_fill_down():
    # F3: a comment filled down onto other people's lines.
    rows = [["Name", "Comment", "Amount"], ["a", "check this", 1], ["b", "", 2], ["c", "", 3], ["d", "", 4], ["e", "late", 5], ["f", "", 6], ["g", "", 7], ["h", "", 8]]
    table = _tab(rows).tables[0]
    assert sum(1 for line in table.lines if 1 in line.cells) == 2 and table.columns[1].role != SECTION_LABEL


def test_continuation_lines_take_no_label():
    # F3: a record's further lines hold one text cell; filling its labels made a count off by four.
    rows = [["Org", "Type", "Vol", "Point"]]
    for i in range(1, 9):
        rows += [[f"Org{i}", "P", i * 100, f"pt{i}"], ["", "", "", f"pt{i}b"], ["", "", "", f"pt{i}c"], ["", "", "", f"pt{i}d"]]
    table = _tab(rows).tables[0]
    assert sum(1 for line in table.lines if 1 in line.cells) == 8 and table.columns[1].role != SECTION_LABEL
    filled = _tab([["Book", "Trader", "Amt"], ["West", "a", 1], ["", "b", 2], ["", "c", 3], ["East", "d", 4], ["", "e", 5], ["", "f", 6]]).tables[0]
    assert names.S_FILLED_DOWN in filled.lines[1].signals and names.S_FILLED_DOWN not in filled.lines[0].signals


def test_a_family_says_how_much_its_members_repeat_each_other():
    # F1: one list sorted two ways, and a tab that is the sum of two others, merge on column names; adding across them doubles.
    def tab(name, lines):
        return {"name": name, "rows": [["Deal", "Vol"]] + [[f"d{i}", i] for i in lines]}

    book = compile_workbook(workbook_from_json({"name": "w", "tabs": [tab("All", range(1, 21)), tab("Financial", range(1, 11)), tab("Physical", range(11, 21))]}))
    family = book.families[0]
    assert sum(line.cells[2].value for line in family.table.lines) == 2 * sum(range(1, 21))
    assert family.adds_twice
    by_member = {m.member: m for m in family.overlap}
    assert by_member["All"].repeated == 1.0 and by_member["Financial"].inside == ("All",) and by_member["All"].inside == ()
    # Day tabs of one template merge and do not repeat.
    def day(name, base):
        return {"name": name, "rows": [["Deal", "Vol"]] + [[f"d{i}", base + i] for i in range(12)]}

    book = compile_workbook(workbook_from_json({"name": "w", "tabs": [day("Jan 01", 100), day("Jan 02", 200), day("Jan 03", 300)]}))
    assert len(book.families) == 1 and not book.families[0].adds_twice and book.families[0].overlap[0].repeated == 0.0
    assert len(book.families[0].table.lines) == 36


def test_a_date_from_the_clock_is_no_as_of():
    # F11: =TODAY() read as the table's as-of date: a workbook of 2001 read 2014, and in a live sheet it moves every day.
    assert classify("=TODAY()", 1, 1).kind == names.FX_VOLATILE and classify("=NOW()-1", 1, 1).kind == names.FX_VOLATILE and classify('="as of "&TEXT(TODAY(),"m/d/y")', 1, 1).kind == names.FX_VOLATILE
    assert classify("=DATE(2001,1,1)", 1, 1).kind == "date"
    rows = [["Report", 41886], [], ["Pipe", "Vol"], ["a", 1], ["b", 2], ["c", 3]]
    formats = [{"f": "General"}, {"f": "mm/dd/yy"}]
    table = _tab_heads(rows, {3}, columnFormats=formats, formulas=[[0, 1, "=TODAY()"]]).tables[0]
    assert table.context.as_of is None and table.context.as_of_address == ""
    assert _tab_heads(rows, {3}, columnFormats=formats).tables[0].context.as_of_address == "B1"


def test_an_unchanged_sheet_keeps_its_hash_and_its_model():
    # N11, F11: a clock cell moves the hash daily; a host's budget must not change the model.
    from engine.sheet import payload as wire

    def raw(today, styles=None, **more):
        tab = {"name": "S", "rows": [["Report", today], [], ["Pipe", "Vol"], ["a", 1], ["b", 2], ["c", 3]], "columnFormats": [{"f": "General"}, {"f": "mm/dd/yy"}], "formulas": [[0, 1, "=TODAY()"]]}
        if styles is not None:
            tab["rowStyles"] = styles
        tab.update(more)
        return tab

    assert wire.tab_hash(tab_from_json(raw(41886))) == wire.tab_hash(tab_from_json(raw(45000)))
    assert wire.tab_hash(tab_from_json({**raw(41886), "rows": [["Report", 41886], [], ["Pipe", "Vol"], ["a", 9], ["b", 2], ["c", 3]]})) != wire.tab_hash(tab_from_json(raw(41886)))
    read = tab_from_json(raw(41886, [[2, 2, 2, 0, 0]]))
    none = tab_from_json(raw(41886, [[2, 2, 2, 0, 0]], styleRead=False))
    assert read.style_read and not none.style_read and not none.row_styles and wire.tab_hash(read) != wire.tab_hash(none)
    assert compile_tab(none).style_read is False
    # Above the limit the style sent does not count, so a clock cannot change what a host's reading gives.
    limit, wire.STYLE_CELL_LIMIT = wire.STYLE_CELL_LIMIT, 3
    try:
        big_with = tab_from_json(raw(41886, [[2, 2, 2, 0, 0]]))
        big_without = tab_from_json(raw(41886, [], styleRead=False))
    finally:
        wire.STYLE_CELL_LIMIT = limit
    assert not big_with.style_read and repr(compile_tab(big_with)) == repr(compile_tab(big_without))


def test_an_axis_holds_one_grain_and_no_totals():
    # F12: months and quarters in one axis, a typed total as a peer: top_col always answered a total.
    rows = [["Pipe", "Jan-01", "Feb-01", "Mar-01", "Q1 01", "Apr-01", "May-01", "Jun-01", "Q2 01", "2001"], ["a", 1, 2, 3, 6, 1, 1, 1, 3, 9], ["b", 2, 2, 2, 6, 2, 2, 2, 6, 12], ["c", 3, 3, 3, 9, 3, 3, 3, 9, 18]]
    table = _tab(rows).tables[0]
    assert [(a.kind, a.positions) for a in table.axes] == [(AXIS_PERIOD, (1, 2, 3, 5, 6, 7))]
    assert table.columns[4].role != PEER and table.columns[9].role != PEER
    rows = [["", "Actual", "", "", ""], ["Pipe", "Gas", "Oil", "Power", "Total"], ["a", 2, 1, 3, 6], ["b", 4, 9, 6, 19], ["c", 7, 3, 9, 19]]
    table = _tab_heads(rows, {1, 2}, merges=[{"s": {"r": 0, "c": 1}, "e": {"r": 0, "c": 4}}]).tables[0]
    assert [a.positions for a in table.axes] == [(1, 2, 3)] and table.columns[4].role == ROW_TOTAL
    # A column that adds up the others under no total word.
    rows = [["", "Actual", "", "", ""], ["Pipe", "Gas", "Oil", "Power", "All"], ["a", 2, 1, 3, 6], ["b", 4, 9, 6, 19], ["c", 7, 3, 9, 19]]
    table = _tab_heads(rows, {1, 2}, merges=[{"s": {"r": 0, "c": 1}, "e": {"r": 0, "c": 4}}]).tables[0]
    assert [a.positions for a in table.axes] == [(1, 2, 3)] and table.columns[4].role == ROW_TOTAL


def test_a_group_axis_needs_one_format():
    # F12: currency beside percents under one label read as peers.
    rows = [["", "2001 Actual", "", ""], ["Pipe", "Volume", "Price", "Share"], ["a", 2, 1.5, 0.1], ["b", 4, 2.5, 0.2], ["c", 7, 3.5, 0.3]]
    formats = [{"f": "General"}, {"f": "#,##0"}, {"f": "$#,##0.00"}, {"f": "0%"}]
    table = _tab_heads(rows, {1, 2}, columnFormats=formats, merges=[{"s": {"r": 0, "c": 1}, "e": {"r": 0, "c": 3}}]).tables[0]
    assert not table.axes and all(c.role != PEER for c in table.columns)


def test_a_tall_merge_over_a_number_is_one_figure():
    # N13: a tall merge over a number repeated it on every line, and a sum read it each time.
    rows = [["Pipe", "Item", "Cap"], ["a", "x", 100], ["a", "y", ""], ["a", "z", ""], ["b", "x", 50], ["b", "y", ""], ["b", "z", ""]]
    table = _tab(rows, merges=[{"s": {"r": 1, "c": 2}, "e": {"r": 3, "c": 2}}, {"s": {"r": 4, "c": 2}, "e": {"r": 6, "c": 2}}]).tables[0]
    assert sum(line.cells[2].value for line in table.lines if 2 in line.cells) == 150


def test_a_name_keeps_what_the_sheet_wrote():
    # N3: 7933047 was named 7.93305e+06 and "(1)" became -1, so a question's word no longer met it.
    rows = [["Pipe", 7933047, "(1)"], ["a", 1, 2], ["b", 3, 4], ["c", 5, 6]]
    table = _tab_heads(rows, {1}).tables[0]
    assert [c.name for c in table.columns] == ["Pipe", "7933047", "(1)"]


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
    facts = line_facts(grid)
    every = line_features(facts, grid)
    features = every[1]
    model = load_model()
    assert set(model["steps"]) <= set(FEATURES) and set(features) == set(FEATURES)
    parts = contributions(features)
    assert abs(model["base"] + sum(parts.values()) - header_score(features)) < 1e-9
    # The second reading is one too, over named coordinates of the first reading's scores.
    second = model["second"]
    assert set(second["steps"]) <= set(SECOND_FEATURES)
    values = second_features(facts, grid, {r: header_score(v, model) for r, v in every.items()})[1]
    assert values["score"] == header_score(features) and set(values) == set(SECOND_FEATURES)
    parts = contributions(values, second)
    assert abs(second["base"] + sum(parts.values()) - final_scores(facts, grid, every, model)[1]) < 1e-9


def test_the_line_model_artifact_is_small_pinned_and_retrains_the_same():
    import hashlib
    import json

    from engine.sheet.line_model import ARTIFACT
    from training.sheet.train import expand, fit, ranges

    with open(ARTIFACT, "rb") as source:
        data = source.read()
    assert b"\r" not in data and len(data) < 32_000
    manifest = json.load(open(os.path.join(os.path.dirname(ARTIFACT), "weights_manifest.json"), encoding="utf-8"))
    assert manifest["committed_artifacts"]["sheet_line_model.json"]["sha256"] == hashlib.sha256(data).hexdigest()
    rows = [[float(i % 7), float(i % 3)] for i in range(200)]
    targets = [int(i % 7 == 0) for i in range(200)]
    first, again = (fit(rows, targets, [1.0] * 200, 30, ("a", "b"), least_weight=0.1) for _ in range(2))
    assert first == again and first["steps"]["a"]["cuts"]
    assert expand(ranges({5, 1, 2, 3, 9})) == [1, 2, 3, 5, 9]


def test_training_labels_hold_row_numbers_and_hashes_only():
    import json

    from training.sheet.train import LABELS, expand

    labels = json.load(open(LABELS, encoding="utf-8"))["tabs"]
    assert {t["source"] for t in labels} == {"hand", "markup", "free"} and len(labels) > 1500
    for t in labels[:50] + labels[-50:]:
        assert set(t) == {"file", "tab", "source", "head", "other"} and len(t["file"]) == 16 and len(t["tab"]) == 16
        expand(t["head"]), expand(t["other"])


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
    test_total_lines_are_printed_and_never_records, test_total_word_inside_a_name_stays_a_record, test_total_word_inside_every_key_is_no_mark, test_total_word_beside_a_blank_key_stays_printed,
    test_grand_total_and_subtotals_that_tie_never_leave, test_a_key_left_of_repeated_names_goes_with_every_block, test_all_names_alike_are_a_peer_run_not_blocks, test_a_column_beyond_the_named_blocks_stays_in_a_table,
    test_the_line_model_artifact_is_small_pinned_and_retrains_the_same, test_training_labels_hold_row_numbers_and_hashes_only, test_a_units_line_repeating_over_new_names_opens_a_table, test_a_total_word_naming_a_column_can_be_a_header,
    test_a_closing_total_ends_its_table_when_records_resume, test_titles_with_one_cell_a_block_part_side_by_side_blocks, test_each_side_by_side_list_has_its_own_bands, test_a_lone_label_between_a_total_and_figures_is_a_band, test_axes_cross_spacers_and_totals_and_reach_flag_columns,
    test_a_later_line_styled_as_the_header_is_a_header, test_rule_lines_are_spacers_and_never_names, test_no_header_inside_the_filter_range, test_a_data_line_joined_to_a_repeated_header_stays_data, test_a_lone_data_line_called_a_header_stays_in_its_table,
    test_a_spacer_column_does_not_part_labels_from_their_figures, test_statistic_lines_under_a_date_key_are_printed, test_a_repeated_block_that_differs_opens_a_section, test_a_repeat_after_a_band_keeps_the_bands_section,
    test_the_lone_title_over_a_header_is_the_first_band, test_text_records_after_a_dated_title_stay_records, test_a_solid_block_ending_early_does_not_fill_down, test_a_sparse_comment_right_of_a_full_key_does_not_fill_down,
    test_continuation_lines_take_no_label, test_a_family_says_how_much_its_members_repeat_each_other, test_a_date_from_the_clock_is_no_as_of, test_an_unchanged_sheet_keeps_its_hash_and_its_model,
    test_an_axis_holds_one_grain_and_no_totals, test_a_group_axis_needs_one_format, test_a_tall_merge_over_a_number_is_one_figure, test_a_name_keeps_what_the_sheet_wrote, test_running_balance_is_not_a_total, test_bands_give_sections,
    test_sparse_label_column_fills_down_and_traces_to_its_source, test_stacked_tables_split, test_repeated_header_continues_its_table, test_side_by_side_blocks_split, test_spacer_columns_do_not_split_one_table,
    test_repeated_blocks_merge_into_one_family, test_months_across_are_an_axis, test_sequence_and_group_axes, test_stray_cells_are_set_apart_by_kind, test_a_column_of_one_unit_reads_as_numbers, test_hidden_lines_and_columns_are_kept_and_flagged,
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
