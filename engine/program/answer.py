"""A question over a workbook, answered or asked back: the program path with the Decisions API in its two places.

  prepare   the workbook compiled (engine/sheet), its text dates typed by the API and the workbook compiled again with
            them (engine/program/dates.py), the decoder warmed and each table's targets listed for the stock reading: once
            per workbook content (engine/prepared.py prepared_workbook keeps it), so it holds nothing of one request. A
            served request's links add the columns they reach (engine/program/connected.py), and the workbook so
            connected is prepared once per what it reaches (engine/prepared.py connected_workbook).
  convert   when the request's money columns can be read in other currencies, one closed question over the currencies:
            the one the question wants its answer in, if any, adds those columns converted.
  answer    on the request's tables (store.py, on the request's database): the stock reading (stock.py) -> the reading
            record (closed.py) -> one decide call, and a second over the value sets when the value question has an answer; beside them the said call over the question's own words and the order call over
            the question alone (whether it takes the first or the last lines of a table) ->
            the fused reading -> the typed k-best decoder, top DECODE_K,
            every program verified on the request's tables -> the candidate order of the engine's prior chooser -> the
            candidates that returned rows -> the choice record -> one decide call -> the per-answer decision
            (closed.choose_answer): the answer and its mark, from the choice's own probability. Nothing waits for
            another model.

No model writes SQL or a program here: the API returns probabilities over options the engine built, the decoder builds
every program from the reading, and the verifier runs each one. Every external call goes through engine.llm.decide, and
LLMUnavailable reaches the caller: a question is not answered from a reading the API did not make.
"""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from engine.llm import decide
from engine.named.chooser import PRIOR, score_candidates
from engine.named.units import Target
from engine.program.catalog import Catalog
from engine.program.closed import (ANSWERED, ERRED, WARNED, answer_key, choice_record, choose_answer, convert_choice, convert_record,
                                   fused_reading, order_record, reading_record, said_record, value_set_question)
from engine.program.dates import typed_dates
from engine.program.decoder import Decoder
from engine.program.language import Program, coordinates
from engine.program.output import in_header_words, read_as
from engine.program.scoring import DEFAULT_WEIGHTS
from engine.program.stock import stock_reader, workbook_targets
from engine.program.store import RequestTables
from engine.program.verifier import verify
from engine.request_timing import span
from engine.sheet import compile_workbook
from engine.sheet.payload import WorkbookPayload

DECODE_K = 50              # programs the decoder lists and the verifier runs


@dataclass
class Prepared:
    """One workbook, ready for questions: its catalog (text dates typed), decoder, each table's targets, the text dates
    typed (cell text -> ISO day or month) and, for a workbook a request's links connect (engine/program/connected.py),
    the column pairs its keys link. Nothing in it belongs to one request: the request's tables are built on the
    request's database (RequestTables)."""
    catalog: Catalog
    decoder: Decoder
    targets: dict[str, tuple[list[Target], list[tuple[str, ...]]]]
    typed: dict[str, str] = field(default_factory=dict)
    linked: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class Answer:
    """What the path says back. kind: answered (sure when the choice gives it closed.T_SURE or more: very likely
    correct; else likely correct), warned (likely wrong: given, with the coordinates the runner-up answer reads differently
    in uncertain; alike when the runner-up's reading reads word for word like it) or erred (very likely wrong: program,
    rows and read_as are then the likeliest reading, served when its retry cannot answer; empty when no candidate ran).
    mass: the likeliest answer's probability, summed over the candidates returning it.

    For serving, evaluation and display: listed_* hold every candidate that returned rows, in decoder order (the choice's
    r00, r01, ...): its program, read-as line, rows and its answer's identity (closed.answer_key); pick is the likeliest
    answer's first listing (None when nothing ran) and runner_up the next answer's; probabilities is the choice's answer
    (None when nothing ran); tokens are the input tokens the calls billed."""
    kind: str
    program: Program | None
    rows: tuple[tuple, ...]
    read_as: str
    mass: float | None
    uncertain: tuple[str, ...]
    listed_read_as: tuple[str, ...]
    listed_rows: tuple[tuple[tuple, ...], ...]
    listed_answers: tuple[str, ...]
    probabilities: dict[str, float] | None
    tokens: int
    listed_programs: tuple[Program, ...] = ()
    pick: int | None = None
    runner_up: int | None = None
    alike: bool = False
    sure: bool = False


def prepare(workbook: WorkbookPayload) -> Prepared:
    """A workbook ready for questions. Raises LLMUnavailable when the date-typing call fails."""
    with span("program_compile"):
        catalog = Catalog.from_model(compile_workbook(workbook))
    with span("decisions_dates"):
        typed = typed_dates(catalog)
    if typed:
        with span("program_compile"):
            catalog = Catalog.from_model(compile_workbook(workbook, typed))
    return prepare_catalog(catalog, typed)


def prepare_catalog(catalog: Catalog, typed: dict[str, str] | None = None, linked: frozenset[tuple[str, str]] = frozenset()) -> Prepared:
    """A compiled workbook's catalog ready for questions: the decoder warmed and each table's targets listed. typed: the
    text dates its compile read; linked: the column pairs its keys link (engine/program/connected.py)."""
    with span("program_prepare"):
        decoder = Decoder(catalog)
        decoder.warm()
        targets = workbook_targets(catalog)
    return Prepared(catalog, decoder, targets, dict(typed or {}), linked)


def convert_target(question: str, catalog: Catalog, targets: list[str], active_sheet: str | None = None) -> str | None:
    """The currency the question wants its answer in, among ``targets``, or None: one closed question, asked before the
    reading because it decides whether the reading is offered the money columns converted into it
    (engine/program/connected.py Conversion). Raises LLMUnavailable."""
    state, questions, criteria = convert_record(catalog, question, targets, active_sheet)
    with span("decisions_convert"):
        decision = decide(state, questions)
    return convert_choice(decision.fields["convert"], criteria)


def answer(question: str, prepared: Prepared, tables: RequestTables, active_sheet: str | None = None) -> Answer:
    """One question over a prepared workbook, its programs run on tables (the request's, over prepared.catalog).
    active_sheet: the tab the client has in view when the question is asked; on a workbook of several tabs both records
    name it. The answer comes back with its mark, from the choice's own probability (closed.choose_answer): no other
    model is asked about it. Raises LLMUnavailable."""
    catalog = prepared.catalog
    in_view = active_sheet
    with span("program_read"):
        named = stock_reader().read(question, catalog, prepared.decoder, prepared.targets)
        record = reading_record(question, named, catalog, in_view)
    said_state, said_questions, said_words = said_record(question)
    order_state, order_questions = order_record(question)
    with span("decisions_read"), ThreadPoolExecutor(max_workers=2) as beside:
        # the said and order calls read the question alone, so they run beside the reading call and the value-set call
        said_call = beside.submit(contextvars.copy_context().run, decide, said_state, said_questions) if said_questions else None
        order_call = beside.submit(contextvars.copy_context().run, decide, order_state, order_questions)
        reading = decide(record.state, record.questions)
        set_question, set_names = value_set_question(record, reading.fields)
        sets = decide(record.state, {"value_set": set_question}) if set_question else None
        said = said_call.result() if said_call is not None else None
        order = order_call.result()
    tokens = reading.tokens + (sets.tokens if sets else 0) + (said.tokens if said else 0) + order.tokens
    with span("program_pool"):
        fused = fused_reading(named, record, reading.fields, set_names, sets.fields["value_set"] if sets else None,
                              said_words, said.fields if said else None, prepared.linked, order.fields)
        decoded = prepared.decoder.decode(fused.reading, k=DECODE_K, weights=DEFAULT_WEIGHTS)
        verified = verify(decoded.programs, tables, fused.expected, top=DECODE_K)
        listed = [candidate for candidate in score_candidates(verified, fused, catalog, PRIOR) if candidate.rows]
        lines = tuple(read_as(candidate.verified.scored.program, catalog) for candidate in listed)
        answers = tuple(answer_key(candidate.rows) for candidate in listed)
    probabilities = None
    if listed:
        with span("decisions_choose"):
            state, questions = choice_record(catalog, question, [(line, candidate.rows) for line, candidate in zip(lines, listed)], in_view)
            choice = decide(state, questions)
        probabilities, tokens = choice.fields["reading"], tokens + choice.tokens
    worded = tuple(in_header_words(candidate.verified.scored.program, catalog) for candidate in listed)
    decision = choose_answer(probabilities, answers, worded)
    chosen = listed[decision.pick] if decision.pick is not None else None
    uncertain: tuple[str, ...] = ()
    if decision.kind == WARNED and decision.runner_up is not None:
        theirs = set(coordinates(listed[decision.runner_up].verified.scored.program))
        uncertain = tuple(name for name in coordinates(chosen.verified.scored.program) if name not in theirs)
    return Answer(decision.kind, chosen.verified.scored.program if chosen else None, chosen.rows if chosen else (),
                  lines[decision.pick] if chosen else "", decision.mass, uncertain, lines,
                  tuple(candidate.rows for candidate in listed), answers, probabilities, tokens,
                  tuple(candidate.verified.scored.program for candidate in listed), decision.pick, decision.runner_up,
                  decision.alike is not None, decision.sure)


__all__ = ["ANSWERED", "DECODE_K", "ERRED", "WARNED", "Answer", "Prepared", "answer", "convert_target", "prepare", "prepare_catalog"]
