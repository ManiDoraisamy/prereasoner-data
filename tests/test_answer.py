"""The program path with the Decisions API in its two places (engine/program/answer.py, closed.py, stock.py, dates.py), on
small synthetic sheets. Hermetic: engine.llm.DECISIONS_TRANSPORT is a fake that answers each closed question by a rule
of the test's own, the stock reader's backbone is a fake whose vectors come from a text's hash (a phrase and a target of
the same text read alike), and nothing reaches a network, a model file or Postgres.

usage: python -X utf8 -m tests.test_answer"""
from __future__ import annotations

import hashlib
import json
import math
import sys

import torch

import engine.llm as llm
import engine.program.stock as stock
from engine.program.answer import ANSWERED, ASKED, WARNED, answer, prepare
from engine.program.catalog import Catalog
from engine.program.closed import (CHOICE_NONE, CHOOSE_K, NONE, SAID_OPTIONS_MOST, T_ASK, T_FLAG, TAB_IN_VIEW_CONVENTION, W, answer_key, choice_record, choose_answer, fused_reading, preview,
                                   reading_record, said_record, said_scores, said_spans, sheet_state, value_set_question)
from engine.program.dates import typed_dates
from engine.program.decoder import Decoder
from engine.program.names import PERCENT_OP, SAID_ABOVE, SLOT_NUMBER, SLOT_PERIOD, SLOT_ROW_OP, SLOT_SAID, coordinate
from engine.program.store import RequestTables
from engine.sheet import compile_workbook, workbook_from_json

ROWS = [["Region", "Product", "Amount", "Date", "Year built"],
        ["East", "Pens", 120.0, "2024-01-05", 1990.0],
        ["West", "Pens", 80.0, "2024-02-11", 1985.0],
        ["East", "Ink", 45.5, "2023-03-02", 2001.0],
        ["North", "Paper", 60.0, "2024-03-19", 1979.0],
        ["West", "Ink", 30.0, "2023-07-23", 2010.0],
        ["East", "Paper", 75.0, "2024-08-30", 1995.0],
        ["North", "Pens", 20.0, "2023-11-14", 2005.0]]


def _workbook(rows=ROWS, name="Orders", more=()):
    tabs = [{"name": name, "rows": rows}, *more]
    return workbook_from_json({"name": "book", "tabs": tabs})


def _catalog(rows=ROWS, **more):
    return Catalog.from_model(compile_workbook(_workbook(rows, **more)))


class FakeBackbone:
    """Vectors from a text's hash: the same text reads the same, different texts nearly orthogonal."""
    dim = 32

    def encode_texts(self, texts, *, max_tokens=32, batch=32):
        distinct = sorted(set(texts), key=lambda text: (len(text), text))
        vectors = [[(hashlib.sha256(f"{text}|{i}".encode()).digest()[0] - 127.5) / 127.5 for i in range(self.dim)] for text in distinct]
        return distinct, torch.tensor(vectors, dtype=torch.float32) if vectors else torch.zeros((0, self.dim))

    def text_tokens(self, texts, *, max_tokens=32):
        return [min(len(text) + 2, max_tokens) for text in texts]


def _use_fake_reader():
    stock._READER = stock.StockReader(FakeBackbone())


class FakeTransport:
    """Answers a body as the endpoint does: each question's probabilities over exactly its options. rule(name, question,
    state) -> {option: weight} for the options it favours; the rest share what is left."""

    def __init__(self, rule):
        self.rule, self.bodies = rule, []

    def __call__(self, payload: bytes) -> dict:
        body = json.loads(payload.decode("utf-8"))
        self.bodies.append(body)
        state = json.loads(body["input"][len("STATE:\n"):])
        answers = {}
        for name, question in body["questions"].items():
            favoured = self.rule(name, question, state) or {}
            rest = [option for option in question["criteria"] if option not in favoured]
            left = max(0.0, 1.0 - sum(favoured.values()))
            answers[name] = {"probabilities": {**favoured, **{option: left / len(rest) for option in rest}} if rest else dict(favoured)}
        return {"answers": answers, "tokens": 7}


def _with_transport(transport, call):
    held = llm.DECISIONS_TRANSPORT
    llm.DECISIONS_TRANSPORT = transport
    try:
        return call()
    finally:
        llm.DECISIONS_TRANSPORT = held


def _option(question, needle):
    """The option whose description holds needle."""
    return next((option for option, text in question["criteria"].items() if needle in text), None)


def _reading_rule(name, question, state):
    """The reading a person would give "total amount for East in 2024": a sum of Amount, filtered by Region = East."""
    wanted = {"form": "agg", "aggregate": "sum", "measure": "Amount", "value": "'East'", "filter": "Region", "answer_type": "number"}
    if name in wanted and _option(question, wanted[name]) is not None:
        return {_option(question, wanted[name]): 0.8, NONE: 0.15} if NONE in question["criteria"] else {_option(question, wanted[name]): 0.9}
    if NONE in question["criteria"]:
        return {NONE: 0.9}
    return {}


def _stock_reading(question, catalog):
    _use_fake_reader()
    decoder = Decoder(catalog)
    decoder.warm()
    return stock.stock_reader().read(question, catalog, decoder, stock.workbook_targets(catalog))


# ------------------------------------------------------------------------------------------- the per-answer decision
def test_a_candidates_probability_is_summed_over_the_same_answer() -> None:
    """Two readings returning one answer (0.3 + 0.3) beat a single likelier reading (0.35) of another."""
    choice = choose_answer({"r00": 0.35, "r01": 0.3, "r02": 0.3, NONE: 0.05}, ["a", "b", "b"])
    assert choice.pick == 1 and choice.runner_up == 0 and abs(choice.mass - 0.6) < 1e-12 and choice.kind == ANSWERED


def test_ties_go_to_the_earlier_listing() -> None:
    choice = choose_answer({"r00": 0.25, "r01": 0.25, "r02": 0.25, "r03": 0.25}, ["a", "b", "a", "b"])
    assert choice.pick == 0 and choice.runner_up == 1


def test_thresholds_ask_warn_and_answer() -> None:
    assert choose_answer({"r00": T_ASK - 0.01, "r01": 0.2, NONE: 0.1}, ["a", "b"]).kind == ASKED
    assert choose_answer({"r00": T_ASK, "r01": 0.2, NONE: 0.1}, ["a", "b"]).kind == WARNED
    assert choose_answer({"r00": T_FLAG - 1e-9, NONE: 0.1}, ["a"]).kind == WARNED
    assert choose_answer({"r00": T_FLAG, NONE: 0.1}, ["a"]).kind == ANSWERED


def test_none_likelier_than_the_best_answer_asks() -> None:
    choice = choose_answer({"r00": 0.45, NONE: 0.55}, ["a"])
    assert choice.kind == ASKED and choice.pick == 0 and choice.none == 0.55


def test_nothing_listed_asks() -> None:
    assert choose_answer(None, []).kind == ASKED and choose_answer(None, []).pick is None
    assert choose_answer({NONE: 1.0}, []).kind == ASKED


def test_answer_key_tells_rows_apart() -> None:
    assert answer_key([(1.0,)]) == answer_key(((1.0,),)) and answer_key([(1.0,)]) != answer_key([(1,)]) and answer_key([("a", 2)]) != answer_key([("a", 3)])


# ------------------------------------------------------------------------------------------------------ the records
def test_sheet_state_describes_every_column_with_three_examples() -> None:
    catalog = _catalog()
    state = sheet_state(catalog, "total amount for East")
    assert state["question"] == "total amount for East" and list(state) == ["question", "workbook"]
    table = state["workbook"][0]
    assert list(table) == ["table", "tab", "lines", "columns"] and table["tab"] == "Orders" and table["lines"] == 7
    region = table["columns"][0]
    assert region["id"] == f"{table['table']}.0" and region["header"] == "Region" and region["examples"] == ["East", "West", "North"]


def test_sheet_state_names_a_family_table_by_all_its_tabs() -> None:
    other = {"name": "Second", "rows": [ROWS[0], *[[row[0], row[1], row[2] + 1, row[3], row[4]] for row in ROWS[1:]]]}
    catalog = _catalog(name="First", more=(other,))
    families = [entry for entry in sheet_state(catalog, "q")["workbook"] if "merges tabs" in entry]
    assert families and families[0]["tab"] == "First + Second" and families[0]["merges tabs"] == ["First", "Second"]


def test_a_workbook_of_several_tabs_names_the_tab_in_view() -> None:
    """Data tables on two tabs: the description names the tab in view, the client's active sheet when it is one of them,
    else the first data table's tab, and the convention; both records carry it. One tab: the description is unchanged."""
    staff = {"name": "Staff", "rows": [["Name", "Team", "Hours"], ["Ana", "Blue", 12.0], ["Bo", "Red", 7.0], ["Cy", "Blue", 9.0]]}
    catalog = _catalog(more=(staff,))
    state = sheet_state(catalog, "q")
    assert list(state) == ["question", "workbook", "tab in view", "convention"] and state["tab in view"] == "Orders"
    assert state["convention"] == TAB_IN_VIEW_CONVENTION and state["workbook"] == sheet_state(_catalog(), "q")["workbook"] + state["workbook"][1:]
    assert sheet_state(catalog, "q", "Staff")["tab in view"] == "Staff" and sheet_state(catalog, "q", "Chart 1")["tab in view"] == "Orders"
    assert "tab in view" not in sheet_state(_catalog(), "q", "Orders")
    named = _stock_reading("hours of the blue team", catalog)
    assert reading_record("hours of the blue team", named, catalog, "Staff").state["tab in view"] == "Staff"
    assert choice_record(catalog, "q", [("r", [(1,)])], "Staff")[0]["tab in view"] == "Staff"

def test_reading_record_offers_each_slot_its_coordinates() -> None:
    catalog = _catalog()
    named = _stock_reading("total amount for East in 2024", catalog)
    record = reading_record("total amount for East in 2024", named, catalog)
    for slot, mapping in record.options.items():
        criteria = record.questions[slot]["criteria"]
        assert set(mapping) <= set(criteria) and all(name.startswith(f"{slot}:") for name in mapping.values()), slot
        assert list(mapping) == [f"o{at:02d}" for at in range(len(mapping))], slot
    assert NONE in record.questions["value"]["criteria"] and NONE in record.questions["measure"]["criteria"] and NONE not in record.questions["form"]["criteria"]
    values = list(record.options["value"].values())
    assert len(values) == len(set(values)) and all(name == name.casefold() for name in values), "each value once, on the folded coordinate"
    assert any(text.endswith("= 'East'") for text in record.questions["value"]["criteria"].values()), "a value reads with the sheet's spelling"
    assert {"stated_number", "stated_period", "stated_year", "value_count", "row_formula"} <= set(record.questions)
    assert "written_day" not in record.questions, "the question writes no day number"
    assert record.numbers["o03"] == coordinate(SLOT_NUMBER, "3") and record.counted and len(record.percent) == 3
    assert any(name == coordinate(SLOT_PERIOD, "2024-q1") for name in record.periods.values())


def test_written_day_questions_need_a_year_and_a_day_number() -> None:
    catalog = _catalog()
    named = _stock_reading("orders before March 10, 2024", catalog)
    record = reading_record("orders before March 10, 2024", named, catalog)
    assert "written_day" in record.questions and "date_relation" in record.questions
    assert record.days["d000"] == "2024-01-10" and len(record.days) == 12


def test_a_sheet_without_days_or_years_asks_no_period() -> None:
    rows = [[row[0], row[1], row[2]] for row in ROWS]
    catalog = _catalog(rows)
    named = _stock_reading("total amount for East in 2024", catalog)
    record = reading_record("total amount for East in 2024", named, catalog)
    assert "stated_period" not in record.questions and "stated_year" not in record.questions and not record.periods and not record.years


def test_fused_reading_reads_the_api_against_the_uniform_share_and_values_against_none() -> None:
    catalog = _catalog()
    question = "total amount for East in 2024"
    named = _stock_reading(question, catalog)
    record = reading_record(question, named, catalog)
    transport = FakeTransport(_reading_rule)
    fields = _with_transport(transport, lambda: llm.decide(record.state, record.questions)).fields
    fused = fused_reading(named, record, fields)
    form = fields["form"]
    agg = record.options["form"][_option(record.questions["form"], "agg")]
    assert abs(fused.reading.scores[agg] - W * (math.log(form[_option(record.questions["form"], "agg")]) - math.log(1 / len(form)))) < 1e-9
    east = record.options["value"][_option(record.questions["value"], "'East'")]
    assert fused.reading.scores[east] > 0, "the named value is likelier than none"
    others = [name for option, name in record.options["value"].items() if name != east]
    assert all(fused.reading.scores[name] < 0 for name in others), "every other value is below none"
    assert fused.reading.scores[coordinate(SLOT_ROW_OP, PERCENT_OP)] < 0, "no percent: the API read none"
    assert not any(name in fused.reading.scores and name not in named.reading.scores for name in record.numbers.values()), "no number joins below none"
    assert fused.read.closed["aggregate"]["sum"] > 0.5 and fused.expected == "number"


def test_value_sets_ask_about_the_likeliest_values() -> None:
    catalog = _catalog()
    question = "total amount for East or West"
    named = _stock_reading(question, catalog)
    record = reading_record(question, named, catalog)
    east, west = _option(record.questions["value"], "'East'"), _option(record.questions["value"], "'West'")
    fields = {"value": {east: 0.45, west: 0.4, NONE: 0.15}}
    set_question, names = value_set_question(record, fields)
    assert names == [record.options["value"][east], record.options["value"][west]]
    assert list(set_question["criteria"]) == ["saa", "sab", "sac", NONE] and set_question["criteria"]["sac"] == "Region = 'East' and Region = 'West'"
    fused = fused_reading(named, record, fields, names, {"saa": 0.05, "sab": 0.05, "sac": 0.85, NONE: 0.05})
    assert fused.reading.scores[names[0]] > 0 and fused.reading.scores[names[1]] > 0


def test_choice_record_lists_each_candidate_with_its_answer_and_none() -> None:
    catalog = _catalog()
    listed = [(f"reading {at}", [(at, "x")] * (at + 1)) for at in range(CHOOSE_K + 5)]
    state, questions = choice_record(catalog, "q", listed)
    criteria = questions["reading"]["criteria"]
    assert state == sheet_state(catalog, "q") and len(criteria) == CHOOSE_K + 1 and criteria[NONE] == CHOICE_NONE
    assert criteria["r00"] == "reading 0 -> answer: 0, x" and criteria["r04"] == f"reading 4 -> answer: {preview([(4, 'x')] * 5)}"
    assert preview([(4, "x")] * 5) == "4, x | 4, x | 4, x (and 2 more rows)"


def test_the_request_body_is_sorted_and_hashable() -> None:
    """The body is one byte string per record: sorted keys, STATE as its JSON text; a replay finds it by its hash."""
    payload = llm.decisions_payload({"question": "q", "workbook": []}, {"b": {"instructions": "i", "criteria": {"y": "1", "x": "2"}}, "a": {"instructions": "j", "criteria": {"x": "1", "none": "n"}}})
    body = json.loads(payload)
    assert list(body) == ["input", "model", "questions"] and list(body["questions"]) == ["a", "b"] and list(body["questions"]["b"]["criteria"]) == ["x", "y"]
    assert body["input"] == 'STATE:\n{"question": "q", "workbook": []}' and llm.decision_hash(payload) == hashlib.sha256(payload).hexdigest()



def test_said_spans_are_the_questions_own_words() -> None:
    """Spans of one to three words as written, each distinct (folded) span once, in the order it first stands; the
    said call's STATE is the question alone and both questions offer the same spans."""
    assert said_spans("More or less? more") == ["More", "or", "less", "More or", "or less", "less more", "More or less", "or less more"]
    state, questions, words = said_record("is it more or less")
    assert state == {"question": "is it more or less"} and list(questions) == ["said_above", "said_below"]
    assert questions["said_above"]["criteria"] == {**words, NONE: "none"} == questions["said_below"]["criteria"] and words["w000"] == "is"
    assert said_record("?!") == ({}, {}, {}) and len(said_spans(" ".join(f"w{i}" for i in range(200)))) == SAID_OPTIONS_MOST


def test_said_words_join_the_reading_only_above_none() -> None:
    _, _, words = said_record("is it more or less")
    more, less = (key for key, span in words.items() if span in ("more", "less"))
    fields = {"said_above": {more: 0.7, NONE: 0.2, less: 0.1}, "said_below": {less: 0.05, NONE: 0.9, more: 0.05}}
    scores = said_scores({}, words, fields)
    assert scores == {coordinate(SLOT_SAID, SAID_ABOVE, "more"): round(W * (math.log(0.7) - math.log(0.2)), 4)}
    held = {coordinate(SLOT_SAID, SAID_ABOVE, "more"): 9.0}
    assert said_scores(held, words, fields) == held, "a coordinate already held keeps its score"


def test_an_either_or_is_answered_in_the_questions_words() -> None:
    """"more or less than 200": the said call reads "more" for above and "less" for below, and the check answers in them."""
    _use_fake_reader()
    prepared = _with_transport(FakeTransport(lambda *a: {}), lambda: prepare(_workbook()))

    def rule(name, question, state):
        if name == "reading":
            return {next(option for option in question["criteria"] if option != NONE): 0.8}
        if name in ("said_above", "said_below"):
            word = "more" if name == "said_above" else "less"
            return {next(key for key, text in question["criteria"].items() if text == word): 0.8, NONE: 0.1}
        wanted = {"form": "check", "quantifier": "versus", "aggregate": "sum", "measure": "Amount", "value": "'East'", "filter": "Region"}
        if name in wanted and _option(question, wanted[name]) is not None:
            return {_option(question, wanted[name]): 0.8, NONE: 0.15} if NONE in question["criteria"] else {_option(question, wanted[name]): 0.9}
        return {NONE: 0.9} if NONE in question["criteria"] else {}
    said = _with_transport(FakeTransport(rule), lambda: answer("is the total amount for East more or less than 200", prepared, RequestTables(prepared.catalog)))
    assert said.kind == ANSWERED and said.rows == (("more",),) and said.read_as.startswith("'more' if ")

# --------------------------------------------------------------------------------------------------------- the path
def test_answer_reads_decodes_and_chooses_through_its_calls() -> None:
    """The reading call (and the value-set call), the said call beside them, then the choice call."""
    _use_fake_reader()
    prepared = _with_transport(FakeTransport(lambda *a: {}), lambda: prepare(_workbook()))
    question = "total amount for East"

    def rule(name, criteria_question, state):
        if name == "reading":
            first = next(option for option in criteria_question["criteria"] if option != NONE)
            return {first: 0.8}
        return _reading_rule(name, criteria_question, state)
    transport = FakeTransport(rule)
    said = _with_transport(transport, lambda: answer(question, prepared, RequestTables(prepared.catalog)))
    names = [list(body["questions"]) for body in transport.bodies]
    assert names[-1] == ["reading"] and ["said_above", "said_below"] in names and any("value" in called for called in names), names
    assert len(names) in (3, 4), names
    assert said.listed_rows and said.probabilities is not None and len(said.probabilities) == min(len(said.listed_rows), CHOOSE_K) + 1
    assert said.listed_answers == tuple(answer_key(rows) for rows in said.listed_rows)
    expected = choose_answer(said.probabilities, said.listed_answers)
    assert said.kind == expected.kind and said.rows == said.listed_rows[expected.pick] and said.read_as == said.listed_read_as[expected.pick]
    assert said.tokens == 7 * len(transport.bodies)
    assert ((240.5,),) in said.listed_rows, "the sum of Amount for East is listed"
    assert len(said.listed_programs) == len(said.listed_rows) and said.pick == expected.pick and said.runner_up == expected.runner_up
    assert said.program is said.listed_programs[said.pick]


def test_a_failed_call_reaches_the_caller() -> None:
    _use_fake_reader()
    prepared = _with_transport(FakeTransport(lambda *a: {}), lambda: prepare(_workbook()))

    def down(payload):
        raise llm.LLMUnavailable("down")
    try:
        _with_transport(down, lambda: answer("total amount for East", prepared, RequestTables(prepared.catalog)))
    except llm.LLMUnavailable:
        return
    raise AssertionError("an unavailable API was not reported")


def test_text_dates_are_typed_when_the_workbook_loads() -> None:
    """A column of dates written with a month word is typed by one column question and one month question per word, and
    the workbook compiled again reads those cells as days."""
    months = {"Mar": "o03", "Jun": "o06"}
    rows = [["Shipped", "Amount"], ["Mar 5 2024", 10.0], ["Jun 17 2024", 20.0], ["Mar 30 2023", 5.0]]

    def rule(name, question, state):
        if name.startswith("c"):
            return {"date": 0.95}
        word = question["instructions"].split('"')[1]
        return {months[word]: 0.9} if word in months else {NONE: 0.9}
    transport = FakeTransport(rule)
    workbook = _workbook(rows)
    catalog = Catalog.from_model(compile_workbook(workbook))
    assert not any(catalog.tables[0].days[0]), "the compiler alone leaves them as text"
    typed = _with_transport(transport, lambda: typed_dates(catalog))
    assert typed == {"Mar 5 2024": "2024-03-05", "Jun 17 2024": "2024-06-17", "Mar 30 2023": "2023-03-30"}
    assert [list(body["questions"]) for body in transport.bodies] == [["c000"], ["waa", "wab"]]
    retyped = Catalog.from_model(compile_workbook(workbook, typed))
    assert retyped.tables[0].days[0] == ["2024-03-05", "2024-06-17", "2023-03-30"]
    _use_fake_reader()
    prepared = _with_transport(FakeTransport(rule), lambda: prepare(workbook))
    assert prepared.typed == typed and all(prepared.catalog.tables[0].days[0])


def test_digit_dates_ask_the_order_only_when_the_column_never_shows_it() -> None:
    rows = [["When", "Amount"], ["01/02/2024 12:00:00 AM", 1.0], ["03/04/2024 12:00:00 AM", 2.0], ["05/06/2024 12:00:00 AM", 3.0]]
    transport = FakeTransport(lambda name, question, state: {"date": 0.9} if name.startswith("c") else {"day": 0.8} if name == "order" else {})
    catalog = Catalog.from_model(compile_workbook(_workbook(rows)))
    typed = _with_transport(transport, lambda: typed_dates(catalog))
    assert typed["01/02/2024 12:00:00 AM"] == "2024-02-01" and [list(body["questions"]) for body in transport.bodies] == [["c000"], ["order"]]
    shown = [["When", "Amount"], ["01/02/2024 12:00:00 AM", 1.0], ["25/06/2024 12:00:00 AM", 2.0], ["05/06/2024 12:00:00 AM", 3.0]]
    transport = FakeTransport(lambda name, question, state: {"date": 0.9} if name.startswith("c") else {})
    catalog = Catalog.from_model(compile_workbook(_workbook(shown)))
    typed = _with_transport(transport, lambda: typed_dates(catalog))
    assert typed["01/02/2024 12:00:00 AM"] == "2024-02-01" and typed["25/06/2024 12:00:00 AM"] == "2024-06-25", "the 25 says the day comes first"
    assert len(transport.bodies) == 1, "no order question"
    year_first = [["When", "Amount"], ["1999/07/01", 1.0], ["2001/12/25", 2.0], ["2003/01/09", 3.0]]
    transport = FakeTransport(lambda name, question, state: {"date": 0.9} if name.startswith("c") else {})
    catalog = Catalog.from_model(compile_workbook(_workbook(year_first)))
    assert _with_transport(transport, lambda: typed_dates(catalog)) == {"1999/07/01": "1999-07-01", "2001/12/25": "2001-12-25", "2003/01/09": "2003-01-09"}


def test_the_dev_replay_holds_each_question_to_its_latest_kept_measurement() -> None:
    """A kept change was measured by rerunning only the questions it touches, so a question's reference is the last
    layer that measured it; the base run's outcome of a remeasured question is no longer the stack's."""
    import gzip
    import tempfile
    from pathlib import Path
    from regress.run_dev_decisions import REFERENCE_TAGS, reference
    base, *layers = REFERENCE_TAGS["databench"]
    assert layers, "DataBench's kept changes were remeasured in layers"
    with tempfile.TemporaryDirectory() as folder:
        results = Path(folder) / "results"
        results.mkdir()
        def measured(tag, rows):
            with gzip.open(results / f"e2e_{tag}_stock_databench.jsonl.gz", "wt", encoding="utf-8") as sink:
                sink.write("".join(json.dumps(row) + "\n" for row in rows))
        measured(base, [{"id": "a", "tag": base}, {"id": "b", "tag": base}, {"id": "c", "tag": base}])
        for at, tag in enumerate(layers):
            measured(tag, [{"id": "b", "tag": tag}] + ([{"id": "c", "tag": tag}] if at == 0 else []))
        held = reference("databench", Path(folder))
    assert held["a"]["tag"] == base, "a question no kept change touched keeps the base measurement"
    assert held["b"]["tag"] == layers[-1], "the latest layer wins"
    assert held["c"]["tag"] == layers[0], "a layer holds until a later one remeasures the question"


TESTS = [
    test_a_candidates_probability_is_summed_over_the_same_answer, test_ties_go_to_the_earlier_listing, test_thresholds_ask_warn_and_answer,
    test_none_likelier_than_the_best_answer_asks, test_nothing_listed_asks, test_answer_key_tells_rows_apart,
    test_sheet_state_describes_every_column_with_three_examples, test_sheet_state_names_a_family_table_by_all_its_tabs, test_a_workbook_of_several_tabs_names_the_tab_in_view,
    test_reading_record_offers_each_slot_its_coordinates, test_written_day_questions_need_a_year_and_a_day_number,
    test_a_sheet_without_days_or_years_asks_no_period, test_fused_reading_reads_the_api_against_the_uniform_share_and_values_against_none,
    test_value_sets_ask_about_the_likeliest_values, test_choice_record_lists_each_candidate_with_its_answer_and_none,
    test_the_request_body_is_sorted_and_hashable, test_answer_reads_decodes_and_chooses_through_its_calls, test_said_spans_are_the_questions_own_words,
    test_said_words_join_the_reading_only_above_none, test_an_either_or_is_answered_in_the_questions_words, test_a_failed_call_reaches_the_caller,
    test_text_dates_are_typed_when_the_workbook_loads, test_digit_dates_ask_the_order_only_when_the_column_never_shows_it,
    test_the_dev_replay_holds_each_question_to_its_latest_kept_measurement,
]

if __name__ == "__main__":
    print("=== the program path with the Decisions API: records, fusion, the per-answer decision, date typing ===")
    failed = 0
    for t in TESTS:
        try:
            t()
        except Exception as exc:                                     # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {str(exc)[:600]}")
    print(f"test_answer: {len(TESTS) - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
