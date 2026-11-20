"""A question over a workbook, answered or asked back: the program path with the Decisions API in its two places.

  prepare   the workbook compiled (engine/sheet), its text dates typed by the API and the workbook compiled again with
            them (engine/program/dates.py), the decoder warmed, the request's SQLite tables built (store.py) and each
            table's targets listed for the stock reading.
  answer    the stock reading (stock.py) -> the reading record (closed.py) -> one decide call, and a second over the value
            sets when the value question has an answer; beside them the said call over the question's own words ->
            the fused reading -> the typed k-best decoder, top DECODE_K,
            every program verified on the request's tables -> the candidate order of the engine's prior chooser -> the
            candidates that returned rows -> the choice record -> one decide call -> the per-answer decision.

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
from engine.program.closed import (ANSWERED, ASKED, WARNED, answer_key, choice_record, choose_answer, fused_reading, reading_record, said_record,
                                   value_set_question)
from engine.program.dates import typed_dates
from engine.program.decoder import Decoder
from engine.program.language import Program, coordinates
from engine.program.output import read_as
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
    """One workbook, ready for questions: its catalog (text dates typed), decoder, request tables, each table's targets,
    the text dates typed (cell text -> ISO day or month), and the client's active sheet when it said one."""
    catalog: Catalog
    decoder: Decoder
    tables: RequestTables
    targets: dict[str, tuple[list[Target], list[tuple[str, ...]]]]
    typed: dict[str, str] = field(default_factory=dict)
    active_sheet: str | None = None


@dataclass(frozen=True)
class Answer:
    """What the path says back. kind: answered, warned (given, with the coordinates the runner-up answer reads differently
    in uncertain) or asked (program, rows and read_as are then the likeliest reading, to ask back about; empty when no
    candidate ran). mass: the likeliest answer's probability, summed over the candidates returning it.

    For evaluation and display: listed_* hold every candidate that returned rows, in decoder order (the choice's r00,
    r01, ...): its read-as line, its rows and its answer's identity (closed.answer_key); probabilities is the choice's
    answer (None when nothing ran); tokens are the input tokens the calls billed."""
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


def prepare(workbook: WorkbookPayload, active_sheet: str | None = None) -> Prepared:
    """A workbook ready for questions; active_sheet: the tab the client has in view, if it says one. Raises
    LLMUnavailable when the date-typing call fails."""
    with span("program_compile"):
        catalog = Catalog.from_model(compile_workbook(workbook))
    with span("decisions_dates"):
        typed = typed_dates(catalog)
    with span("program_prepare"):
        if typed:
            catalog = Catalog.from_model(compile_workbook(workbook, typed))
        decoder = Decoder(catalog)
        decoder.warm()
        tables = RequestTables(catalog)
        for table in catalog.tables:
            tables.ensure(table.id)
        targets = workbook_targets(catalog)
    return Prepared(catalog, decoder, tables, targets, typed, active_sheet)


def answer(question: str, prepared: Prepared, active_sheet: str | None = None) -> Answer:
    """One question over a prepared workbook. active_sheet: the tab the client has in view when the question is asked
    (prepare's when None); on a workbook of several tabs both records name it. Raises LLMUnavailable."""
    catalog = prepared.catalog
    in_view = active_sheet if active_sheet is not None else prepared.active_sheet
    with span("program_read"):
        named = stock_reader().read(question, catalog, prepared.decoder, prepared.targets)
        record = reading_record(question, named, catalog, in_view)
    said_state, said_questions, said_words = said_record(question)
    with span("decisions_read"), ThreadPoolExecutor(max_workers=1) as beside:
        # the said call reads the question alone, so it runs beside the reading call and the value-set call
        said_call = beside.submit(contextvars.copy_context().run, decide, said_state, said_questions) if said_questions else None
        reading = decide(record.state, record.questions)
        set_question, set_names = value_set_question(record, reading.fields)
        sets = decide(record.state, {"value_set": set_question}) if set_question else None
        said = said_call.result() if said_call is not None else None
    tokens = reading.tokens + (sets.tokens if sets else 0) + (said.tokens if said else 0)
    with span("program_pool"):
        fused = fused_reading(named, record, reading.fields, set_names, sets.fields["value_set"] if sets else None,
                              said_words, said.fields if said else None)
        decoded = prepared.decoder.decode(fused.reading, k=DECODE_K, weights=DEFAULT_WEIGHTS)
        verified = verify(decoded.programs, prepared.tables, fused.expected, top=DECODE_K)
        listed = [candidate for candidate in score_candidates(verified, fused, catalog, PRIOR) if candidate.rows]
        lines = tuple(read_as(candidate.verified.scored.program, catalog) for candidate in listed)
        answers = tuple(answer_key(candidate.rows) for candidate in listed)
    probabilities = None
    if listed:
        with span("decisions_choose"):
            state, questions = choice_record(catalog, question, [(line, candidate.rows) for line, candidate in zip(lines, listed)], in_view)
            choice = decide(state, questions)
        probabilities, tokens = choice.fields["reading"], tokens + choice.tokens
    decision = choose_answer(probabilities, answers)
    chosen = listed[decision.pick] if decision.pick is not None else None
    uncertain: tuple[str, ...] = ()
    if decision.kind == WARNED and decision.runner_up is not None:
        theirs = set(coordinates(listed[decision.runner_up].verified.scored.program))
        uncertain = tuple(name for name in coordinates(chosen.verified.scored.program) if name not in theirs)
    return Answer(decision.kind, chosen.verified.scored.program if chosen else None, chosen.rows if chosen else (),
                  lines[decision.pick] if chosen else "", decision.mass, uncertain, lines,
                  tuple(candidate.rows for candidate in listed), answers, probabilities, tokens)


__all__ = ["ANSWERED", "ASKED", "DECODE_K", "WARNED", "Answer", "Prepared", "answer", "prepare"]
