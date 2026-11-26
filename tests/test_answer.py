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
from engine import config
from engine.answer_presentation import warned_reason
from engine.program.answer import ANSWERED, ERRED, WARNED, answer, prepare
from engine.program.closed import DOUBT_ANOTHER, DOUBT_NONE, DOUBT_UNREAD, UNJUDGED
from engine.program.catalog import Catalog
from engine.program.closed import (CHOICE_NONE, CHOOSE_K, NONE, SAID_OPTIONS_MOST, T_ERR, T_FLAG, TAB_IN_VIEW_CONVENTION, W, answer_key, choice_record, choose_answer, fused_reading, preview,
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


def _no_retry_call(_payload):
    """A hermetic test's retry model (engine/llm.py QUERY_TRANSPORT): none, so a retry fails as an outage would, and no
    test reaches the network; a test of the retry sets its own (tests/test_sql_ast.py _neon)."""
    raise llm.LLMUnavailable("no retry model in a hermetic test")


class FakeJudge:
    """A hermetic test's second judge (engine/llm.py JUDGE_TRANSPORT): it reads the judge's body back into the choice's
    question and picks the option the test's own rule favours most for it (rule("reading", question, state), the first
    option when it favours none), so it agrees with a fake choice answering by the same rule; a test of a disagreeing
    judge gives it another rule. With no rule the judge fails as an outage would, and no test reaches the network."""

    def __init__(self, rule=None):
        self.rule, self.bodies = rule, []

    def __call__(self, payload: bytes) -> dict:
        if self.rule is None:
            raise llm.LLMUnavailable("no second judge in a hermetic test")
        body = json.loads(payload.decode("utf-8"))
        self.bodies.append(body)
        prompt = body["contents"][0]["parts"][0]["text"]
        state_text, rest = prompt[len("STATE:\n"):].split("\n\nQUESTION: ", 1)
        instructions, listed = rest.split("\n\nOPTIONS:\n", 1)
        keys = body["generationConfig"]["responseJsonSchema"]["properties"]["choice"]["enum"]
        criteria = dict(line.split(": ", 1) for line in listed.split("\n"))
        assert list(criteria) == keys, "the options listed are the keys the answer may be"
        favoured = self.rule("reading", {"instructions": instructions, "criteria": criteria}, json.loads(state_text)) or {}
        choice = max(favoured, key=lambda key: (favoured[key], -keys.index(key))) if favoured else keys[0]
        return {"text": json.dumps({"choice": choice}), "usage": {}, "seconds": 0.0}


def _with_transport(transport, call, judge=None):
    """``call`` with the Decisions transport replaced by ``transport``, no retry model, and the second judge a FakeJudge
    answering by the transport's own rule (``judge``: another judge transport)."""
    held = llm.DECISIONS_TRANSPORT
    held_query, held_judge = llm.QUERY_TRANSPORT, llm.JUDGE_TRANSPORT
    llm.DECISIONS_TRANSPORT = transport
    if llm.QUERY_TRANSPORT is llm._post_query:
        llm.QUERY_TRANSPORT = _no_retry_call
    if judge is not None or llm.JUDGE_TRANSPORT is llm._post_judge:
        llm.JUDGE_TRANSPORT = judge if judge is not None else FakeJudge(getattr(transport, "rule", None))
    try:
        return call()
    finally:
        llm.DECISIONS_TRANSPORT = held
        llm.QUERY_TRANSPORT, llm.JUDGE_TRANSPORT = held_query, held_judge


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


def test_the_answer_is_served_by_the_reading_the_choice_preferred() -> None:
    """Of the listings returning the likeliest answer, the one the choice gave the most probability is served ("total
    amount in France" was served as 'the size of the total of amount ... (all country → currency together)', its
    answer's first listing, where the choice preferred another reading of the same rows); the decision and the
    runner-up are the answer's, as before."""
    choice = choose_answer({"r00": 0.1, "r01": 0.2, "r02": 0.5, NONE: 0.05}, ["a", "b", "a"])
    assert choice.pick == 2 and choice.runner_up == 1 and abs(choice.mass - 0.6) < 1e-12 and choice.kind == ANSWERED
    # contrastive: the first listing is served where it is the choice's likeliest of its answer
    assert choose_answer({"r00": 0.5, "r01": 0.2, "r02": 0.1}, ["a", "b", "a"]).pick == 0


def test_an_answer_worded_like_another_is_warned_never_asked_back() -> None:
    """tests.test_scale: "total Amount" on a sheet with two columns headed Amount was answered 40 at 0.87, unwarned: both
    columns' totals read "the total of Amount on orders", so the question could not tell which was computed. It was
    asked back (65658636) until the owner's order of 2026-11-21, "never ask back unless the question is wrong": an
    answer one of whose readings reads word for word like an offered reading of another answer is served warned, its
    ``alike`` the other answer's listing (the reason names it). Asked back, it moved four dev answers, two of them right
    (nt-11106, FD-0172)."""
    lines = ['the total of Amount on "orders"', 'the total of Amount on "orders"', 'the number of lines on "orders"']
    alike = choose_answer({"r00": 0.87, "r01": 0.1, "r02": 0.01}, ["a", "b", "c"], lines)
    assert alike.kind == WARNED and alike.pick == 0 and alike.alike == 1 and alike.runner_up == 1
    # contrastive: the same words for the same answer, and different words for different answers, are answered
    assert choose_answer({"r00": 0.87, "r01": 0.1}, ["a", "a"], lines[:2]).kind == ANSWERED
    assert choose_answer({"r00": 0.87, "r01": 0.1}, ["a", "b"], ['the total of Amount on "orders"', 'the total of Units on "orders"']).kind == ANSWERED
    # negative: a reading past the choice's offer (CHOOSE_K) is no clash
    far = ["x"] + ["y"] * (CHOOSE_K - 1) + ["x"]
    assert choose_answer({"r00": 0.9}, ["a"] + ["b"] * CHOOSE_K, far).kind == ANSWERED


def test_ties_go_to_the_earlier_listing() -> None:
    choice = choose_answer({"r00": 0.25, "r01": 0.25, "r02": 0.25, "r03": 0.25}, ["a", "b", "a", "b"])
    assert choice.pick == 0 and choice.runner_up == 1


def test_thresholds_err_warn_and_answer() -> None:
    erred = choose_answer({"r00": T_ERR - 0.01, "r01": 0.2, NONE: 0.1}, ["a", "b"])
    assert erred.kind == ERRED and erred.pick == 0, "most likely wrong, still served (retried in serving), never asked"
    assert choose_answer({"r00": T_ERR, "r01": 0.2, NONE: 0.1}, ["a", "b"]).kind == WARNED
    assert choose_answer({"r00": T_FLAG - 1e-9, NONE: 0.1}, ["a"]).kind == WARNED
    assert choose_answer({"r00": T_FLAG, NONE: 0.1}, ["a"]).kind == ANSWERED


def test_none_likelier_than_the_best_answer_errs() -> None:
    choice = choose_answer({"r00": 0.45, NONE: 0.55}, ["a"])
    assert choice.kind == ERRED and choice.pick == 0 and choice.none == 0.55


def test_nothing_listed_errs() -> None:
    assert choose_answer(None, []).kind == ERRED and choose_answer(None, []).pick is None
    assert choose_answer({NONE: 1.0}, []).kind == ERRED


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


def test_a_year_written_with_letters_offers_the_ten_or_hundred_years_it_begins() -> None:
    """"How many orders in the 1970s", "parishes founded in the 1800's": 44 of WikiTableQuestions' 11,321 training
    questions outside dev name ten or a hundred years by the year they begin, written with letters glued on, which read
    as an amount (1970) or nothing (1800 is no year the reader reads alone). Such a year reads as a period, never an
    amount, and the stated period offers the ten years it begins and, ending in 00, the hundred, for the API to choose;
    a year written alone is offered no span of years it begins."""
    catalog = _catalog()
    def spans(question):
        named = _stock_reading(question, catalog)
        assert not any(name.startswith(f"{SLOT_NUMBER}:") and name.split(":", 1)[1][:2] in ("18", "19") for name in named.reading.scores), "no amount"
        return set(reading_record(question, named, catalog).periods.values())
    assert coordinate(SLOT_PERIOD, "1970-01-01..1979-12-31") in spans("how many orders were placed in the 1970s?")
    eighteen = spans("how many orders in the 1800's?")
    assert {coordinate(SLOT_PERIOD, "1800-01-01..1809-12-31"), coordinate(SLOT_PERIOD, "1800-01-01..1899-12-31")} <= eighteen
    assert not any(name.endswith("..2029-12-31") or name.endswith("..2119-12-31") for name in spans("orders in 2020"))


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
    """The reading call (and the value-set call), the said and order calls beside them (the order call's four questions:
    the first or last lines, a run, a median or standard deviation, a range between two numbers), then the choice call."""
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
    assert names[-1] == ["reading"] and ["said_above", "said_below"] in names and ["line_order", "line_run", "number_range", "series_figure"] in names and any("value" in called for called in names), names
    assert len(names) in (4, 5), names
    assert said.listed_rows and said.probabilities is not None and len(said.probabilities) == min(len(said.listed_rows), CHOOSE_K) + 1
    assert said.listed_answers == tuple(answer_key(rows) for rows in said.listed_rows)
    expected = choose_answer(said.probabilities, said.listed_answers)
    assert said.kind == expected.kind and said.rows == said.listed_rows[expected.pick] and said.read_as == said.listed_read_as[expected.pick]
    assert said.tokens == 7 * len(transport.bodies)
    assert ((240.5,),) in said.listed_rows, "the sum of Amount for East is listed"
    assert len(said.listed_programs) == len(said.listed_rows) and said.pick == expected.pick and said.runner_up == expected.runner_up
    assert said.program is said.listed_programs[said.pick]


def test_an_answer_stays_unmarked_only_where_the_second_judge_picks_it() -> None:
    """Two judges that must agree (owner, 2026-11-26): on unseen tables 13.6% and 12.8% of answers were given unmarked
    and wrong, and no threshold on the choice's own probability reaches 8% within 20% marked. An answer the choice would
    give unmarked stays unmarked only where the second judge (engine/llm.py judge) picks a listing of the same answer;
    another answer warns it, with the judge's reading as what it may also mean, none warns it, and so does a judge that
    did not answer. The answer served is the choice's in every case, and an answer the choice itself warned or erred is
    as the choice left it. The path asks the judge the choice's own question, beside the choice."""
    answers = ["a", "a", "b", "c"]
    sure = {"r00": 0.5, "r01": 0.3, "r02": 0.1, "r03": 0.05, NONE: 0.05}
    alone = choose_answer(sure, answers)
    assert (alone.kind, alone.pick, alone.runner_up, alone.doubt) == (ANSWERED, 0, 2, None)
    agreed = choose_answer(sure, answers, judged="r01")       # another listing of the same answer
    assert (agreed.kind, agreed.pick, agreed.runner_up, agreed.doubt) == (ANSWERED, 0, 2, None)
    another = choose_answer(sure, answers, judged="r03")
    assert (another.kind, another.pick, another.runner_up, another.doubt) == (WARNED, 0, 3, DOUBT_ANOTHER)
    nothing = choose_answer(sure, answers, judged=NONE)
    assert (nothing.kind, nothing.pick, nothing.runner_up, nothing.doubt) == (WARNED, 0, 2, DOUBT_NONE)
    unread = choose_answer(sure, answers, judged=UNJUDGED)
    assert (unread.kind, unread.pick, unread.doubt) == (WARNED, 0, DOUBT_UNREAD)
    unsure = {"r00": 0.3, "r01": 0.2, "r02": 0.3, "r03": 0.1, NONE: 0.1}      # 0.5: warned by the choice alone
    left = choose_answer(unsure, answers, judged="r03")
    assert choose_answer(unsure, answers).kind == WARNED and (left.kind, left.runner_up, left.doubt) == (WARNED, 2, None)
    lost = choose_answer({"r00": 0.2, "r02": 0.1, NONE: 0.7}, answers, judged="r00")
    assert (lost.kind, lost.doubt) == (ERRED, None)

    _use_fake_reader()
    prepared = _with_transport(FakeTransport(lambda *a: {}), lambda: prepare(_workbook()))
    question = "total amount for East"

    def rule(name, criteria_question, state):     # the choice holds its first listing likeliest
        if name == "reading":
            return {next(option for option in criteria_question["criteria"] if option != NONE): 0.8}
        return _reading_rule(name, criteria_question, state)

    def ask(judge):
        return _with_transport(FakeTransport(rule), lambda: answer(question, prepared, RequestTables(prepared.catalog)), judge)
    same = FakeJudge(rule)
    both = ask(same)
    assert (both.kind, both.doubt) == (ANSWERED, None)
    body = same.bodies[0]
    assert body["model"] == config.JUDGE_MODEL and body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
    keys = body["generationConfig"]["responseJsonSchema"]["properties"]["choice"]["enum"]
    assert keys == [f"r{at:02d}" for at in range(len(both.listed_rows))] + [NONE], "the choice's own options, none last"
    other = next(at for at, key in enumerate(both.listed_answers) if key != both.listed_answers[both.pick])
    doubted = ask(FakeJudge(lambda name, criteria_question, state: {f"r{other:02d}": 0.9}))
    assert (doubted.kind, doubted.doubt, doubted.runner_up) == (WARNED, DOUBT_ANOTHER, other)
    assert doubted.rows == both.rows and doubted.read_as == both.read_as, "the answer served is the choice's"
    assert doubted.uncertain, "what the judge's reading reads differently is named"
    assert (ask(FakeJudge(lambda name, criteria_question, state: {NONE: 0.9})).doubt, ask(FakeJudge()).doubt) == (DOUBT_NONE, DOUBT_UNREAD)
    reason = warned_reason(doubted.read_as, doubted.listed_read_as[other], doubted.mass, False, doubted.doubt)
    assert reason.startswith(f"I read this as “{doubted.read_as}”") and reason.endswith("”, which gives another answer.") and "a second check read it as" in reason
    assert warned_reason("the total of Amount", None, 0.9, False, DOUBT_NONE) == "I read this as “the total of Amount”, 90% sure, but a second check found no reading that answers the question as asked."
    assert warned_reason("the total of Amount", None, 0.9, False, DOUBT_UNREAD).endswith("; the second check of this reading did not run.")


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


def test_a_batch_another_run_is_writing_is_read_to_its_last_whole_line() -> None:
    """Two runs on one checkout share the recorded answers: one reads every batch while another appends to its own. On
    2026-11-24 a run started while the dev gate was writing, read a half-written last line, and failed all 180 of its
    questions with a JSON error. A line not yet ended is a write in progress and is read once it is whole."""
    import tempfile
    from regress import eval_sets
    with tempfile.TemporaryDirectory() as folder:
        from pathlib import Path
        Path(folder, "batch.jsonl").write_bytes(b'{"hash": "a1", "answers": {}, "tokens": 3}\n{"hash": "b2", "answ')
        store = eval_sets.CallStore(Path(folder))
        assert store.get("a1") == {"answers": {}, "tokens": 3} and store.get("b2") is None


def test_packing_a_batch_again_keeps_what_was_packed() -> None:
    """On 2026-11-24 the calls were packed while a long run was still writing its batch: the run started the plain file
    again, and packing it later would have replaced the packed lines with the new ones alone. Packing adds to them."""
    import gzip
    import tempfile
    from pathlib import Path
    from regress import eval_sets
    with tempfile.TemporaryDirectory() as folder:
        batch = Path(folder, "run-7.jsonl")
        batch.write_bytes(b'{"hash": "b2", "answers": {}, "tokens": 2}\n{"hash": "a1", "answers": {}, "tokens": 1}\n')
        eval_sets.pack(Path(folder))
        batch.write_bytes(b'{"hash": "c3", "answers": {}, "tokens": 3}\n')
        eval_sets.pack(Path(folder))
        packed = gzip.decompress(Path(folder, "run-7.jsonl.gz").read_bytes()).decode("utf-8").splitlines()
        assert [line[10:12] for line in packed] == ["a1", "b2", "c3"] and not batch.exists()
        store = eval_sets.CallStore(Path(folder))
        assert store.get("c3") == {"answers": {}, "tokens": 3} and store.get("a1") is not None


def test_the_dev_replay_runs_from_a_clean_clone() -> None:
    """The dev replay's reference and the Decisions answers it reads are committed (regress/sets): each set's reference
    holds every one of its questions once, a served label each, and the recorded answers hold every call the reference
    made, so a clone replays the 896 dev questions and the 91 demo ones without the API or any file of this machine."""
    from regress import eval_sets
    from regress.run_dev_decisions import SETS, reference
    sizes = {"messy": 120, "wtq": 300, "databench": 300, "freshdev": 176, "demo": 91}
    store = eval_sets.CallStore()
    for name in SETS:
        held = reference(name)
        assert len(held) == sizes[name], (name, len(held))
        assert all(row["label"] in ("right", "flagged_right", "flagged_wrong", "wrong", "asked") for row in held.values()), name
        missing = [digest for row in held.values() for digest in row.get("calls") or [] if store.get(digest) is None]
        assert not missing, f"{name}: {len(missing)} calls of the reference are not recorded"


def test_a_warmed_reader_owns_its_weights_and_reads_the_same() -> None:
    # 2026-11-25: an instance's first question spent about 8 s on each stock reading (24 and 37 s on its first request)
    # and later questions 0.2 s: the backbone's weights stay mapped from their file until first read. warm() copies
    # them into the process's own memory and encodes once at every length, into no cache, and reads nothing new.
    class Backbone(torch.nn.Module):
        dim = 4

        def __init__(self):
            super().__init__()
            self.weight = torch.nn.Parameter(torch.arange(16, dtype=torch.float32).reshape(4, 4) / 16)
            self.register_buffer("offset", torch.ones(4))
            self.lengths = []

        def encode_texts(self, texts, *, max_tokens=32, batch=32):
            distinct = sorted(set(texts), key=lambda text: (len(text), text))
            self.lengths.extend(len(text.split()) for text in distinct)
            rows = [torch.tensor([float(len(text)), float(sum(map(ord, text)) % 7), 1.0, 0.0]) for text in distinct]
            return distinct, (torch.stack(rows) @ self.weight + self.offset) if rows else torch.zeros((0, self.dim))

        def text_tokens(self, texts, *, max_tokens=32):
            return [len(text.split()) + 2 for text in texts]

    backbone = Backbone()
    reader = stock.StockReader(backbone)
    before = reader.encode(["total amount", "Paris"])
    reader.cache.clear()
    storages = (backbone.weight.data_ptr(), backbone.offset.data_ptr())
    assert reader.warm() is reader
    assert backbone.weight.data_ptr() != storages[0], "the weights were not copied"
    assert backbone.offset.data_ptr() != storages[1], "the buffers were not copied"
    assert set(backbone.lengths) >= set(range(1, stock.TEXT_TOKENS + 1)), "a token length was never encoded"
    assert not reader.cache, "warming wrote the cache"
    assert torch.allclose(torch.from_numpy(reader.encode(["total amount", "Paris"])), torch.from_numpy(before)), "warming changed a reading"


TESTS = [
    test_a_warmed_reader_owns_its_weights_and_reads_the_same,
    test_a_candidates_probability_is_summed_over_the_same_answer, test_the_answer_is_served_by_the_reading_the_choice_preferred, test_an_answer_worded_like_another_is_warned_never_asked_back, test_ties_go_to_the_earlier_listing, test_thresholds_err_warn_and_answer,
    test_none_likelier_than_the_best_answer_errs, test_nothing_listed_errs, test_answer_key_tells_rows_apart,
    test_sheet_state_describes_every_column_with_three_examples, test_sheet_state_names_a_family_table_by_all_its_tabs, test_a_workbook_of_several_tabs_names_the_tab_in_view,
    test_reading_record_offers_each_slot_its_coordinates, test_written_day_questions_need_a_year_and_a_day_number,
    test_a_year_written_with_letters_offers_the_ten_or_hundred_years_it_begins, test_a_sheet_without_days_or_years_asks_no_period, test_fused_reading_reads_the_api_against_the_uniform_share_and_values_against_none,
    test_value_sets_ask_about_the_likeliest_values, test_choice_record_lists_each_candidate_with_its_answer_and_none,
    test_the_request_body_is_sorted_and_hashable, test_answer_reads_decodes_and_chooses_through_its_calls, test_said_spans_are_the_questions_own_words,
    test_said_words_join_the_reading_only_above_none, test_an_either_or_is_answered_in_the_questions_words, test_an_answer_stays_unmarked_only_where_the_second_judge_picks_it,
    test_a_failed_call_reaches_the_caller,
    test_a_batch_another_run_is_writing_is_read_to_its_last_whole_line, test_packing_a_batch_again_keeps_what_was_packed, test_text_dates_are_typed_when_the_workbook_loads, test_digit_dates_ask_the_order_only_when_the_column_never_shows_it,
    test_the_dev_replay_runs_from_a_clean_clone,
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
