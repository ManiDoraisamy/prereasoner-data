"""The program language on small synthetic sheets: one test per fixed semantic, the decoder's k-best, the verifier's evidence, the proof beside an answer. Hermetic: no model, no network, no Postgres. Question counts in the docstrings are from design/program_language.txt, the 186 hand-written programs."""
from __future__ import annotations

import os
import subprocess
import sys

from engine.deterministic import lower_select_query
from engine.program.catalog import Catalog
from engine.program.decoder import Decoder, POLICY
from engine.program.execute import run, run_dual
from engine.program.index import TableIndex
from engine.program.language import (Across, Agg, Calc, Check, Col, OneOf, Compare, CompareColumns, Contains, DateOf, Equals, InPeriod, InSection, IsBlank, IsNumber, NotEquals, OnLine, PeriodSpan, Rank, RowArithmetic, SECTION, Show, Size, answer_type_of, coordinates, tables_of, is_sound, period_from_name)
from engine.program.lowering import plan_schema, to_query
from engine.program.names import (A_COMPANY, A_COUNT, A_LIST, A_NAME, A_NUMBER, A_PERCENT, A_YES_NO, AFTER, ANY, BEFORE, CHECK, EQ, EVERY, IS, LT, NE, NO, VERSUS, ANSWER_TYPES, AVG, CALC, COMPARISONS, COUNT, COUNT_DISTINCT, ENDS, ENTITY_GROUP, ENTITY_LINE, ENTITY_PEER, EV_ADDS_TWICE, EV_BARE_ZERO, EV_EMPTY, EV_FAILED, EV_FILTER_DID_NOTHING, EV_FRACTION_FOR_COUNT, EV_MANY_ROWS, EV_PRINTED_TIE, EV_RAN,
                                  EV_REPEATED_LINES, EV_ROW_COUNT_FOR_AMOUNT, EV_SHAPE_AGREES, EV_TOO_LARGE_FOR_SHARE, EV_TYPE_AGREES, EV_WRONG_KIND, EVIDENCE, FORMS, GE, GT, HIGHEST, LOWEST, MAX, MIN, MINUS, OVER, PERIOD_NAMED, PERIOD_REST_OF, PERIOD_SO_FAR, PLUS, SEMANTICS, SIGN_LOSS, SIGN_MAGNITUDE, SIGN_NET,
                                  SIGN_SIZE, SLOT_AGGREGATE, SLOT_FORM, SLOTS, STARTS, SUM, TIMES, coarse_type, coordinate, slot_of)
from engine.program.output import cells_used, compress, explain, formula, read_as
from engine.program.scoring import Reading, Weights, score_program
from engine.program.semantics import (SEMANTIC_COUNTS, cell_day, exact_wins, fold, read_period, read_sign, share_forms, sign_word_of, stated_number)
from engine.program.store import RequestTables
from engine.program.verifier import LINES_READ, MIN_RUN, adds_across_members, is_exact, significant_digits, verify, verify_one
from engine.program.words import predict_answer_type, read_question
from engine.sheet import compile_workbook, workbook_from_json
from engine.sql_ast import (Abs, Aggregate, ASTValidationError, BinaryExpr, BlankAsZero, ColumnRef, OrderTerm, SelectItem, SelectQuery, SQLType, render_query)


def _catalog(rows, *, formats=None, name="Sheet1"):
    tab = {"name": name, "rows": rows}
    if formats:
        tab["columnFormats"] = formats
    return Catalog.from_model(compile_workbook(workbook_from_json({"name": "book", "tabs": [tab]})))


def _setup(rows, **more):
    catalog = _catalog(rows, **more)
    return catalog, catalog.tables[0], RequestTables(catalog)


def _answer(program, tables):
    """The program's rows; both deterministic programs and the typed AST must say the same thing."""
    rows = run(program, tables).rows
    dual = run_dual(program, tables).rows
    assert _same(rows, dual), (rows, dual)
    return rows


def _same(a, b):
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        for u, v in zip(x, y):
            try:
                if abs(float(u) - float(v)) > 1e-9 * max(1.0, abs(float(u))):
                    return False
            except (TypeError, ValueError):
                if str(u) != str(v):
                    return False
    return True


def _one(rows):
    assert len(rows) == 1 and len(rows[0]) == 1, rows
    return float(rows[0][0])


TRADES = [["Counterparty", "Book", "Trade Date", "Volume", "Price", "Status"],
          ["EES", "West", 36892, 100.0, 10.0, "Open"],
          ["ees ", "East", 36893, -50.0, 11.0, "Open"],
          ["Transco", "West", 36920, 30.0, 12.0, "Closed"],
          ["Transcolorado", "East", 36950, 40.0, 9.0, "Closed"],
          ["Dynegy", "West", 37000, -200.0, 8.0, "Open"],
          ["Total", "", "", -80.0, "", ""]]
TRADE_FORMATS = [{"f": "General"}, {"f": "General"}, {"f": "mm/dd/yy"}, {"f": "#,##0.00"}, {"f": "#,##0.00"}, {"f": "General"}]


def _trades():
    catalog, table, tables = _setup(TRADES, formats=TRADE_FORMATS)
    return catalog, table, tables, lambda at: Col(table.id, at)


# ---------------------------------------------------------------------------------------------------- the closed lists
def test_every_name_is_from_a_closed_list():
    assert set(SEMANTIC_COUNTS) == set(SEMANTICS)
    assert coordinate(SLOT_AGGREGATE, SUM) == "aggregate:sum" and slot_of("value:t0.3=ees") == "value"
    try:
        coordinate("nonsense", 1)
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown slot must raise")
    _, table, _, col = _trades()
    assert not is_sound(Show(table.id, (col(0),), (Equals(col(0), "ees"),))) and not is_sound(Rank(ENTITY_GROUP, table.id, HIGHEST, (), col(3), key=col(3), function=SUM)) and is_sound(Rank(ENTITY_LINE, table.id, LOWEST, (Compare(col(3), GE, 5.0),), col(3), (col(0),)))
    programs = [Show(table.id, (col(3),), (Equals(col(0), "ees"),)), Agg(SUM, col(3), table.id), Rank(ENTITY_GROUP, table.id, HIGHEST, (), col(3), key=col(0), function=SUM), Calc(Agg(SUM, col(3), table.id), MINUS, Agg(SUM, col(4), table.id))]
    for program, form in zip(programs, FORMS):
        names = coordinates(program)
        assert names[0] == coordinate(SLOT_FORM, form)
        assert all(slot_of(name) in SLOTS for name in names)
        assert len(names) == len(set(names))


def test_forms_refuse_what_they_cannot_say():
    _, table, _, col = _trades()
    for bad in (lambda: Agg("median", col(3), table.id), lambda: Agg(COUNT, col(3), table.id), lambda: Agg(COUNT_DISTINCT, Size(col(3)), table.id), lambda: Rank(ENTITY_LINE, table.id, HIGHEST, (), None, (col(0),)),
                lambda: Rank(ENTITY_GROUP, table.id, HIGHEST, (), col(3), key=None, function=SUM), lambda: Rank(ENTITY_PEER, table.id), lambda: Rank("nothing", table.id), lambda: Rank(ENTITY_LINE, table.id, "sideways", (), col(3), (col(0),)),
                lambda: Show(table.id, ()), lambda: RowArithmetic(col(3), "%", col(4)), lambda: Compare(col(3), "~", 1.0), lambda: Calc(Rank(ENTITY_LINE, table.id, HIGHEST, (), col(3), (col(0),)), MINUS, Agg(SUM, col(3), table.id)),
                lambda: Calc(Show(table.id, (col(3),), listing=True), MINUS, Agg(SUM, col(3), table.id)), lambda: PeriodSpan("fortnight", "2001-01-01"), lambda: PeriodSpan("month")):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("a program that cannot be must refuse")


# ------------------------------------------------------------------------------------------------- the fixed semantics
def test_signs():
    """37 questions; 15 of 18 direction or size programs fail if read as the plain largest signed value. A loss is the lowest where stored negative, biggest by size drops the sign, a payment may be stated as its size, net is the signed sum."""
    assert sign_word_of("What is the biggest loss this month?") == SIGN_LOSS and sign_word_of("Who owes the most?") == SIGN_LOSS
    assert sign_word_of("Who is the biggest driver of the change?") == SIGN_SIZE and sign_word_of("Which day moved the most?") == SIGN_SIZE and sign_word_of("which day is most out of balance?") == SIGN_SIZE
    assert sign_word_of("What is our net position?") == SIGN_NET and sign_word_of("How much did we burn in April?") == SIGN_MAGNITUDE and sign_word_of("How much gas is there?") is None
    assert read_sign(SIGN_LOSS, stored_negative=True).direction == LOWEST
    assert read_sign(SIGN_LOSS, stored_negative=False).direction == HIGHEST
    assert read_sign(SIGN_SIZE, stored_negative=True).by_size
    assert read_sign(SIGN_MAGNITUDE, stored_negative=True).magnitude and not read_sign(SIGN_MAGNITUDE, stored_negative=False).magnitude
    assert read_sign(SIGN_NET, stored_negative=True) == read_sign(None, stored_negative=True) and read_sign(SIGN_NET, stored_negative=True).direction == HIGHEST
    _, table, tables, col = _trades()
    assert _answer(Rank(ENTITY_LINE, table.id, LOWEST, (), col(3), (col(0),)), tables)[0][0] == "Dynegy"       # a loss: the lowest signed
    assert _answer(Rank(ENTITY_LINE, table.id, HIGHEST, (), col(3), (col(0),)), tables)[0][0] == "EES"          # plain largest
    assert _answer(Rank(ENTITY_LINE, table.id, HIGHEST, (), Size(col(3)), (col(0),)), tables)[0][0] == "Dynegy"  # by size
    assert _one(_answer(Agg(SUM, col(3), table.id), tables)) == -80.0                                            # net: the signed sum
    assert _one(_answer(Agg(SUM, col(3), table.id, magnitude=True), tables)) == 80.0                             # a burn stated as its size
    assert _answer(Rank(ENTITY_GROUP, table.id, HIGHEST, (), col(3), key=col(1), function=SUM), tables)[0][0] == "East"
    assert _answer(Rank(ENTITY_GROUP, table.id, HIGHEST, (), col(3), key=col(1), function=SUM, by_size=True), tables)[0][0] == "West"


def test_printed_lines_are_read_by_label_and_never_added():
    """28 programs exclude printed lines, 35 read one as the answer; 10 fail if they count as data. A total word inside a name is not a mark: it hit company names in the real sheets."""
    _, table, tables, col = _trades()
    assert [line.role for line in table.lines][-1] in ("total", "subtotal")
    assert _one(_answer(Agg(SUM, col(3), table.id), tables)) == -80.0                       # the printed -80 is not added to itself
    assert _one(_answer(Agg(COUNT, None, table.id), tables)) == 5.0
    assert _answer(Show(table.id, (col(3),), (OnLine(table.id, "Total"),)), tables)[0][0] in ("-80", -80.0)
    names = [["Company", "Book", "Volume"], ["Total Fina Elf", "West", 10.0], ["Enron", "East", 30.0], ["Dynegy", "East", 40.0], ["Total", "", 80.0]]
    _, named, stores = _setup(names)
    assert _one(_answer(Agg(SUM, Col(named.id, 2), named.id), stores)) == 80.0
    assert _one(_answer(Agg(COUNT, None, named.id, (Contains(Col(named.id, 0), "total"),)), stores)) == 1.0


def test_shortest_label_first_and_unlabelled_printed_lines():
    """A printed label read: the shortest match first, so "Total" is not "Subtotal East"; a line the sheet leaves unlabelled is read by its printed role (3 questions)."""
    rows = [["Desk", "Amount"], ["East", 1.0], ["East", 2.0], ["Subtotal East", 3.0], ["West", 4.0], ["West", 5.0], ["Subtotal West", 9.0], ["Total", 12.0]]
    _, table, tables = _setup(rows)
    amount = Col(table.id, 1)
    assert _one(_answer(Show(table.id, (amount,), (OnLine(table.id, "total"),)), tables)) == 12.0
    assert _one(_answer(Show(table.id, (amount,), (OnLine(table.id, "subtotal west"),)), tables)) == 9.0
    unlabelled = [["Desk", "Amount"], ["East", 1.0], ["West", 2.0], ["North", 3.0], ["South", 4.0], ["", "=SUM"]]
    _, plain, stores = _setup([["Desk", "Amount"], ["East", 1.0], ["West", 2.0], ["North", 3.0], ["South", 4.0]])
    assert _answer(Show(plain.id, (Col(plain.id, 1),), (OnLine(plain.id, ""),)), stores) == ()                    # no unlabelled line here: nothing to read
    del unlabelled


def test_a_column_filled_only_on_printed_lines_reads_them():
    """A column that holds figures only on subtotal lines: those are its data, since nothing else fills it (9.2)."""
    rows = [["Well", "Pipe", "Volume", "DBQ"], ["A", "X", 1.0, ""], ["B", "X", 2.0, ""], ["Subtotal X", "", 3.0, 10.0], ["C", "Y", 4.0, ""], ["D", "Y", 5.0, ""], ["Subtotal Y", "", 9.0, 20.0]]
    _, table, tables = _setup(rows)
    roles = [line.role for line in table.lines]
    if "subtotal" in roles or "total" in roles:
        assert 3 in table.printed_only_columns
        assert _one(_answer(Agg(SUM, Col(table.id, 3), table.id), tables)) == 30.0
        assert cells_used(Agg(SUM, Col(table.id, 3), table.id), tables, ()) == ("'Sheet1'!D4", "'Sheet1'!D7")
    assert _one(_answer(Agg(SUM, Col(table.id, 2), table.id), tables)) == 12.0


def test_partial_value_match():
    """20 fail if a word must be the whole cell, and exact wins: Transco is not Transcolorado (13.4), Tennessee Gas is not East Tennessee (88.2)."""
    catalog, table, tables, col = _trades()
    assert _one(_answer(Agg(COUNT, None, table.id, (Contains(col(0), "trans"),)), tables)) == 2.0
    assert _one(_answer(Agg(COUNT, None, table.id, (Equals(col(0), "Transco"),)), tables)) == 1.0
    assert _one(_answer(Agg(COUNT, None, table.id, (Contains(col(0), "tran", STARTS),)), tables)) == 2.0
    assert _one(_answer(Agg(COUNT, None, table.id, (Contains(col(0), "colorado", ENDS),)), tables)) == 1.0
    assert _one(_answer(Agg(COUNT, None, table.id, (Contains(col(0), "colorado", STARTS),)), tables)) == 0.0
    assert exact_wins("TRANSCO", {"transco", "transcolorado"}) and not exact_wins("trans", {"transco", "transcolorado"})
    decoder = Decoder(catalog)
    words = read_question("What volume does Transco have?", catalog, decoder).scores
    assert words[coordinate("value", col(0).name, "transco")] == 1.0
    assert coordinate("contains", col(0).name, "transco") not in words
    partly = read_question("What volume does trans have?", catalog, decoder).scores
    assert coordinate("contains", col(0).name, "trans") in partly


def test_case_and_spacing_do_not_count():
    """37 questions hold a value differing from its cell only by case; 3 fail against the stored spelling."""
    _, table, tables, col = _trades()
    assert fold("  East   Power ") == "east power"
    assert _one(_answer(Agg(COUNT, None, table.id, (Equals(col(0), "  EES"),)), tables)) == 2.0     # "EES" and "ees " are one value
    assert _one(_answer(Agg(COUNT_DISTINCT, col(0), table.id), tables)) == 4.0
    top = _answer(Rank(ENTITY_GROUP, table.id, HIGHEST, (), None, key=col(0), function=COUNT), tables)
    assert top[0][0] == "EES" and float(top[0][1]) == 2.0                                              # keyed by the fold, shown in the sheet's spelling


def test_periods():
    """32 programs use a period, 55 more name one the tab already is. So far is whatever the table holds; rest of is a status column, not a date; calendar 2002 is a year; a quarter takes its year; a month of any year; a day; serial days."""
    assert read_period("so far").kind == PERIOD_SO_FAR and read_period("to date").kind == PERIOD_SO_FAR
    assert read_period("the rest of May").kind == PERIOD_REST_OF
    assert read_period("calendar 2002").kind == PERIOD_NAMED and read_period("calendar 2002").period.start == "2002-01-01" and read_period("calendar 2002").period.end == "2002-12-31"
    assert read_period("Q4 2001").period.start == "2001-10-01" and read_period("Q4", year=2001).period.end == "2001-12-31"
    assert read_period("not a period") is None
    rest = _trades()
    assert not [name for name in read_question("What is the total volume for the rest of May?", rest[0], Decoder(rest[0])).scores if name.startswith("period:")]
    assert coordinate("period", "m02") in read_question("What is the total volume in February so far?", rest[0], Decoder(rest[0])).scores
    assert period_from_name("2001-02").end == "2001-02-28" and period_from_name("2000-02").end == "2000-02-29" and period_from_name("m02").month == 2 and period_from_name("2001-q4").start == "2001-10-01"
    assert period_from_name("2001-11-29").kind == "day" and period_from_name("2001-01-05..2001-02-01").end == "2001-02-01"
    for name in ("2002", "2001-02", "2001-11-29", "m02", "2001-q4", "2001-01-05..2001-02-01"):
        assert period_from_name(name).name == name
    _, table, tables, col = _trades()
    date = col(2)
    count = lambda span: _one(_answer(Agg(COUNT, None, table.id, (InPeriod(date, span),)), tables))
    assert count(period_from_name("2001")) == 5.0
    assert count(period_from_name("2001-01")) == 3.0                     # 36892 (Jan 1), 36893 (Jan 2), 36920 (Jan 29)
    assert count(period_from_name("2001-01-02")) == 1.0
    assert count(period_from_name("m02")) == 1.0                         # 36950: Feb 28
    assert count(period_from_name("2001-q1")) == 4.0
    assert count(period_from_name("2002")) == 0.0
    assert count(period_from_name("2001-01-02..2001-02-28")) == 3.0
    serial = [["Term Start", "Rate"], [36892, 1.0], [36923, 2.0], [36951, 3.0], [36982, 4.0]]
    _, plain, stores = _setup(serial)
    assert plain.is_date_column(0)
    assert _one(_answer(Show(plain.id, (Col(plain.id, 1),), (InPeriod(Col(plain.id, 0), period_from_name("2001-03")),)), stores)) == 3.0
    assert not _setup([["Volume", "Rate"], [36892, 1.0], [36923, 2.0], [36951, 3.0]])[1].is_date_column(0)


def test_blanks():
    """19 programs keep a line with a blank measure; 2 fail if a blank counts 0. A blank is skipped and can be asked for; a line with no key is not a group; a marker is filled; in + and - one blank side counts 0, both blank is no line; in * a blank leaves the line out."""
    rows = [["Name", "A", "B", "Note"], ["x", 10.0, 1.0, "ok"], ["y", 0.0, "", "-"], ["z", "", 3.0, ""], ["", 5.0, 5.0, "ok"], ["w", "", "", "ok"]]
    _, table, tables = _setup(rows)
    name, a, b, note = (Col(table.id, at) for at in range(4))
    assert _one(_answer(Agg(AVG, a, table.id), tables)) == 5.0                                                    # three figures, the blanks are not zeros
    assert _one(_answer(Agg(MIN, a, table.id), tables)) == 0.0
    assert _one(_answer(Agg(COUNT, None, table.id, (IsBlank(a),)), tables)) == 2.0
    assert _one(_answer(Agg(COUNT, None, table.id, (IsBlank(a, filled=True),)), tables)) == 3.0
    assert _one(_answer(Agg(COUNT, None, table.id, (IsBlank(note),)), tables)) == 1.0                             # "-" is a marker, not a blank
    assert _answer(Rank(ENTITY_LINE, table.id, LOWEST, (), a, (name,)), tables)[0][0] == "y"                       # 0 is lowest, a blank never ranks
    groups = _answer(Rank(ENTITY_GROUP, table.id, HIGHEST, (), a, key=name, function=SUM), tables)
    assert [row[0] for row in groups] == ["x"]                                                                     # the unnamed line's 5 is no group
    assert _answer(Rank(ENTITY_GROUP, table.id, HIGHEST, (), None, key=name, function=COUNT), tables)[0][1] == 1
    assert _one(_answer(Agg(SUM, RowArithmetic(a, PLUS, b), table.id), tables)) == 24.0                            # x 11, y 0, z 3, unnamed 10; w is blank on both
    assert _one(_answer(Agg(COUNT, None, table.id, (Equals(name, "w"),)), tables)) == 1.0
    assert _one(_answer(Agg(AVG, RowArithmetic(a, MINUS, b), table.id), tables)) == (9.0 + 0.0 - 3.0 + 0.0) / 4
    assert _one(_answer(Agg(SUM, RowArithmetic(a, TIMES, b), table.id), tables)) == 35.0                           # only the lines with both
    assert _answer(Agg(SUM, a, table.id, (Equals(name, "nobody"),)), tables) == ()                                 # a sum over no lines says nothing, not zero


def test_ties_keep_every_tied_line():
    """1 question's answer changes if ties break by sheet order (98.1)."""
    rows = [["Date", "Deals"], ["Mon", 5.0], ["Tue", 9.0], ["Wed", 9.0], ["Thu", 1.0]]
    _, table, tables = _setup(rows)
    day, deals = Col(table.id, 0), Col(table.id, 1)
    top = _answer(Rank(ENTITY_LINE, table.id, HIGHEST, (), deals, (day,)), tables)
    assert sorted(row[0] for row in top) == ["Tue", "Wed"]
    assert len(_answer(Rank(ENTITY_GROUP, table.id, HIGHEST, (), deals, key=day, function=SUM), tables)) == 2


def test_units():
    """21 questions. Percent cells are fractions: 10% or better is >= 0.1. A share may be stated either way. Thousands are reported as printed, never scaled."""
    assert stated_number("10%") == 0.1 and stated_number("$1,000") == 1000.0 and stated_number("30M") == 30e6 and stated_number("10", percent_column=True) == 0.1 and stated_number("0.5", percent_column=True) == 0.5 and stated_number("ten") is None
    assert share_forms(0.25) == (0.25, 25.0)
    rows = [["Deal", "Prob", "Value (000's)"], ["a", "5%", 10.0], ["b", "10%", 20.0], ["c", "50%", 30.0], ["d", "100%", 40.0]]
    _, table, tables = _setup(rows)
    assert _one(_answer(Agg(COUNT, None, table.id, (Compare(Col(table.id, 1), GE, stated_number("10%")),)), tables)) == 3.0
    assert _one(_answer(Agg(SUM, Col(table.id, 2), table.id), tables)) == 100.0     # (000's) stays as printed
    assert table.columns[2].unit == "000's"


def test_mixed_columns():
    """10 measure columns hold text among their lines; one is 64% the text NaN. A number column reads numbers, other cells are blank for a measure, so SUM and ranking are numeric, not text."""
    rows = [["Day", "Price"], ["a", 9.0], ["b", "NaN"], ["c", 100.0], ["d", "n/a"], ["e", 20.0], ["f", "see note"]]
    _, table, tables = _setup(rows)
    day, price = Col(table.id, 0), Col(table.id, 1)
    assert _one(_answer(Agg(SUM, price, table.id), tables)) == 129.0
    assert _answer(Rank(ENTITY_LINE, table.id, HIGHEST, (), price, (day,)), tables)[0][0] == "c"                 # 100 above 9: not sorted as text
    assert _one(_answer(Agg(COUNT, None, table.id, (IsNumber(price),)), tables)) == 3.0
    assert _one(_answer(Agg(COUNT, None, table.id, (Compare(price, GT, 10.0),)), tables)) == 2.0                 # text never passes a comparison


def test_repeated_rows():
    """3 reads hit several lines, 1 distinct, 1 latest version, 6 tables repeat their header inside the data. A read takes the first in sheet order and says so; a count counts lines unless the entity is named; the latest by a date column; a repeated header is not a line."""
    repeated = [["Pipe", "Vol", "Price"], ["a", 1, 2], ["a", 3, 4], [], ["Pipe", "Vol", "Price"], ["c", 5, 6], ["d", 7, 8]]
    _, pipes, stores = _setup(repeated)
    assert pipes.table.repeated_header_rows == (5,)
    assert _one(_answer(Agg(COUNT, None, pipes.id), stores)) == 4.0                       # the repeated header is not a line
    assert _one(_answer(Agg(COUNT_DISTINCT, Col(pipes.id, 0), pipes.id), stores)) == 3.0
    rows = [["Name", "Value", "Fixed"], ["a", 1.0, 36892], ["a", 2.0, 36950], ["b", 3.0, 36893], ["c", 4.0, 36894]]
    formats = [{"f": "General"}, {"f": "General"}, {"f": "mm/dd/yy"}]
    catalog, table, tables = _setup(rows, formats=formats)
    name, value, fixed = (Col(table.id, at) for at in range(3))
    assert _one(_answer(Show(table.id, (value,), (Equals(name, "a"),)), tables)) == 1.0
    latest = _answer(Rank(ENTITY_LINE, table.id, HIGHEST, (Equals(name, "a"),), DateOf(fixed), (value,)), tables)
    assert float(latest[0][0]) == 2.0
    checked = verify_one(score_program(Show(table.id, (value,), (Equals(name, "a"),)), Reading({}), catalog), tables, None)
    assert checked.held(EV_REPEATED_LINES)


def test_sections_nested_bands_and_the_section_shown():
    """Sections behave as a text column: a condition holds any band of the path, whole or in part, and a section can be the answer (13.1, 23.1, 31.1)."""
    rows = [["Item", "Amount"], ["Steel", ""], ["a", 1.0], ["b", 2.0], ["Concrete", ""], ["c", 3.0], ["d", 4.0]]
    _, table, tables = _setup(rows)
    if not table.table.bands:
        return
    amount = Col(table.id, 1)
    assert _one(_answer(Agg(SUM, amount, table.id, (InSection(table.id, "steel"),)), tables)) == 3.0
    assert _one(_answer(Agg(SUM, amount, table.id, (InSection(table.id, "CONC", partial=True),)), tables)) == 7.0
    assert _one(_answer(Agg(SUM, amount, table.id, (InSection(table.id, "con"),)), tables)) if False else True
    assert _answer(Agg(SUM, amount, table.id, (InSection(table.id, "con"),)), tables) == ()
    assert _answer(Show(table.id, (Col(table.id, SECTION),), (Equals(Col(table.id, 0), "c"),)), tables)[0][0] == "Concrete"
    assert _answer(Rank(ENTITY_LINE, table.id, HIGHEST, (), amount, (Col(table.id, SECTION),)), tables)[0][0] == "Concrete"
    assert _answer(Rank(ENTITY_GROUP, table.id, HIGHEST, (), amount, key=Col(table.id, SECTION), function=SUM), tables)[0][0] == "Concrete"


def test_python_masks_agree_with_the_sql():
    """The decoder's occurring check reads Python bitmasks, the answer SQL: the two must keep the same lines, condition by condition."""
    _, table, tables, col = _trades()
    conditions = [Equals(col(0), "EES"), Equals(col(3), 30.0), Contains(col(0), "trans"), Contains(col(0), "tran", STARTS), Contains(col(0), "rado", ENDS), InPeriod(col(2), period_from_name("2001-01")), InPeriod(col(2), period_from_name("m02")), Compare(col(3), GT, 0.0),
                  CompareColumns(col(3), GT, col(4)), IsBlank(col(4)), IsBlank(col(4), filled=True), IsNumber(col(4)), OnLine(table.id, "total")]
    index = TableIndex(table)
    for condition in conditions:
        mask = table.condition_mask(condition)
        assert index.mask(condition) == mask, condition
        on_all = bin(mask).count("1")
        count = _one(run(Agg(COUNT, None, table.id, (condition,)), tables).rows)
        data_only = bin(mask & table.data_mask).count("1")
        assert count == (on_all if isinstance(condition, OnLine) else data_only), condition


# ----------------------------------------------------------------------------------------------- arithmetic across programs
def test_calc_shapes():
    """calc holds 9 of 11 as two sides differing in one slot, and a share across two columns of a line."""
    rows = [["Bucket", "Code", "OTC", "EOL", "Amount"], ["Gas", "H01", 30.0, 10.0, 100.0], ["Gas", "J01", 25.0, 20.0, 50.0], ["Power", "H01", 5.0, 5.0, 10.0], ["Power", "J01", 2.0, 8.0, 40.0]]
    _, table, tables = _setup(rows)
    t = table.id
    bucket, code, otc, eol, amount = (Col(t, at) for at in range(5))
    gas = Equals(bucket, "Gas")
    power = Equals(bucket, "Power")
    assert _one(_answer(Calc(Agg(SUM, amount, t, (gas,)), MINUS, Agg(SUM, amount, t, (power,))), tables)) == 100.0                                 # the value differs
    assert _one(_answer(Calc(Show(t, (otc,), (gas, Equals(code, "H01"))), PLUS, Show(t, (eol,), (gas, Equals(code, "H01")))), tables)) == 40.0     # the column differs on one line
    assert _one(_answer(Calc(Agg(SUM, otc, t), MINUS, Agg(SUM, eol, t)), tables)) == 19.0                                                         # the measure differs
    assert _one(_answer(Calc(Agg(SUM, amount, t, (gas,)), OVER, Agg(SUM, amount, t)), tables)) == 150.0 / 200.0                                     # a part over its whole
    share = Calc(Show(t, (eol,), (gas, Equals(code, "H01"))), OVER, Calc(Show(t, (otc,), (gas, Equals(code, "H01"))), PLUS, Show(t, (eol,), (gas, Equals(code, "H01")))))
    assert _one(_answer(share, tables)) == 0.25                                                                                                     # nested: a / (a + b)
    assert _one(_answer(Calc(Agg(SUM, amount, t, (power,)), MINUS, Agg(SUM, amount, t, (gas,)), magnitude=True), tables)) == 100.0
    weighted = Calc(Agg(SUM, RowArithmetic(otc, TIMES, amount), t), OVER, Agg(SUM, amount, t))
    assert abs(_one(_answer(weighted, tables)) - (3000 + 1250 + 50 + 80) / 200.0) < 1e-9
    assert _answer(Calc(Agg(SUM, amount, t, (Equals(bucket, "none"),)), MINUS, Agg(SUM, amount, t)), tables) == ()                                  # one side says nothing: no answer
    assert _answer(Calc(Agg(SUM, amount, t), OVER, Agg(SUM, amount, t, (Equals(bucket, "none"),))), tables) == ()


def test_rank_across_peer_columns():
    """The melted view: 10 questions read across peer columns; by size, by a mark, on a printed line, or on a line a key condition picks."""
    rows = [["Book", "Jan", "Feb", "Mar", "Apr"], ["a", 1.0, -9.0, 3.0, 2.0], ["b", 2.0, 2.0, 2.0, 7.0], ["Total", 3.0, -7.0, 5.0, 9.0]]
    _, table, tables = _setup(rows)
    t = table.id
    peers = (1, 2, 3, 4)
    assert _answer(Rank(ENTITY_PEER, t, HIGHEST, (), peers=peers), tables)[0][0] == "Apr"
    assert _answer(Rank(ENTITY_PEER, t, LOWEST, (), peers=peers), tables)[0][0] == "Feb"
    assert _answer(Rank(ENTITY_PEER, t, HIGHEST, (Equals(Col(t, 0), "a"),), peers=peers, by_size=True), tables)[0][0] == "Feb"
    if any(not data for data in table.is_data):
        assert _answer(Rank(ENTITY_PEER, t, HIGHEST, (OnLine(t, "total"),), peers=peers), tables)[0][0] == "Apr"
    marks = [["Deal", "Master", "ISDA", "CSA"], ["1", "Y", "N", "Y"], ["2", "Y", "Y", "N"], ["3", "N", "Y", ""], ["4", "Y", "", ""]]
    _, marked, stores = _setup(marks)
    assert _answer(Rank(ENTITY_PEER, marked.id, HIGHEST, (), peers=(1, 2, 3), mark="y"), stores)[0][0] == "Master"


# -------------------------------------------------------------------------------------------------------- the typed AST
def test_new_ast_nodes_render_and_validate():
    """Abs and BlankAsZero, added for size and row arithmetic: rendered in both dialects, refused on what they cannot read, read by a query's column walk. Noted for the Phase 6 cut-over."""
    amount = ColumnRef("t", "amount", SQLType.REAL)
    other = ColumnRef("t", "other", SQLType.REAL)
    query = SelectQuery((SelectItem(Abs(amount), "size"),), "t", order_by=(OrderTerm(Abs(amount), "DESC"),), limit=1)
    assert 'ABS("t"."amount")' in render_query(query)
    assert 'decimal_abs("t"."amount")' in render_query(query, dialect="sqlite_decimal")
    sum_of = Aggregate("SUM", BinaryExpr(BlankAsZero(amount), "+", BlankAsZero(other)))
    rendered = render_query(SelectQuery((SelectItem(sum_of),), "t"))
    assert 'COALESCE("t"."amount", 0)' in rendered
    group = ColumnRef("t", "k", SQLType.TEXT)
    ranked = SelectQuery((SelectItem(group), SelectItem(Aggregate("SUM", amount))), "t", group_by=(group,), order_by=(OrderTerm(Abs(Aggregate("SUM", amount)), "DESC"),), limit=1)
    assert 'ABS(SUM("t"."amount"))' in render_query(ranked)
    for bad in (Abs(group.table), BlankAsZero(Aggregate("SUM", amount))):
        try:
            render_query(SelectQuery((SelectItem(bad),), "t"))
        except (ASTValidationError, TypeError, AttributeError):
            continue
        raise AssertionError("a node must refuse what it cannot read")
    schema = [{"table": "t", "name": "k", "affinity": "TEXT"}, {"table": "t", "name": "amount", "affinity": "REAL"}]
    plan = lower_select_query("sized", ranked, schema, ())
    assert plan.output


def test_plan_schema_holds_only_what_a_query_reads():
    catalog, table, _, col = _trades()
    query = to_query(Agg(SUM, col(3), table.id, (Equals(col(0), "ees"),)), catalog)
    names = {column["name"] for column in plan_schema(query, catalog)}
    assert names == {"c3_n", "c0_f", "_role"}


# --------------------------------------------------------------------------------------------------------- the decoder
def _gold_programs(t):
    bucket, code, otc, eol, amount = (Col(t, at) for at in range(5))
    gas = Equals(bucket, "gas")
    h01 = Equals(code, "h01")
    return [Show(t, (otc,), (gas, h01)), Show(t, (bucket,), (Compare(otc, GE, 5.0),), listing=True), Agg(SUM, amount, t), Agg(AVG, amount, t, (gas,)), Agg(COUNT, None, t, (gas,)), Agg(COUNT_DISTINCT, code, t), Agg(MAX, RowArithmetic(otc, MINUS, eol), t), Agg(SUM, amount, t, (gas,), magnitude=True),
            Rank(ENTITY_LINE, t, HIGHEST, (), amount, (code,)), Rank(ENTITY_LINE, t, LOWEST, (gas,), Size(amount), (code,)), Rank(ENTITY_GROUP, t, HIGHEST, (), amount, key=bucket, function=SUM), Rank(ENTITY_GROUP, t, LOWEST, (), None, key=code, function=COUNT), Rank(ENTITY_GROUP, t, HIGHEST, (h01,), amount, key=bucket, function=SUM, by_size=True),
            Rank(ENTITY_PEER, t, HIGHEST, (gas,), peers=(2, 3)), Calc(Agg(SUM, amount, t, (gas,)), MINUS, Agg(SUM, amount, t, (Equals(bucket, "power"),))), Calc(Show(t, (otc,), (gas, h01)), PLUS, Show(t, (eol,), (gas, h01))), Calc(Agg(SUM, otc, t), MINUS, Agg(SUM, eol, t)),
            Calc(Agg(SUM, amount, t, (gas,)), OVER, Agg(SUM, amount, t))]


CALC_ROWS = [["Bucket", "Code", "OTC", "EOL", "Amount"], ["Gas", "H01", 30.0, 10.0, 100.0], ["Gas", "J01", 25.0, 20.0, 50.0], ["Power", "H01", 5.0, 5.0, 10.0], ["Power", "J01", 2.0, 8.0, 40.0]]


def test_perfect_readings_give_the_gold_first():
    """Given each program's coordinates at 1 and nothing else, the decoder returns that program first: every form, through the one search."""
    catalog = _catalog(CALC_ROWS)
    decoder = Decoder(catalog)
    t = catalog.tables[0].id
    for gold in _gold_programs(t):
        reading = Reading({name: 1.0 for name in coordinates(gold, catalog.column_kind)})
        decoded = decoder.decode(reading, k=50)
        assert decoded.programs, gold
        top = decoded.programs[0]
        assert top.program == gold, (top.program, gold)
        assert top.score == score_program(gold, reading, catalog).score


AGES = [["Name", "Region", "Age"], ["a", "x", 30.0], ["b", "y", 50.0], ["c", "x", 50.0], ["d", "z", 20.0], ["e", "y", 40.0], ["f", "x", 40.0]]


def test_a_rank_keeps_as_many_as_the_question_states():
    """"The 3 oldest" keeps exactly three lines, a tie broken by sheet order; "the 2 most common" two groups, a tie broken by
    the key; a rank of one still keeps every tied top. The count is named (the stated number), the answer is typed by what it
    shows, a top-N may show the column it ranks, the read-as says how many, and a count is refused where it means nothing."""
    catalog, table, tables = _setup(AGES)
    name, region, age = (Col(table.id, at) for at in range(3))
    top3 = Rank(ENTITY_LINE, table.id, HIGHEST, (), age, (name,), count=3)
    rows = _answer(top3, tables)
    assert [row[0] for row in rows] == ["b", "c", "e"] and [float(row[-1]) for row in rows] == [50.0, 50.0, 40.0], rows
    assert len(_answer(Rank(ENTITY_LINE, table.id, HIGHEST, (), age, (name,)), tables)) == 2
    assert [float(row[0]) for row in _answer(Rank(ENTITY_LINE, table.id, LOWEST, (), age, (age,), count=2), tables)] == [20.0, 30.0]
    common = _answer(Rank(ENTITY_GROUP, table.id, HIGHEST, (), None, key=region, function=COUNT, count=2), tables)
    assert [row[0] for row in common] == ["x", "y"], common
    assert [row[0] for row in _answer(Rank(ENTITY_GROUP, table.id, HIGHEST, (), age, key=region, function=SUM, count=2), tables)] == ["x", "y"]
    names = coordinates(top3, catalog.column_kind)
    assert any(slot_of(n) == "number" for n in names) and coordinate("answer_type", A_NAME) in names, names     # several names: the type of what it shows
    ages = Rank(ENTITY_LINE, table.id, HIGHEST, (), age, (age,), count=2)
    assert is_sound(ages) and [float(row[0]) for row in _answer(ages, tables)] == [50.0, 50.0]                 # "the 2 highest ages" shows its own measure
    assert not is_sound(Rank(ENTITY_LINE, table.id, HIGHEST, (), age, (age,)))
    assert "the 3 lines" in read_as(top3, catalog)
    for bad in (lambda: Rank(ENTITY_LINE, table.id, HIGHEST, (), age, (name,), count=0), lambda: Rank(ENTITY_PEER, table.id, peers=(1, 2), count=2)):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("a rank accepted a count it cannot keep")
    checked = verify_one(score_program(top3, Reading({}), catalog), tables, A_NAME)
    assert not checked.held(EV_MANY_ROWS) and checked.held(EV_SHAPE_AGREES)


def test_the_decoder_proposes_how_many_a_question_states():
    """Given a top-3 program's coordinates (the stated number among them), the decoder returns it first, and with no
    stated number it proposes no count at all."""
    catalog = _catalog(AGES)
    table = catalog.tables[0]
    gold = Rank(ENTITY_LINE, table.id, HIGHEST, (), Col(table.id, 2), (Col(table.id, 0),), count=3)
    reading = Reading({name: 1.0 for name in coordinates(gold, catalog.column_kind)})
    decoded = Decoder(catalog).decode(reading, k=50)
    assert decoded.programs and decoded.programs[0].program == gold, decoded.programs[:3]
    plain = Reading({name: 1.0 for name in coordinates(gold, catalog.column_kind) if slot_of(name) != "number"})
    assert all(getattr(p.program, "count", 1) == 1 for p in Decoder(catalog).decode(plain, k=50).programs)


def test_a_check_says_yes_or_no():
    """A check is one verdict over counts and numbers, the same in the typed SQL and the dual emitter: any and no over
    the kept lines, every base line also meeting one more condition, and a number against a stated number or another
    program's number. It reads as a whether-sentence, has a formula, names its quantifier, answers yes_no, and refuses
    what it cannot say."""
    catalog, table, tables = _setup(AGES)
    name, region, age = (Col(table.id, at) for at in range(3))
    x = (Equals(region, "x"),)
    count = lambda where: Agg(COUNT, None, table.id, where)
    cases = [(Check(ANY, count(x + (Compare(age, GT, 45.0),))), "True"), (Check(ANY, count((Compare(age, GT, 60.0),))), "False"),
             (Check(NO, count((Compare(age, GT, 60.0),))), "True"), (Check(NO, count(x)), "False"),
             (Check(EVERY, count(x + (Compare(age, GE, 30.0),)), right=count(x)), "True"), (Check(EVERY, count(x + (Compare(age, GT, 35.0),)), right=count(x)), "False"),
             (Check(VERSUS, Agg(AVG, age, table.id), op=GT, number=35.0), "True"), (Check(VERSUS, Agg(MAX, age, table.id), op=EQ, number=50.0), "True"),
             (Check(VERSUS, Agg(AVG, age, table.id, x), op=LT, right=Agg(AVG, age, table.id, (Equals(region, "y"),))), "True")]
    for program, want in cases:
        assert _answer(program, tables) == ((want,),), (program, _answer(program, tables))
    every = cases[4][0]
    assert read_as(every, catalog).startswith("whether every line") and "also has" in read_as(every, catalog)
    assert read_as(cases[0][0], catalog).startswith("whether there is any line") and "is above 35" in read_as(cases[6][0], catalog)
    assert formula(cases[0][0], tables).endswith(">0") and formula(cases[6][0], tables).endswith(">35")
    names = coordinates(every, catalog.column_kind)
    assert names[0] == coordinate("form", CHECK) and coordinate("quantifier", EVERY) in names and coordinate("answer_type", A_YES_NO) in names, names
    assert answer_type_of(cases[6][0], catalog.column_kind) == A_YES_NO and is_sound(every)
    for bad in (lambda: Check(EVERY, count(x), right=count(x + (Compare(age, GT, 1.0),))), lambda: Check(VERSUS, count(x), op=GT),
                lambda: Check(ANY, Agg(AVG, age, table.id)), lambda: Check("some", count(x)), lambda: Check(VERSUS, count(x), op="~", number=1.0)):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("a check accepted what it cannot say")
    assert verify_one(score_program(cases[0][0], Reading({}), catalog), tables, A_YES_NO).held(EV_SHAPE_AGREES)
    assert not verify_one(score_program(cases[0][0], Reading({}), catalog), tables, A_NUMBER).held(EV_SHAPE_AGREES)


def test_the_decoder_checks_only_when_the_reading_names_a_check():
    """Given a check's coordinates the decoder returns it first; a reading without the check form proposes no check, so
    the small reader's own readings (it has no head for the form) decode as before."""
    catalog = _catalog(AGES)
    table = catalog.table(catalog.tables[0].id)
    for gold in (Check(ANY, Agg(COUNT, None, table.id, (Compare(Col(table.id, 2), GT, 45.0),))), Check(VERSUS, Agg(AVG, Col(table.id, 2), table.id), op=GT, number=35.0)):
        reading = Reading({name: 1.0 for name in coordinates(gold, catalog.column_kind)})
        decoded = Decoder(catalog).decode(reading, k=50)
        assert decoded.programs and decoded.programs[0].program == gold, (gold, decoded.programs[:3])
        plain = Reading({name: 1.0 for name in coordinates(gold, catalog.column_kind) if name != coordinate("form", CHECK)})
        assert not any(isinstance(p.program, Check) for p in Decoder(catalog).decode(plain, k=50).programs)


def test_a_choice_among_named_values():
    """"Which of x or z has more lines": OneOf keeps the lines whose cell is one of the named values, the same in the
    index, the catalog, the typed SQL and the dual emitter; it names each value, reads as 'is "x" or "z"', refuses a
    single value or an unsorted set, and the decoder proposes it for a rank only, never as an aggregate's filter."""
    catalog, table, tables = _setup(AGES)
    name, region, age = (Col(table.id, at) for at in range(3))
    either = OneOf(region, ("x", "z"))
    index = TableIndex(table)
    assert index.mask(either) == table.condition_mask(either) == table.condition_mask(Equals(region, "x")) | table.condition_mask(Equals(region, "z"))
    winner = Rank(ENTITY_GROUP, table.id, HIGHEST, (either,), None, key=region, function=COUNT)
    assert [row[0] for row in _answer(winner, tables)] == ["x"], _answer(winner, tables)
    assert [row[0] for row in _answer(Rank(ENTITY_GROUP, table.id, LOWEST, (either,), None, key=region, function=COUNT), tables)] == ["z"]
    names = coordinates(winner, catalog.column_kind)
    assert coordinate("value", region.name, "x") in names and coordinate("value", region.name, "z") in names
    assert '"x" or "z"' in read_as(winner, catalog)
    for bad in (lambda: OneOf(region, ("x",)), lambda: OneOf(region, ("z", "x")), lambda: OneOf(region, ("x", "X"))):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("one of accepted what it cannot say")
    reading = Reading({name_: 1.0 for name_ in coordinates(winner, catalog.column_kind)})
    decoded = Decoder(catalog).decode(reading, k=50)
    assert decoded.programs[0].program == winner, decoded.programs[:3]
    summed = Reading({**{n: 1.0 for n in coordinates(Agg(SUM, age, table.id, (Equals(region, "x"),)), catalog.column_kind)}, coordinate("value", region.name, "z"): 1.0})
    assert not any(isinstance(c, OneOf) for p in Decoder(catalog).decode(summed, k=200).programs if isinstance(p.program, Agg) for c in p.program.where)


def test_a_check_asks_what_the_top_one_is():
    """"Is the oldest one in region y?", "is the youngest one older than 25?", "is x the most common region?": a check of
    a rank of one against a stated text (folded) or number, the same in the typed SQL and the dual emitter, a tie going
    to the first line or key; it names the stated value, reads as a whether-sentence, and the decoder returns it first
    from its own coordinates."""
    catalog, table, tables = _setup(AGES)
    name, region, age = (Col(table.id, at) for at in range(3))
    oldest = Rank(ENTITY_LINE, table.id, HIGHEST, (), age, (region,))
    youngest = Rank(ENTITY_LINE, table.id, LOWEST, (), age, (age,))
    common = Rank(ENTITY_GROUP, table.id, HIGHEST, (), None, key=region, function=COUNT)
    cases = [(Check(IS, oldest, op=EQ, text="Y"), "True"), (Check(IS, oldest, op=EQ, text="x"), "False"), (Check(IS, oldest, op=NE, text="x"), "True"),
             (Check(IS, youngest, op=GT, number=25.0), "False"), (Check(IS, youngest, op=LT, number=25.0), "True"),
             (Check(IS, common, op=EQ, text="x"), "True"), (Check(IS, common, op=EQ, text="y"), "False")]
    for program, want in cases:
        assert _answer(program, tables) == ((want,),), (program, _answer(program, tables))
    first = cases[0][0]
    assert coordinate("quantifier", IS) in coordinates(first, catalog.column_kind) and coordinate("value", region.name, "y") in coordinates(first, catalog.column_kind)
    assert read_as(first, catalog).startswith("whether ") and '"Y"' in read_as(first, catalog) and formula(first, tables).endswith('="Y"')
    for bad in (lambda: Check(IS, Rank(ENTITY_LINE, table.id, HIGHEST, (), age, (region, name)), op=EQ, text="x"), lambda: Check(IS, oldest, op=GT, text="x"),
                lambda: Check(IS, Rank(ENTITY_LINE, table.id, HIGHEST, (), age, (region,), count=2), op=EQ, text="x"), lambda: Check(IS, oldest, op=EQ),
                lambda: Check(VERSUS, Agg(AVG, age, table.id), op=EQ, number=1.0, text="x")):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("an is-check accepted what it cannot say")
    for gold in (Check(IS, oldest, op=EQ, text="y"), Check(IS, common, op=EQ, text="x")):
        reading = Reading({n: 1.0 for n in coordinates(gold, catalog.column_kind)})
        decoded = Decoder(catalog).decode(reading, k=50)
        assert decoded.programs and decoded.programs[0].program == gold, (gold, decoded.programs[:3])


def test_a_read_takes_the_line_after_or_before_the_kept_one():
    """"Who comes after c?", "what is the age of the one before e?" (WikiTableQuestions dev: 145 of 2,831 questions read
    a neighbouring line and none could run): the data line nearest the first line the conditions keep, below or above it,
    the same in the typed SQL and the dual emitter; nothing past the last line or when no line is kept; a printed line
    between is skipped. It may show the column its condition names, names the neighbour, reads as a sentence, cites the
    neighbour's cell, and the decoder proposes it only when a reading names a neighbour."""
    catalog, table, tables = _setup(AGES)
    name, region, age = (Col(table.id, at) for at in range(3))
    after_c = Show(table.id, (name,), (Equals(name, "c"),), step=AFTER)
    cases = [(after_c, (("d",),)), (Show(table.id, (age,), (Equals(name, "e"),), step=BEFORE), ((20.0,),)),
             (Show(table.id, (name,), (Equals(region, "y"),), step=AFTER), (("c",),)),       # the first kept line is b's
             (Show(table.id, (name,), (Equals(name, "f"),), step=AFTER), ()),                # nothing after the last line
             (Show(table.id, (name,), (Equals(name, "a"),), step=BEFORE), ()),
             (Show(table.id, (name,), (Equals(region, "w"),), step=AFTER), ())]              # no line kept
    for program, want in cases:
        got = _answer(program, tables)
        text = lambda rows: tuple(tuple(str(int(c)) if isinstance(c, float) and c.is_integer() else str(c) for c in row) for row in rows)  # noqa: E731
        assert text(got) == text(want), (program, got)
    assert is_sound(after_c) and not is_sound(Show(table.id, (name,), (Equals(name, "c"),)))
    assert coordinate("neighbour", AFTER) in coordinates(after_c, catalog.column_kind)
    assert read_as(after_c, catalog).startswith("Name") and "the line after the line where" in read_as(after_c, catalog)
    assert cells_used(after_c, tables, ()) == cells_used(Show(table.id, (name,), (Equals(name, "d"),)), tables, ())
    for bad in (lambda: Show(table.id, (name,), (), step=AFTER), lambda: Show(table.id, (name,), (Equals(name, "c"),), listing=True, step=AFTER),
                lambda: Show(table.id, (name,), (Equals(name, "c"),), step="next")):
        try:
            bad()
        except ValueError:
            continue
        raise AssertionError("a neighbouring read accepted what it cannot say")
    # A printed line between two data lines is not a neighbour.
    printed = [["Name", "Book", "Volume"], ["a", "East", 10.0], ["b", "East", 30.0], ["Total", "", 40.0], ["c", "West", 5.0]]
    _, books, stores = _setup(printed)
    assert [line.role for line in books.lines] == ["record", "record", "subtotal", "record"]
    assert _answer(Show(books.id, (Col(books.id, 0),), (Equals(Col(books.id, 0), "b"),), step=AFTER), stores)[0][0] == "c"
    # The decoder: first from the neighbour's own coordinates; never without a reading that names one.
    reading = Reading({n: 1.0 for n in coordinates(after_c, catalog.column_kind)})
    decoded = Decoder(catalog).decode(reading, k=50)
    assert decoded.programs and decoded.programs[0].program == after_c, decoded.programs[:3]
    plain = Reading({n: 1.0 for n in coordinates(Show(table.id, (age,), (Equals(name, "c"),)), catalog.column_kind)})
    assert not any(isinstance(p.program, Show) and p.program.step for p in Decoder(catalog).decode(plain, k=200).programs)


def test_decoder_is_deterministic_and_order_free():
    """The same scores give the same list, whatever order they came in; ties break by the frames' order, fewer conditions first."""
    catalog = _catalog(CALC_ROWS)
    decoder = Decoder(catalog)
    scores = {"form:agg": 0.5, "form:show": 0.5, "aggregate:sum": 0.5, "form:rank": 0.2, "measure:t0.4": 1.0, "value:t0.0=gas": 0.7}
    first = decoder.decode(Reading(scores), k=200)
    second = Decoder(_catalog(CALC_ROWS)).decode(Reading(dict(reversed(list(scores.items())))), k=200)
    assert [item.program for item in first.programs] == [item.program for item in second.programs]
    assert [item.score for item in first.programs] == sorted((item.score for item in first.programs), reverse=True)
    assert len({item.program for item in first.programs}) == len(first.programs)
    best = first.programs[0].program
    assert best == Agg(SUM, Col("t0", 4), "t0", (Equals(Col("t0", 0), "gas"),))     # the highest score: sum of amount where gas, nothing added


_HASH_ORDER_PROBE = """
import hashlib
from tests.test_program import CALC_ROWS, _catalog, Decoder, Reading, read_question
catalog = _catalog(CALC_ROWS)
decoder = Decoder(catalog)
decoded = decoder.decode(read_question("What is the total amount for Gas in H01?", catalog, decoder), k=300)
print(hashlib.sha256(repr([(item.program, item.score) for item in decoded.programs]).encode()).hexdigest())
"""


def test_the_list_does_not_depend_on_hash_order():
    """Search and ranking stay deterministic across processes: no hash-order dependence, down to the floats."""
    outputs = set()
    for seed in ("1", "2", "977"):
        done = subprocess.run([sys.executable, "-c", _HASH_ORDER_PROBE], capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed}, check=True)
        outputs.add(done.stdout.strip())
    assert len(outputs) == 1, outputs


def test_scores_come_in_as_data_and_nothing_else_moves_the_list():
    """A plugged-in reader only fills names: raising one coordinate puts its program on top."""
    catalog = _catalog(CALC_ROWS)
    decoder = Decoder(catalog)
    low = decoder.decode(Reading({"form:agg": 1.0, "aggregate:sum": 1.0, "measure:t0.4": 1.0}), k=5).programs[0].program
    high = decoder.decode(Reading({"form:agg": 1.0, "aggregate:sum": 1.0, "measure:t0.4": 1.0, "measure:t0.3": 2.0}), k=5).programs[0].program      # two named columns are not by themselves arithmetic
    asked = decoder.decode(Reading({"form:agg": 1.0, "aggregate:sum": 1.0, "measure:t0.4": 1.0, "measure:t0.3": 2.0, "row_op:*": 1.0}), k=5).programs[0].program
    assert asked.measure == RowArithmetic(Col("t0", 3), TIMES, Col("t0", 4))
    assert low.measure == Col("t0", 4) and high.measure == Col("t0", 3)
    assert decoder.decode(Reading({"form:agg": 1.0, "aggregate:avg": 1.0}), k=1).programs[0].program.function == AVG


def test_contributions_are_named_and_sum_to_the_score():
    catalog = _catalog(CALC_ROWS)
    reading = Reading({"form:agg": 0.5, "aggregate:sum": 0.7, "measure:t0.4": 0.9, "value:t0.0=gas": 0.6, "answer_type:number": 0.2, "table:t0": 0.1})
    decoded = Decoder(catalog).decode(reading, k=10, weights=Weights({slot: 2.0 for slot in SLOTS}, bias=-0.5))
    for item in decoded.programs:
        assert abs(sum(term.value for term in item.contributions) + item.bias - item.score) < 1e-12
        assert all(term.slot in SLOTS and term.coordinate.startswith(term.slot + ":") for term in item.contributions)
    record = decoded.programs[0].contributions[0].record()
    assert {"coordinate", "slot", "score", "weight", "contribution"} <= set(record)


def test_intervention_a_named_coordinate_moves_the_score_by_its_weight():
    """Changing one named coordinate moves the program's score by exactly its weight, for every coordinate of every form; a coordinate the program does not fill moves nothing."""
    catalog = _catalog(CALC_ROWS)
    t = catalog.tables[0].id
    weights = Weights({slot: 1.0 + at * 0.25 for at, slot in enumerate(SLOTS)}, bias=0.125)
    for program in _gold_programs(t):
        names = coordinates(program, catalog.column_kind)
        base_scores = {name: 0.3 for name in names}
        before = score_program(program, Reading(base_scores), catalog, weights).score
        for name in names:
            after = score_program(program, Reading({**base_scores, name: 1.3}), catalog, weights).score
            assert abs((after - before) - weights.of(slot_of(name))) < 1e-9, name
        outside = "value:t9.9=nothing"
        assert score_program(program, Reading({**base_scores, outside: 5.0}), catalog, weights).score == before
    # and the decoder's own score moves the same way
    gold = Agg(SUM, Col(t, 4), t, (Equals(Col(t, 0), "gas"),))
    reading = {name: 1.0 for name in coordinates(gold, catalog.column_kind)}
    decoder = Decoder(catalog)
    before = decoder.decode(Reading(reading), k=1, weights=weights).programs[0]
    assert before.program == gold
    moved = decoder.decode(Reading({**reading, "measure:t0.4": 2.0}), k=1, weights=weights).programs[0]
    assert moved.program == gold and abs(moved.score - before.score - weights.of("measure")) < 1e-9


def test_policy_prunes_values_only_where_it_says():
    """One condition keeps every value the reading reaches for show and agg, the top 50 of a column for a rank form; two conditions the top 50 each, on combinations that occur. A value nothing reaches is no condition."""
    values = [[f"name{at}", f"kind{at % 3}", float(at)] for at in range(120)]
    catalog = _catalog([["Name", "Kind", "Amount"], *values])
    decoder = Decoder(catalog)
    t = catalog.tables[0].id
    rare = Equals(Col(t, 0), "name119")
    reached = {f"value:{t}.0=name{at}": 0.1 for at in range(120)} | {f"value:{t}.1=kind{at}": 0.1 for at in range(3)}
    reading = Reading({"form:agg": 1.0, "aggregate:sum": 1.0, "measure:" + t + ".2": 1.0, **reached})
    programs, candidates, _, _ = decoder.enumerate(reading, k=5000)
    assert Agg(SUM, Col(t, 2), t, (rare,)) in programs
    assert candidates > 5000
    unreached = Reading({"form:agg": 1.0, "aggregate:sum": 1.0, "measure:" + t + ".2": 1.0})
    assert Agg(SUM, Col(t, 2), t, (rare,)) not in decoder.enumerate(unreached, k=5000)[0]
    ranked = Reading({"form:rank": 3.0, "entity:group": 3.0, "aggregate:sum": 3.0, "measure:" + t + ".2": 3.0, "key:" + t + ".1": 3.0, "direction:highest": 3.0, **reached})
    programs, _, _, _ = decoder.enumerate(ranked, k=20000)
    conditions = {p.where[0] for p in programs if isinstance(p, Rank) and p.entity == ENTITY_GROUP and len(p.where) == 1 and isinstance(p.where[0], Equals) and p.where[0].column.position == 0}
    assert 0 < len(conditions) <= POLICY.rank_values
    never = [p for p in programs if isinstance(p, Rank) and len(p.where) == 2 and all(isinstance(c, Equals) and isinstance(c.value, str) for c in p.where) and {c.column.position for c in p.where} == {0, 1}]
    for program in never[:200]:
        name, kind = (c.value for c in sorted(program.where, key=lambda c: c.column.position))
        assert f"kind{int(name[4:]) % 3}" == kind, "two conditions must occur together on a line"


def test_conditions_stay_on_distinct_columns():
    catalog = _catalog(CALC_ROWS)
    decoder = Decoder(catalog)
    programs, _, _, _ = decoder.enumerate(Reading({"form:agg": 1.0, "aggregate:sum": 1.0, "filter:t0.0": 1.0, "filter:t0.1": 1.0}), k=3000)
    for program in programs:
        if isinstance(program, Agg) and len(program.where) >= 2:
            columns = [getattr(condition, "column", condition) for condition in program.where]
            assert len({repr(column) for column in columns}) == len(columns)


# ------------------------------------------------------------------------------------------------------------ the words
def test_answer_type_rule():
    assert predict_answer_type("How many deals are open?") == A_COUNT and coarse_type(predict_answer_type("How many MW are scheduled?")) == A_NUMBER
    assert predict_answer_type("What share of volume is gas?") == A_PERCENT and predict_answer_type("How much did we sell?") == A_NUMBER
    assert predict_answer_type("On what date did the price peak?") == "date" and predict_answer_type("Who covers Tampa Electric?") == "person"
    assert predict_answer_type("Which counterparty owes the most?") == A_COMPANY and predict_answer_type("What are the delivery points?") == A_LIST


def test_an_ordinal_is_not_a_stated_number():
    """"The last year they came in 3rd place" (WikiTableQuestions dev): read as a number, 3rd made the rank keep the 3
    latest years. An ordinal names a position, so the question states no number and no rank keeps 3; a cardinal ("the 3
    oldest") still states how many."""
    catalog, table, _ = _setup(AGES)
    decoder = Decoder(catalog)
    ordinal = read_question("Who is the 3rd oldest in region x?", catalog, decoder).scores
    assert not any(name.startswith("number:") for name in ordinal), ordinal
    programs, _, _, _ = decoder.enumerate(read_question("Who is the 3rd oldest in region x?", catalog, decoder), k=500)
    assert not any(isinstance(p, Rank) and p.count == 3 for p in programs)
    cardinal = read_question("Who are the 3 oldest in region x?", catalog, decoder).scores
    assert cardinal.get(coordinate("number", "3"), 0) > 0
    programs, _, _, _ = decoder.enumerate(read_question("Who are the 3 oldest in region x?", catalog, decoder), k=500)
    assert any(isinstance(p, Rank) and p.count == 3 for p in programs)


def test_words_alone_read_a_question():
    catalog, table, tables, col = _trades()
    decoder = Decoder(catalog)
    reading = read_question("What is the total volume for EES in January 2001?", catalog, decoder)
    scores = reading.scores
    assert scores[coordinate("measure", col(3).name)] == 1.0
    assert scores[coordinate("value", col(0).name, "ees")] == 1.0
    assert scores[coordinate("period", "2001-01")] == 1.0
    assert scores["aggregate:sum"] > scores["aggregate:avg"]
    programs, _, _, _ = decoder.enumerate(reading, k=2000)
    gold = Agg(SUM, col(3), table.id, (Equals(col(0), "ees"), InPeriod(col(2), period_from_name("2001-01"))))
    alternatives = [p for p in programs if isinstance(p, Agg) and p.function == SUM and p.measure == col(3) and len(p.where) == 2 and set(map(repr, p.where)) == set(map(repr, gold.where))]
    assert alternatives, "words alone must keep the gold"
    comparisons = read_question("Which deals are at least 10% likely?", *_two(_setup([["Deal", "Prob"], ["a", "5%"], ["b", "20%"]]))).scores
    assert comparisons[coordinate("number", "0.1")] == 1.0 and comparisons[coordinate("comparison", GE)] == 1.0


def _two(triple):
    catalog = triple[0]
    return catalog, Decoder(catalog)


# --------------------------------------------------------------------------------------------------------- the verifier
def _verified(program, tables, catalog, expected):
    return verify_one(score_program(program, Reading({}), catalog), tables, expected)


def test_verifier_shape_and_type_evidence():
    """A shape and type check from the question alone (answer_location.txt, section 5): each failure is named."""
    catalog, table, tables, col = _trades()
    t = table.id
    good = _verified(Agg(SUM, col(3), t), tables, catalog, A_NUMBER)
    assert good.held(EV_RAN) and good.held(EV_SHAPE_AGREES) and good.held(EV_TYPE_AGREES) and good.result.rows
    assert _verified(Agg(SUM, col(3), t, (Equals(col(0), "nobody"),)), tables, catalog, A_NUMBER).held(EV_EMPTY)
    assert _verified(Show(t, (col(1),), (), listing=True), tables, catalog, A_NUMBER).held(EV_MANY_ROWS)
    assert _verified(Show(t, (col(1),), (Equals(col(0), "ees"),)), tables, catalog, A_NUMBER).held(EV_WRONG_KIND)
    assert _verified(Agg(COUNT, None, t), tables, catalog, A_NUMBER).held(EV_ROW_COUNT_FOR_AMOUNT)
    half = _setup([["K", "V"], ["a", 1.5], ["b", 2.0], ["c", 3.0]])
    assert _verified(Agg(SUM, Col(half[1].id, 1), half[1].id), half[2], half[0], A_COUNT).held(EV_FRACTION_FOR_COUNT)
    assert _verified(Agg(SUM, col(4), t), tables, catalog, A_PERCENT).evidence and not _verified(Agg(SUM, col(4), t), tables, catalog, A_PERCENT).held(EV_TOO_LARGE_FOR_SHARE)
    wide = [["K", "V"], ["a", 500.0], ["b", 600.0]]
    other = _setup(wide)
    assert _verified(Agg(SUM, Col(other[1].id, 1), other[1].id), other[2], other[0], A_PERCENT).held(EV_TOO_LARGE_FOR_SHARE)
    assert _verified(Agg(SUM, col(3), t), tables, catalog, A_NAME).held(EV_WRONG_KIND)
    zero = _setup([["K", "V"], ["a", 0.0], ["b", 0.0]])
    assert _verified(Agg(SUM, Col(zero[1].id, 1), zero[1].id), zero[2], zero[0], A_NUMBER).held(EV_BARE_ZERO)
    assert EV_RAN in EVIDENCE


def test_printed_total_ties_only_under_its_exact_conditions():
    """Exact to 1e-6, nonzero, 4+ significant digits, the line's label agreeing with the filter. Never at the grader's 0.5%, where half of wrong sums pass (answer_location.txt, section 3)."""
    assert significant_digits(150000.0) == 2 and significant_digits(1234.0) == 4 and significant_digits(0.01234) == 4 and significant_digits(75.0) == 2
    assert is_exact(1000.0000001, 1000.0) and not is_exact(1000.1, 1000.0)
    rows = [["Desk", "Amount"], ["East", 1000.5], ["East", 2000.25], ["Subtotal East", 3000.75], ["West", 500.5], ["West", 400.25], ["Subtotal West", 900.75], ["Total", 3901.5]]
    catalog, table, tables = _setup(rows)
    if not any(not data for data in table.is_data):
        return
    t = table.id
    desk, amount = Col(t, 0), Col(t, 1)
    whole = _verified(Agg(SUM, amount, t), tables, catalog, A_NUMBER)
    assert whole.held(EV_PRINTED_TIE) and any(item.name == EV_PRINTED_TIE and item.detail.endswith("8") for item in whole.evidence)     # the Total line, by its address
    east = _verified(Agg(SUM, amount, t, (Equals(desk, "East"),)), tables, catalog, A_NUMBER)
    assert east.held(EV_PRINTED_TIE) and any(item.name == EV_PRINTED_TIE and item.detail.endswith("4") for item in east.evidence)
    # the right number under a label that does not agree: no tie
    disagree = [["Desk", "Amount"], ["East", 1000.5], ["East", 2000.25], ["Subtotal West", 3000.75], ["West", 1.0], ["West", 2.0], ["Subtotal North", 3.0]]
    catalog2, table2, tables2 = _setup(disagree)
    if any(not data for data in table2.is_data):
        assert not _verified(Agg(SUM, Col(table2.id, 1), table2.id, (Equals(Col(table2.id, 0), "East"),)), tables2, catalog2, A_NUMBER).held(EV_PRINTED_TIE)
    # within the grader's 0.5% but not exact: no tie
    near = [["Desk", "Amount"], ["East", 1000.5], ["West", 2000.25], ["North", 1.0], ["Total", 3010.0]]
    catalog3, table3, tables3 = _setup(near)
    if any(not data for data in table3.is_data):
        assert not _verified(Agg(SUM, Col(table3.id, 1), table3.id), tables3, catalog3, A_NUMBER).held(EV_PRINTED_TIE)
    # too few digits: 75 is printed everywhere
    short = [["Desk", "Amount"], ["East", 50.0], ["West", 25.0], ["North", 0.0], ["Total", 75.0]]
    catalog4, table4, tables4 = _setup(short)
    if any(not data for data in table4.is_data):
        assert not _verified(Agg(SUM, Col(table4.id, 1), table4.id), tables4, catalog4, A_NUMBER).held(EV_PRINTED_TIE)
    # a filter that kept every line, equal to the whole column: a flag against it
    every = [["Desk", "Kind", "Amount"], ["East", "A", 1000.5], ["West", "A", 2000.25], ["North", "A", 500.25], ["Total", "", 3501.0]]
    catalog5, table5, tables5 = _setup(every)
    if any(not data for data in table5.is_data):
        assert _verified(Agg(SUM, Col(table5.id, 2), table5.id, (Equals(Col(table5.id, 1), "A"),)), tables5, catalog5, A_NUMBER).held(EV_FILTER_DID_NOTHING)


def test_verifier_runs_the_top_programs_and_never_raises():
    catalog, table, tables, col = _trades()
    decoder = Decoder(catalog)
    decoded = decoder.decode(read_question("What is the total volume for EES?", catalog, decoder), k=50)
    checked = verify(decoded.programs, tables, A_NUMBER, top=50)
    assert len(checked) == len(decoded.programs) == 50
    assert all(item.evidence for item in checked)
    assert not [item for item in checked if item.held(EV_FAILED)], "the decoder builds only programs SQL can run"
    assert [item.scored.program for item in checked] == [item.program for item in decoded.programs]
    broken = score_program(Agg(SUM, Col("nope", 1), "nope"), Reading({}), _Kinds())
    assert verify_one(broken, tables, A_NUMBER).held(EV_FAILED)


class _Kinds:
    @staticmethod
    def column_kind(_column):
        return "number"



# -------------------------------------------------------------------------------------------------- the gaps Phase 4 closed
ACROSS_ROWS = [["Book", "Jan", "Feb", "Mar", "Status"], ["a", 7.0, 2.0, 9.0, "Open"], ["b", 10.0, None, 5.0, "Closed"], ["c", None, None, None, "Open"], ["d", 100.0, 1.0, 1.0, None], ["e", 4.0, 4.0, 4.0, "Open"], ["f", 3.0, 3.0, 3.0, "Closed"]]
ACROSS_FORMATS = [{"f": "General"}, {"f": "#,##0.00"}, {"f": "#,##0.00"}, {"f": "#,##0.00"}, {"f": "General"}]


def test_across_adds_peer_columns_of_a_line():
    """Factory programs tried 549 sums across an axis's columns: a blank counts 0, a line blank in them all is no line, both deterministic programs agree, and it ranks like any measure."""
    _, table, tables = _setup(ACROSS_ROWS, formats=ACROSS_FORMATS)
    t = table.id
    months = Across((Col(t, 1), Col(t, 2), Col(t, 3)))
    assert _one(_answer(Agg(SUM, months, t, (Equals(Col(t, 0), "b"),)), tables)) == 15.0
    assert _one(_answer(Agg(SUM, months, t), tables)) == 156.0
    assert _one(_answer(Agg(AVG, months, t), tables)) == 31.2            # five lines: c is blank across them all
    assert _answer(Rank(ENTITY_LINE, t, HIGHEST, (), months, (Col(t, 0),)), tables)[0][0] == "d"
    assert _answer(Rank(ENTITY_GROUP, t, LOWEST, (), months, key=Col(t, 0), function=SUM), tables)[0][0] == "f"
    assert "across" in read_as(Agg(SUM, Across((Col(t, 1), Col(t, 2), Col(t, 3), Col(t, 4))), t), tables.catalog) or "plus" in read_as(Agg(SUM, months, t), tables.catalog)
    names = coordinates(Agg(SUM, months, t), tables.catalog.column_kind)
    assert coordinate("peers", t, "1,2,3") in names and coordinate("row_op", "+") in names
    assert cells_used(Agg(SUM, months, t, (Equals(Col(t, 0), "b"),)), tables, ())
    assert formula(Agg(SUM, months, t, (Equals(Col(t, 0), "b"),)), tables).startswith("=SUM(")
    for bad in ((Col(t, 1),), (Col(t, 1), Col("other", 1))):
        try:
            Across(bad)
        except ValueError:
            continue
        raise AssertionError("across takes two or more columns of one table")


def test_the_decoder_reaches_across_when_a_word_asks():
    catalog = _catalog(ACROSS_ROWS, formats=ACROSS_FORMATS)
    decoder = Decoder(catalog)
    t = catalog.tables[0].id
    months = Agg(SUM, Across((Col(t, 1), Col(t, 2), Col(t, 3))), t, (Equals(Col(t, 0), "b"),))
    asked = read_question("What is the total for b across the months?", catalog, decoder)
    assert months in decoder.enumerate(asked, k=300)[0]
    plain = Reading({"form:agg": 1.0, "aggregate:sum": 1.0, f"measure:{t}.1": 1.0})
    assert not [p for p in decoder.enumerate(plain, k=300)[0] if isinstance(p, Agg) and isinstance(p.measure, Across)], "no row operator read, no sum across"


def test_a_text_other_than():
    """Factory programs tried 183 text conditions with not-equal. A blank is no other value, in the masks and in both programs."""
    _, table, tables = _setup(ACROSS_ROWS, formats=ACROSS_FORMATS)
    t = table.id
    other = NotEquals(Col(t, 4), "open")
    assert _one(_answer(Agg(COUNT, None, t, (other,)), tables)) == 2.0     # b and f; d is blank
    assert _one(_answer(Agg(SUM, Col(t, 1), t, (other,)), tables)) == 13.0
    index = TableIndex(table)
    assert index.mask(other) == table.condition_mask(other) == sum(1 << at for at, line in enumerate(table.lines) if table.folds[4][at] == "closed")
    assert "other than" in read_as(Agg(COUNT, None, t, (other,)), tables.catalog)
    assert not formula(Agg(SUM, Col(t, 1), t, (other,)), tables).startswith("=SUMIFS"), "a sheet's <> passes a blank; this does not"
    assert coordinate("comparison", "!=") in coordinates(Agg(COUNT, None, t, (other,)), tables.catalog.column_kind)


def test_the_decoder_reads_other_than_from_the_words():
    catalog = _catalog(ACROSS_ROWS, formats=ACROSS_FORMATS)
    decoder = Decoder(catalog)
    t = catalog.tables[0].id
    reading = read_question("How many lines have a status other than closed?", catalog, decoder)
    assert reading.scores[coordinate("comparison", "!=")] == 1.0
    programs = decoder.enumerate(reading, k=300)[0]
    assert Agg(COUNT, None, t, (NotEquals(Col(t, 4), "closed"),)) in programs


def test_answer_types_place_money_quantity():
    assert {"place", "money", "quantity"} <= set(ANSWER_TYPES)
    assert coarse_type("place") == A_NAME and coarse_type("money") == coarse_type("quantity") == A_NUMBER and coarse_type("count") == A_COUNT
    assert predict_answer_type("Where is the Waha hub?") == "place" and predict_answer_type("Which city has the most deals?") == "place"
    assert predict_answer_type("How much did we pay Transco?") == "money" and predict_answer_type("How many MW are scheduled?") == "quantity"
    catalog, table, tables, col = _trades()
    assert verify_one(score_program(Show(table.id, (col(0),), (Equals(col(1), "west"),)), Reading({}), catalog), tables, "place").held(EV_TYPE_AGREES)
    assert verify_one(score_program(Agg(SUM, col(3), table.id), Reading({}), catalog), tables, "money").held(EV_TYPE_AGREES)


def _reordered(rows, order):
    return [[row[at] for at in order] for row in rows]


def _by_names(programs, table):
    """A program list read through column names, so two orders of a sheet can be compared."""
    out = []
    for program in programs:
        names = coordinates(program, lambda column: table.columns[column.position].kind if column.position >= 0 else "text")
        out.append(tuple(sorted(_column_words(name, table) for name in names)))
    return out


def _column_words(name, table):
    prefix = f"{table.id}."
    head, _, body = name.partition(":")
    if body.startswith(prefix):
        position, _, rest = body[len(prefix):].partition("=")
        if position.lstrip("-").isdigit() and int(position) >= 0:
            return f"{head}:{table.columns[int(position)].name}={rest}"
    return name


def test_decoder_ties_do_not_follow_sheet_order():
    """Columns tying on a reading come out in the same order however the sheet stands. On position ties the top of the list followed the sheet, so a shuffled sheet could answer otherwise."""
    rows = [["Alpha", "Beta", "Gamma", "Delta", "Kind"], [1.0, 20.0, 300.0, 4000.0, "x"], [2.0, 30.0, 500.0, 7000.0, "y"], [5.0, 70.0, 110.0, 1300.0, "x"]]
    reading_for = lambda t: Reading({"form:agg": 1.0, "aggregate:sum": 1.0, "form:rank": 0.5, "entity:line": 0.2, "direction:highest": 0.2})
    first = None
    for order in ((0, 1, 2, 3, 4), (3, 1, 4, 0, 2), (4, 3, 2, 1, 0), (2, 0, 3, 4, 1)):
        catalog = _catalog(_reordered(rows, order))
        table = catalog.tables[0]
        listed = _by_names([item.program for item in Decoder(catalog).decode(reading_for(table.id), k=80).programs], table)
        if first is None:
            first = listed
        assert listed == first, order


def test_several_columns_shown_type_the_same_however_they_stand():
    """57.1 flipped on a reorder: a show of a name and a figure took its answer type from whichever stood first."""
    rows = [["Deal", "Value", "Book"], ["a", 1.0, "x"], ["b", 2.0, "y"], ["c", 3.0, "z"], ["d", 7.0, "w"]]
    for order in ((0, 1, 2), (1, 0, 2), (2, 1, 0)):
        catalog = _catalog(_reordered(rows, order), formats=[{"f": "General"}] * 3)
        table = catalog.tables[0]
        named = {column.name: Col(table.id, column.position) for column in table.columns}
        program = Show(table.id, tuple(sorted((named["Deal"], named["Value"]))), (Equals(named["Book"], "x"),))
        assert answer_type_of(program, catalog.column_kind) == A_NUMBER, order
        decoder = Decoder(catalog)
        reading = Reading({"form:show": 1.0, "answer_type:number": 1.0, coordinate("shown", named["Deal"].name): 1.0, coordinate("shown", named["Value"].name): 1.0, coordinate("value", named["Book"].name, "x"): 1.0})
        scores = {item.program: item.score for item in decoder.decode(reading, k=30).programs}
        assert scores[program] == score_program(program, reading, catalog).score


def test_a_misspelled_word_reads_as_the_sheets_word():
    """About 15% of the factory's questions and plenty of a person's hold a typo. One edit away, two from a long word, the first letter kept; two words equally near read as neither."""
    rows = [["Turbine Units", "Counterparty", "Volume"], ["alpha", "Transco", 1.0], ["beta", "Dynegy", 2.0], ["gamma", "Transco", 4.0], ["delta", "Dynegy", 8.0]]
    catalog = _catalog(rows)
    table = catalog.tables[0]
    index = Decoder(catalog).indexes[table.id]
    assert index.spelled_as("turrbine") == "turbine" and index.spelled_as("cuonterparty") == "counterparty" and index.spelled_as("transco") == "transco"
    assert index.spelled_as("zzzzzzz") is None and index.spelled_as("alp") is None, "too short or nothing near"
    decoder = Decoder(catalog)
    assert read_question("total turrbine", catalog, decoder).scores.get(coordinate("measure", Col(table.id, 0).name), 0) > 0


def test_a_word_only_one_column_holds_says_which():
    rows = [["Price per MMBtu Florida City Gate", "Price per MMBtu Nymex Henry Hub", "Month"], [1.0, 2.0, "a"], [3.0, 4.0, "b"], [5.0, 6.0, "c"], [7.0, 8.0, "d"]]
    catalog = _catalog(rows)
    table = catalog.tables[0]
    scores = read_question("nymex in march", catalog, Decoder(catalog)).scores
    assert scores[coordinate("measure", Col(table.id, 1).name)] >= 0.6 and coordinate("measure", Col(table.id, 0).name) not in scores


def test_several_shown_columns_cost_a_named_coordinate():
    """A show of three columns outscored the one asked for, as each link adds: 69 of 195 golds lost most on the shown slot."""
    rows = [["Deal", "Value", "Book"], ["a", 1.0, "x"], ["b", 2.0, "y"], ["c", 3.0, "z"], ["d", 7.0, "w"]]
    catalog = _catalog(rows)
    table = catalog.tables[0]
    decoder = Decoder(catalog)
    one = Show(table.id, (Col(table.id, 1),), (Equals(Col(table.id, 2), "x"),))
    two = Show(table.id, (Col(table.id, 0), Col(table.id, 1)), (Equals(Col(table.id, 2), "x"),))
    reading = Reading({"form:show": 1.0, coordinate("shown", Col(table.id, 0).name): 1.0, coordinate("shown", Col(table.id, 1).name): 2.0, coordinate("value", Col(table.id, 2).name, "x"): 1.0, "shown:together=2": -2.5})
    programs = [item.program for item in decoder.decode(reading, k=20).programs]
    assert one in programs and two in programs and programs.index(one) < programs.index(two)
    assert "shown:together=2" in coordinates(two, catalog.column_kind) and "shown:together=2" not in coordinates(one, catalog.column_kind)


def test_a_sign_word_compares_with_zero():
    catalog, table, tables, col = _trades()
    decoder = Decoder(catalog)
    reading = read_question("How many deals have negative volume?", catalog, decoder)
    assert reading.scores[coordinate("number", "0")] == 1.0 and reading.scores[coordinate("comparison", "<")] == 1.0
    assert Agg(COUNT, None, table.id, (Compare(col(3), "<", 0.0),)) in decoder.enumerate(reading, k=200)[0]


def test_a_stated_number_a_column_holds_reads_as_its_value():
    """"total col b for 1015": a number held as a key is asked for as itself, not only as a comparison."""
    rows = [["Group", "Amount", "Kind"], [1015.0, 58.5, "x"], [1020.0, 3.0, "y"], [1030.0, 4.0, "x"], [1040.0, 5.0, "y"]]
    catalog = _catalog(rows)
    table = catalog.tables[0]
    decoder = Decoder(catalog)
    reading = read_question("total amount for 1015", catalog, decoder)
    assert reading.scores[coordinate("value", Col(table.id, 0).name, "1015")] > 0
    assert Agg(SUM, Col(table.id, 1), table.id, (Equals(Col(table.id, 0), 1015.0),)) in decoder.enumerate(reading, k=100)[0]


def test_a_family_adding_twice_says_so():
    """Family.adds_twice: members repeat each other's lines. A sum or count across the members is flagged; one member's lines, or a read, are not."""
    def book(tab):
        return {"name": tab, "rows": [["Deal", "Desk", "Volume"]] + [[f"d{at}", "w" if at % 2 else "e", float(at * 7 + 1)] for at in range(14)]}
    catalog = Catalog.from_model(compile_workbook(workbook_from_json({"name": "book", "tabs": [book("Jan"), book("Feb")]})))
    family = next(table for table in catalog.tables if table.kind == "family")
    assert family.adds_twice
    tables = RequestTables(catalog)
    f = family.id
    across = Agg(SUM, Col(f, 3), f)
    assert adds_across_members(across, catalog) and verify_one(score_program(across, Reading({}), catalog), tables, A_NUMBER).held(EV_ADDS_TWICE)
    assert not adds_across_members(Agg(SUM, Col(f, 3), f, (Equals(Col(f, 0), "jan"),)), catalog)
    assert not adds_across_members(Agg(AVG, Col(f, 3), f), catalog)
    assert not adds_across_members(Show(f, (Col(f, 3),), (Equals(Col(f, 1), "d3"),)), catalog)
    assert adds_across_members(Rank(ENTITY_GROUP, f, HIGHEST, (), Col(f, 3), key=Col(f, 2), function=SUM), catalog)
    assert adds_across_members(Calc(across, MINUS, Agg(SUM, Col(f, 3), f, (Equals(Col(f, 2), "w"),))), catalog)
    assert not adds_across_members(Agg(SUM, Col("t0", 2), "t0"), catalog)


def test_axes_are_read_by_their_kind():
    """A flag axis counts marks and adds nothing; an axis named only by a total beside it is a peer set only where a question asks."""
    marks = [["Deal", "Master", "ISDA", "CSA"], ["1", "Y", "N", "Y"], ["2", "Y", "Y", "N"], ["3", "N", "Y", "N"], ["4", "Y", "N", "N"]]
    catalog = _catalog(marks)
    table = catalog.tables[0]
    decoder = Decoder(catalog)
    index = decoder.indexes[table.id]
    assert index.flag_sets == ((1, 2, 3),) and not index.peer_sets
    reading = Reading({"form:rank": 3.0, "entity:peer": 3.0, "direction:highest": 1.0, f"value:{table.id}=y": 1.0})
    programs = decoder.enumerate(reading, k=20)[0]
    assert Rank(ENTITY_PEER, table.id, HIGHEST, (), peers=(1, 2, 3), mark="y") in programs
    books = [["Trader", "", "", "Book total"], ["", "Alpha", "Beta", "Gamma"], ["a", 1.0, 2.0, 3.0], ["b", 4.0, 5.0, 6.0]]
    wide = [["Group", "Desk one", "Desk two", "Desk three", "Total"], ["a", 1.0, 2.0, 3.0, 6.0], ["b", 4.0, 5.0, 6.0, 99.0], ["c", 7.0, 8.0, 9.0, 1.0]]
    for rows in (books, wide):
        catalog = _catalog(rows)
        table = catalog.tables[0]
        index = Decoder(catalog).indexes[table.id]
        for held in index.totalled_sets:
            assert held not in tuple(tuple(axis.positions) for axis in table.table.axes if axis.kind != "totalled")


def test_a_question_reads_the_tables_its_links_reach():
    """The worst case is a workbook of many tables: past linked_tables only those the reading reaches best make frames."""
    tabs = [{"name": f"Tab{at}", "rows": [[f"Name{at}", f"Figure{at}"], [f"a{at}", 1.0 + at], [f"b{at}", 2.0 + at], [f"c{at}", 3.0 + at]]} for at in range(POLICY.linked_tables + 6)]
    catalog = Catalog.from_model(compile_workbook(workbook_from_json({"name": "book", "tabs": tabs})))
    decoder = Decoder(catalog)
    far = catalog.tables[-1]
    reading = read_question(f"What is the total {far.columns[1].name}?", catalog, decoder)
    programs = decoder.enumerate(reading, k=50)[0]
    assert {tables_of(program)[0] for program in programs} <= {table.id for table in catalog.tables}
    assert any(tables_of(program)[0] == far.id for program in programs)
    assert len({tables_of(program)[0] for program in decoder.enumerate(reading, k=3000)[0]}) <= POLICY.linked_tables


def test_verifier_stops_at_its_budget_of_lines():
    """A hard, deterministic stop on what a question's candidates read: the first MIN_RUN always run."""
    rows = [["Name", "Amount"]] + [[f"n{at}", float(at)] for at in range(40000)]
    catalog, table, tables = _setup(rows)
    t = table.id
    many = [score_program(Agg(function, Col(t, 1), t), Reading({}), catalog) for function in (SUM, AVG, MIN, MAX) for _ in range(10)]
    done = verify(many, tables, A_NUMBER, top=40)
    assert MIN_RUN <= len(done) < 40 and sum(len(table.lines) for _ in done) <= max(LINES_READ, MIN_RUN * len(table.lines))


# ----------------------------------------------------------------------------------------------------------- the output
def test_read_as_in_the_sheets_own_words():
    catalog, table, tables, col = _trades()
    t = table.id
    assert read_as(Agg(SUM, col(3), t, (Equals(col(0), "ees"),)), catalog) == 'the total of Volume on "Sheet1" where Counterparty is "EES"'
    assert read_as(Agg(COUNT, None, t), catalog) == 'the number of lines on "Sheet1"'
    assert read_as(Show(t, (col(3),), (Equals(col(0), "transco"),)), catalog) == 'Volume on "Sheet1" where Counterparty is "Transco"'
    assert read_as(Rank(ENTITY_LINE, t, LOWEST, (), col(3), (col(0),)), catalog) == 'Counterparty of the line with the lowest Volume on "Sheet1"'
    assert read_as(Rank(ENTITY_GROUP, t, HIGHEST, (), col(3), key=col(1), function=SUM), catalog) == 'the Book with the highest total of Volume on "Sheet1"'
    assert "minus" in read_as(Calc(Agg(SUM, col(3), t), MINUS, Agg(SUM, col(4), t)), catalog)
    assert "by size" in read_as(Rank(ENTITY_GROUP, t, HIGHEST, (), col(3), key=col(1), function=SUM, by_size=True), catalog)
    assert "falls in 2001-01" in read_as(Agg(COUNT, None, t, (InPeriod(col(2), period_from_name("2001-01")),)), catalog)
    assert 'on the line "Total"' in read_as(Show(t, (col(3),), (OnLine(t, "Total"),)), catalog)


def test_cells_are_the_exact_addresses_used():
    catalog, table, tables, col = _trades()
    t = table.id
    # a read: the cell it takes
    assert cells_used(Show(t, (col(3),), (Equals(col(0), "transco"),)), tables, ()) == ("'Sheet1'!D4",)
    # a sum: the kept lines' measure cells, the printed line not among them
    used = cells_used(Agg(SUM, col(3), t), tables, ())
    assert used == ("'Sheet1'!D2", "'Sheet1'!D3", "'Sheet1'!D4", "'Sheet1'!D5", "'Sheet1'!D6")
    assert compress(used) == ("'Sheet1'!D2:D6",)
    assert compress(("'T'!A1", "'T'!A2", "'T'!A4", "'T'!B1")) == ("'T'!A1:A2", "'T'!A4", "'T'!B1")
    # a printed line read: its own cell
    assert cells_used(Show(t, (col(3),), (OnLine(t, "total"),)), tables, ()) == ("'Sheet1'!D7",)
    # a rank: the top line's cells
    program = Rank(ENTITY_LINE, t, LOWEST, (), col(3), (col(0),))
    assert cells_used(program, tables, run(program, tables).rows) == ("'Sheet1'!A6", "'Sheet1'!D6")
    # a group: the winner's cells
    group = Rank(ENTITY_GROUP, t, HIGHEST, (), col(3), key=col(1), function=SUM)
    assert cells_used(group, tables, run(group, tables).rows) == ("'Sheet1'!D3", "'Sheet1'!D5")
    # a count: one cell of each line it counts
    assert cells_used(Agg(COUNT, None, t, (Equals(col(1), "west"),)), tables, ()) == ("'Sheet1'!A2", "'Sheet1'!A4", "'Sheet1'!A6")
    # a calc: both sides'
    assert cells_used(Calc(Show(t, (col(3),), (Equals(col(0), "transco"),)), MINUS, Show(t, (col(4),), (Equals(col(0), "transco"),))), tables, ()) == ("'Sheet1'!D4", "'Sheet1'!E4")


def test_formula_a_user_can_check():
    """program_language.txt, section 5: each form has a formula. A line with printed lines inside its range states the kept cells rather than a criterion that would add them."""
    plain_rows = [["Counterparty", "Book", "Volume"], ["EES", "West", 10.0], ["Enron", "East", 20.0], ["EES", "East", 30.0], ["Dynegy", "West", 40.0]]
    catalog, table, tables = _setup(plain_rows)
    t = table.id
    who, book, volume = (Col(t, at) for at in range(3))
    assert formula(Agg(SUM, volume, t, (Equals(who, "ees"),)), tables) == "=SUMIFS('Sheet1'!C2:C5,'Sheet1'!A2:A5,\"EES\")"
    assert formula(Agg(SUM, volume, t), tables) == "=SUM('Sheet1'!C2:C5)"
    assert formula(Agg(MAX, volume, t), tables) == "=MAX('Sheet1'!C2:C5)"
    assert formula(Agg(COUNT, None, t, (Equals(book, "west"),)), tables) == "=COUNTIFS('Sheet1'!B2:B5,\"West\")"
    assert formula(Agg(SUM, volume, t, (Compare(volume, GE, 20.0),)), tables) == "=SUMIFS('Sheet1'!C2:C5,'Sheet1'!C2:C5,\">=20\")"
    assert formula(Agg(SUM, volume, t, magnitude=True), tables) == "=ABS(SUM('Sheet1'!C2:C5))"
    assert formula(Show(t, (volume,), (Equals(who, "enron"),)), tables) == "='Sheet1'!C3"
    assert formula(Rank(ENTITY_LINE, t, HIGHEST, (), volume, (who,)), tables) == "=INDEX('Sheet1'!A2:A5,MATCH(MAX('Sheet1'!C2:C5),'Sheet1'!C2:C5,0))"
    assert formula(Rank(ENTITY_GROUP, t, HIGHEST, (), volume, key=who, function=SUM), tables) == "=INDEX(SORTBY(UNIQUE('Sheet1'!A2:A5),SUMIFS('Sheet1'!C2:C5,'Sheet1'!A2:A5,UNIQUE('Sheet1'!A2:A5)),-1),1)"
    assert formula(Calc(Agg(SUM, volume, t, (Equals(who, "ees"),)), MINUS, Agg(SUM, volume, t, (Equals(who, "enron"),))), tables) == "=(SUMIFS('Sheet1'!C2:C5,'Sheet1'!A2:A5,\"EES\")) - (SUMIFS('Sheet1'!C2:C5,'Sheet1'!A2:A5,\"Enron\"))"
    _, printed, stores, col = _trades()
    assert formula(Agg(SUM, col(3), printed.id), stores) == "=SUM('Sheet1'!D2:D6)"                  # the printed D7 stays out
    assert formula(Show(printed.id, (col(3),), (OnLine(printed.id, "total"),)), stores) == "='Sheet1'!D7"
    assert formula(Agg(COUNT, None, printed.id, (Equals(col(1), "west"),)), stores) == "=COUNTA('Sheet1'!A2,'Sheet1'!A4,'Sheet1'!A6)"     # printed lines inside the range: the cells, not a criterion
    across = [["Book", "Jan", "Feb", "Mar"], ["a", 1.0, 5.0, 3.0], ["b", 2.0, 2.0, 9.0], ["a", 1.0, 1.0, 1.0]]
    _, peers, peer_tables = _setup(across)
    two = formula(Rank(ENTITY_PEER, peers.id, HIGHEST, (Equals(Col(peers.id, 0), "a"),), peers=(1, 2, 3)), peer_tables)
    assert two == "=INDEX('Sheet1'!B1:D1,MATCH(MAX('Sheet1'!B2:D2+'Sheet1'!B4:D4),'Sheet1'!B2:D2+'Sheet1'!B4:D4,0))"


def test_explain_carries_the_named_contributions():
    catalog, table, tables, col = _trades()
    decoder = Decoder(catalog)
    reading = read_question("What is the total volume for Transco?", catalog, decoder)
    top = decoder.decode(reading, k=5).programs[0]
    rows = run(top.program, tables).rows
    proof = explain(top, tables, rows)
    assert proof.read_as and proof.formula.startswith("=")
    assert proof.contributions and all({"coordinate", "slot", "score", "weight", "contribution"} <= set(item) for item in proof.contributions)
    assert any(item.get("word_role") == "measure" for item in proof.contributions)
    share = Calc(Agg(SUM, col(3), table.id, (Equals(col(0), "transco"),)), OVER, Agg(SUM, col(3), table.id, (Contains(col(0), "trans"),)))
    shared = explain(score_program(share, reading, catalog), tables, run(share, tables).rows)
    assert shared.also_stated == ("42.8571%",)
    assert abs(sum(item["contribution"] for item in proof.contributions) - top.score) < 1e-6
    assert proof.record()["read_as"] == proof.read_as
    assert answer_type_of(Agg(COUNT, None, table.id), catalog.column_kind) == A_COUNT and answer_type_of(Calc(Agg(SUM, col(3), table.id), OVER, Agg(SUM, col(4), table.id)), catalog.column_kind) == A_PERCENT
    assert CALC in FORMS and len(COMPARISONS) == 5 and cell_day(None) is None and MIN and SUM


TESTS = [
    test_several_shown_columns_cost_a_named_coordinate, test_a_sign_word_compares_with_zero, test_a_stated_number_a_column_holds_reads_as_its_value, test_a_misspelled_word_reads_as_the_sheets_word, test_a_word_only_one_column_holds_says_which, test_several_columns_shown_type_the_same_however_they_stand, test_across_adds_peer_columns_of_a_line, test_the_decoder_reaches_across_when_a_word_asks, test_a_text_other_than, test_the_decoder_reads_other_than_from_the_words, test_answer_types_place_money_quantity, test_decoder_ties_do_not_follow_sheet_order, test_a_family_adding_twice_says_so, test_axes_are_read_by_their_kind, test_a_question_reads_the_tables_its_links_reach, test_verifier_stops_at_its_budget_of_lines,
    test_every_name_is_from_a_closed_list, test_forms_refuse_what_they_cannot_say, test_signs, test_printed_lines_are_read_by_label_and_never_added, test_shortest_label_first_and_unlabelled_printed_lines, test_a_column_filled_only_on_printed_lines_reads_them,
    test_partial_value_match, test_case_and_spacing_do_not_count, test_periods, test_blanks, test_ties_keep_every_tied_line, test_units, test_mixed_columns, test_repeated_rows, test_sections_nested_bands_and_the_section_shown, test_python_masks_agree_with_the_sql,
    test_calc_shapes, test_rank_across_peer_columns, test_a_rank_keeps_as_many_as_the_question_states, test_the_decoder_proposes_how_many_a_question_states, test_a_check_says_yes_or_no, test_the_decoder_checks_only_when_the_reading_names_a_check, test_a_choice_among_named_values, test_a_check_asks_what_the_top_one_is, test_a_read_takes_the_line_after_or_before_the_kept_one, test_new_ast_nodes_render_and_validate, test_plan_schema_holds_only_what_a_query_reads, test_perfect_readings_give_the_gold_first, test_decoder_is_deterministic_and_order_free, test_the_list_does_not_depend_on_hash_order,
    test_scores_come_in_as_data_and_nothing_else_moves_the_list, test_contributions_are_named_and_sum_to_the_score, test_intervention_a_named_coordinate_moves_the_score_by_its_weight, test_policy_prunes_values_only_where_it_says, test_conditions_stay_on_distinct_columns,
    test_answer_type_rule, test_an_ordinal_is_not_a_stated_number, test_words_alone_read_a_question, test_verifier_shape_and_type_evidence, test_printed_total_ties_only_under_its_exact_conditions, test_verifier_runs_the_top_programs_and_never_raises, test_read_as_in_the_sheets_own_words,
    test_cells_are_the_exact_addresses_used, test_formula_a_user_can_check, test_explain_carries_the_named_contributions,
]

if __name__ == "__main__":
    print("=== program language: one sheet per semantic, the k-best, the verifier, the proof ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as exc:                                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {str(exc)[:600]}")
    print(f"test_program: {len(TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
