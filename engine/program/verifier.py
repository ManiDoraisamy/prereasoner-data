"""The verifier: run a decoder's top programs on the request's tables and say what each result shows, as named evidence. It decides nothing: the chooser (Phase 4) reads the evidence, today a caller may.

The evidence is design/answer_location.txt's. A shape and type check from the question alone flags wrong answers at almost no cost to right ones (section 5). A bare zero is no answer. A printed total ties a computed answer only when exact to 1e-6, nonzero, of 4+ significant digits, and the
line's label agrees with the program's filter: at the grader's 0.5% half of wrong sums "verify", at exact chance is 0.1-0.4%, and every long wrong sum that matched read the whole column (section 3). So a sum equal to a whole column's total when the program names a subset is a flag against it.
Agreement between siblings is worthless (sibling readers agree on 111 of 118 and it tells nothing), so no vote is here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from engine.program.execute import Result, run
from engine.program.index import index_of
from engine.program.language import Agg, Col, Equals, InSection, OnLine, Program, Show, addresses_printed, answer_type_of, conditions_of, measure_columns
from engine.program.names import (A_CODE, A_COUNT, A_DATE, A_LIST, A_NAME, A_NUMBER, A_PERCENT, COUNT, EV_BARE_ZERO, EV_EMPTY, EV_FAILED, EV_FILTER_DID_NOTHING, EV_FRACTION_FOR_COUNT, EV_MANY_ROWS, EV_PRINTED_TIE, EV_RAN, EV_REPEATED_LINES, EV_ROW_COUNT_FOR_AMOUNT, EV_ROW_DUMP, EV_SHAPE_AGREES,
                                  EV_TOO_LARGE_FOR_SHARE, EV_TYPE_AGREES, EV_WRONG_KIND, NAME_TYPES, SUM)
from engine.program.scoring import ScoredProgram
from engine.program.semantics import fold
from engine.program.store import RequestTables
from engine.sheet.names import PRINTED_ROLES, TOTAL

_ISO = re.compile(r"^\d{4}-\d{2}(-\d{2})?")
_TOTAL_WORD = re.compile(r"\b(?:grand\s+total|sub-?\s*total|totals?|net|sum)\b")
TIE_RELATIVE = 1e-6
TIE_ABSOLUTE = 1e-9
TIE_DIGITS = 4


@dataclass(frozen=True, slots=True)
class Evidence:
    """One named finding. holds says whether it is true of this result; detail is free text a person reads, an address or a count, never a decision."""
    name: str
    holds: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Verified:
    """A candidate with its result and its evidence, in the decoder's order."""
    scored: ScoredProgram
    result: Result | None
    evidence: tuple[Evidence, ...]

    def held(self, name: str) -> bool:
        return any(item.name == name and item.holds for item in self.evidence)


def _number(cell: object) -> float | None:
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    text = str(cell).strip()
    if _ISO.match(text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _kind(cell: object) -> str:
    if cell is None or (isinstance(cell, str) and not cell.strip()):
        return "null"
    if _number(cell) is not None:
        return "number"
    return "date" if isinstance(cell, str) and _ISO.match(cell.strip()) else "text"


def significant_digits(value: float) -> int:
    """Digits with trailing zeros off: 150,000 has 2, as answer_location.txt counts them."""
    text = f"{abs(value):.10f}".rstrip("0").rstrip(".").replace(".", "").lstrip("0")
    return len(text.rstrip("0")) if text else 0


def is_exact(a: float, b: float) -> bool:
    return abs(a - b) <= max(TIE_RELATIVE * abs(b), TIE_ABSOLUTE)


def shape_evidence(rows: tuple[tuple, ...], program: Program, expected: str | None) -> list[Evidence]:
    """The answer's shape against what the question expects. expected is a program answer type or a fine one, or None when nothing is known."""
    out: list[Evidence] = []
    if not rows:
        return [Evidence(EV_EMPTY, True)]
    first = rows[0]
    numbers = [number for number in map(_number, first) if number is not None]
    if len(rows) == 1 and len(first) == 1 and numbers and numbers[0] == 0:
        out.append(Evidence(EV_BARE_ZERO, True))
    if len(first) >= 4:
        out.append(Evidence(EV_ROW_DUMP, True))
    if expected is None:
        return out
    wants = A_NAME if expected in NAME_TYPES else expected
    kinds = {_kind(cell) for row in rows for cell in row} - {"null"}
    bad: str | None = None
    if wants in (A_NUMBER, A_COUNT, A_PERCENT):
        if len(rows) > 1:
            out.append(Evidence(EV_MANY_ROWS, True, f"{len(rows)} rows"))
            bad = EV_MANY_ROWS
        elif "number" not in kinds:
            bad = EV_WRONG_KIND
        elif wants in (A_NUMBER, A_PERCENT) and isinstance(program, Agg) and program.function == COUNT:
            bad = EV_ROW_COUNT_FOR_AMOUNT
        elif wants == A_COUNT and numbers and abs(numbers[0] - round(numbers[0])) > 1e-9:
            bad = EV_FRACTION_FOR_COUNT
        elif wants == A_PERCENT and numbers and abs(numbers[0]) > 100:
            bad = EV_TOO_LARGE_FOR_SHARE
    elif wants == A_DATE:
        if len(rows) > 3:
            bad = EV_MANY_ROWS
        elif "date" not in kinds and "text" not in kinds:
            bad = EV_WRONG_KIND
    elif wants == A_NAME:
        if len(rows) > 3:
            bad = EV_MANY_ROWS
        elif not kinds & {"text", "date"} and expected != A_CODE:
            bad = EV_WRONG_KIND
    elif wants == A_LIST:
        if "text" not in kinds and "date" not in kinds:
            bad = EV_WRONG_KIND
    if bad is not None and not any(item.name == bad for item in out):
        out.append(Evidence(bad, True))
    out.append(Evidence(EV_SHAPE_AGREES, bad is None))
    return out


def _label_agrees(line, program: Program) -> bool:
    """A printed line's label agrees with what a program keeps: it names one of the program's filter values, or, for a program with no filter, it is the table's own total."""
    label = fold(line.label)
    words: list[str] = []
    for condition in conditions_of(program):
        if isinstance(condition, Equals) and isinstance(condition.value, str):
            words.append(fold(condition.value))
        elif isinstance(condition, (InSection, OnLine)):
            words.append(fold(condition.text))
        elif hasattr(condition, "text"):
            words.append(fold(condition.text))
    bands = fold(" ".join(line.section))
    if words:
        return any(word and (word in label or word in bands) for word in words)
    return line.role == TOTAL or bool(_TOTAL_WORD.search(label))


def printed_tie_evidence(program: Program, value: float, tables: RequestTables) -> list[Evidence]:
    """Does this computed number sit exactly on a printed line of the column it adds? Only under answer_location.txt's conditions, and each line that matches is cited by address."""
    if not isinstance(program, Agg) or program.function != SUM or addresses_printed(program.where) or program.measure is None:
        return []
    out: list[Evidence] = []
    if value == 0 or significant_digits(value) < TIE_DIGITS:
        return out
    table = tables.catalog.table(program.table)
    columns = measure_columns(program.measure)
    if len(columns) != 1 or not isinstance(program.measure, Col):
        return out
    position = columns[0].position
    numbers = table.numbers[position]
    whole = sum(number for number, data in zip(numbers, table.is_data) if number is not None and data)
    for i, line in enumerate(table.lines):
        if line.role in PRINTED_ROLES and numbers[i] is not None and is_exact(numbers[i], value):
            if _label_agrees(line, program):
                cell = line.cells.get(position)
                out.append(Evidence(EV_PRINTED_TIE, True, cell.address if cell is not None else f"row {line.row}"))
                break
    if program.where and is_exact(whole, value) and significant_digits(whole) >= TIE_DIGITS:
        out.append(Evidence(EV_FILTER_DID_NOTHING, True, f"{len(numbers)} lines"))
    return out


def _repeated_lines(program: Program, tables: RequestTables) -> list[Evidence]:
    """A read that hits several lines takes the first, and says so."""
    if not isinstance(program, Show) or program.listing or not program.where:
        return []
    table = tables.catalog.table(program.table)
    index = index_of(table)
    mask = table.all_mask
    for condition in program.where:
        mask &= index.mask(condition)
    hits = bin(mask).count("1")
    return [Evidence(EV_REPEATED_LINES, True, f"{hits} lines")] if hits > 1 else []


def verify_one(scored: ScoredProgram, tables: RequestTables, expected: str | None) -> Verified:
    program = scored.program
    try:
        result = run(program, tables)
    except Exception as exc:                                         # a program of a valid form can still meet a value SQL refuses: it is a failed candidate, not a failed question
        return Verified(scored, None, (Evidence(EV_FAILED, True, type(exc).__name__),))
    evidence = [Evidence(EV_RAN, True)]
    rows = result.rows
    evidence.extend(shape_evidence(rows, program, expected))
    if expected is not None and rows:
        have = answer_type_of(program, tables.catalog.column_kind)
        wants = A_NAME if expected in NAME_TYPES else expected
        evidence.append(Evidence(EV_TYPE_AGREES, have == wants or {have, wants} == {A_NUMBER, A_PERCENT} or {have, wants} == {A_NUMBER, A_COUNT}))
    if rows and len(rows) == 1 and len(rows[0]) == 1:
        value = _number(rows[0][0])
        if value is not None:
            evidence.extend(printed_tie_evidence(program, value, tables))
    evidence.extend(_repeated_lines(program, tables))
    return Verified(scored, result, tuple(evidence))


def verify(programs, tables: RequestTables, expected: str | None = None, *, top: int = 50) -> tuple[Verified, ...]:
    """The first top programs, in order, each with its result and evidence."""
    return tuple(verify_one(scored, tables, expected) for scored in list(programs)[:top])


__all__ = ["Evidence", "Verified", "is_exact", "printed_tie_evidence", "shape_evidence", "significant_digits", "verify", "verify_one"]
