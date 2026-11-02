"""The semantics a program does not choose: one reading of a cell and of a question's sign, period and unit words.

Each rule here came from running 186 hand-written programs over real workbooks and flipping the rule to see which answers depend on it (design/program_language.txt, section 4). SEMANTIC_COUNTS holds those question counts beside the
rule, so a read-as line or a test can cite why a rule is what it is. No score reaches this module: the same slots always give the same program and the same answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from engine.numeric import canonical_decimal
from engine.program.names import (HIGHEST, LOWEST, PERIOD_NAMED, PERIOD_REST_OF, PERIOD_SO_FAR, SEM_BLANKS, SEM_FOLD, SEM_MIXED, SEM_PARTIAL, SEM_PERIODS, SEM_PRINTED, SEM_REPEATED, SEM_SIGNS, SEM_TIES, SEM_UNITS, SIGN_LOSS, SIGN_MAGNITUDE,
                                  SIGN_NET, SIGN_SIZE)
from engine.sheet.model import Cell
from engine.sheet.names import V_BLANK, V_DATE, V_MARKER, V_NUMBER, V_PERIOD
from engine.sheet.values import parse_number, parse_period, serial_to_date

SEMANTIC_COUNTS = {
    SEM_SIGNS: "37 questions; 15 of 18 direction or size programs fail if read as the plain largest signed value",
    SEM_BLANKS: "19 programs keep a line with a blank measure; 2 fail if a blank counts 0",
    SEM_PRINTED: "28 exclude printed lines, 35 read one as the answer; 10 fail if printed lines count as data",
    SEM_TIES: "1 question: its answer changes if ties break by sheet order",
    SEM_PERIODS: "32 programs use a period, 55 more name one the tab already is",
    SEM_UNITS: "21 questions",
    SEM_PARTIAL: "20 fail if a word must be the whole cell",
    SEM_FOLD: "37 questions hold a value differing from its cell only by case; 3 fail against the stored spelling",
    SEM_MIXED: "10 measure columns hold text among their data lines",
    SEM_REPEATED: "3 reads hit several lines, 1 distinct count, 1 latest version, 6 tables repeat their header inside the data",
}

# Numbers in this range, whole, unformatted, in a column of them, are spreadsheet serial days: 1954 to 2064.
SERIAL_DAY_FIRST = 20000
SERIAL_DAY_LAST = 60000


def fold(value: object) -> str:
    """Compare folded: trimmed, spaces collapsed, case ignored."""
    return " ".join(str(value).split()).casefold()


def number_text(value: float | int | Decimal) -> str:
    """A number as exact text: 36.0 is "36", 0.1 is "0.1"."""
    return canonical_decimal(Decimal(str(value)))


def cell_number(cell: Cell | None) -> float | None:
    """A number column reads numbers; every other cell is blank for a measure, so a text NaN among figures neither counts nor stops a sum. A date is not a number."""
    if cell is None or cell.kind != V_NUMBER or isinstance(cell.value, bool):
        return None
    return float(cell.value)


def cell_day(cell: Cell | None) -> str | None:
    """The first ISO day a cell names: a date, a period with a year, or a whole number in the serial-day range. A number is a day only where a condition reads it as one."""
    if cell is None:
        return None
    if cell.kind in (V_DATE, V_PERIOD):
        return cell.period.start if cell.period is not None else None
    if cell.kind == V_NUMBER and is_serial_day(cell.value):
        day = serial_to_date(float(cell.value))
        return day.isoformat() if day is not None else None
    return None


def is_serial_day(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and SERIAL_DAY_FIRST <= value <= SERIAL_DAY_LAST and float(value).is_integer()


def cell_text(cell: Cell | None) -> str | None:
    """A cell as an answer shows it: a number as exact text, a date as its ISO day, anything else as the sheet wrote it, spaces cleaned. A blank is None."""
    if cell is None or cell.kind == V_BLANK:
        return None
    if cell.kind == V_NUMBER and not isinstance(cell.value, bool):
        return number_text(cell.value)
    if cell.kind == V_DATE and cell.value is not None:
        return str(cell.value)
    if cell.kind == V_PERIOD and cell.period is not None and cell.period.start is not None and not isinstance(cell.raw, str):
        return cell.period.start          # a serial day formatted to show a month or a year: its day, not its serial
    text = " ".join(str(cell.raw).split())
    return text or None


def cell_fold(cell: Cell | None) -> str | None:
    text = cell_text(cell)
    return None if text is None else fold(text)


def is_filled(cell: Cell | None) -> bool:
    """A blank can be asked for; a marker such as "-" or "n/a" is a filled cell."""
    return cell is not None and (cell.kind == V_MARKER or cell_text(cell) is not None)


def exact_wins(text: str, cells: set[str]) -> bool:
    """A partial word reads as the whole cell when some cell is exactly it: Transco is not Transcolorado."""
    return fold(text) in cells


# ------------------------------------------------------------------------------------------------------------ signs
_SIGN_WORDS = (
    (SIGN_SIZE, re.compile(r"\b(biggest (?:driver|payment|swing|move|change)|moved? (?:the )?most|out of balance|by size|in absolute|largest (?:change|move|swing|variance)|biggest by)\b")),
    (SIGN_LOSS, re.compile(r"\b(loss|losses|lost|losing|owing|owes?|owed|short|most sold|sold the most|largest credit|deficit|negative)\b")),
    (SIGN_MAGNITUDE, re.compile(r"\b(burn|burned|consumption|consumed|payment|paid|amount due)\b")),
    (SIGN_NET, re.compile(r"\b(net|position|value)\b")),
)


@dataclass(frozen=True, slots=True)
class SignReading:
    """How a figure with a sign is read: which way it ranks, by size or as stored, and whether the result is stated by its size."""
    direction: str
    by_size: bool = False
    magnitude: bool = False


def sign_word_of(question: str) -> str | None:
    """The sign word of a question, or None. A closed list, in order of which reading overrides which."""
    low = fold(question)
    for name, pattern in _SIGN_WORDS:
        if pattern.search(low):
            return name
    return None


def read_sign(word: str | None, stored_negative: bool, asked: str = HIGHEST) -> SignReading:
    """Net, position, value: the signed sum, as asked. A loss, owing, short, most sold: the lowest where the column stores them negative, the sign as stored; a column of positive losses ranks as asked. Biggest driver, moved most,
    out of balance, biggest payment: by size. A burn, a payment or an amount due stored negative is stated as its size."""
    if word == SIGN_LOSS:
        return SignReading(LOWEST if stored_negative else asked)
    if word == SIGN_SIZE:
        return SignReading(HIGHEST, by_size=True)
    if word == SIGN_MAGNITUDE:
        return SignReading(asked, magnitude=stored_negative)
    if word in (SIGN_NET, None):
        return SignReading(asked)
    raise ValueError(f"unknown sign word: {word}")


# ---------------------------------------------------------------------------------------------------------- periods
_SO_FAR = re.compile(r"\b(so far|to date|thus far|as of now)\b")
_REST_OF = re.compile(r"\b(rest|remainder|remaining|balance) of\b")
_CALENDAR = re.compile(r"\b(?:calendar|cal|cy)[\s'-]*(\d{4}|\d{2})\b")
_YEARLESS_QUARTER = re.compile(r"\b(?:q([1-4])|([1-4])q)\b")


@dataclass(frozen=True, slots=True)
class PeriodReading:
    """What a question's period words say. kind PERIOD_NAMED carries the sheet parser's period; so far and rest of carry none: so far is whatever the table holds, rest of is a status column."""
    kind: str
    period: object = None


def read_period(words: str, year: int | None = None) -> PeriodReading | None:
    """One period's words. "so far" adds no condition; "rest of May" is a status column, not a date; "calendar 2002" is that year; "Q4" takes the year the table's context gives."""
    low = fold(words)
    if _SO_FAR.search(low):
        return PeriodReading(PERIOD_SO_FAR)
    if _REST_OF.search(low):
        return PeriodReading(PERIOD_REST_OF)
    calendar = _CALENDAR.search(low)
    if calendar:
        digits = calendar.group(1)
        return PeriodReading(PERIOD_NAMED, parse_period(digits if len(digits) == 4 else f"20{digits}"))
    quarter = _YEARLESS_QUARTER.fullmatch(low)
    if quarter and year is not None:
        return PeriodReading(PERIOD_NAMED, parse_period(f"Q{quarter.group(1) or quarter.group(2)} {year}"))
    period = parse_period(words)
    return None if period is None else PeriodReading(PERIOD_NAMED, period)


# ------------------------------------------------------------------------------------------------------------ units
def stated_number(text: str, percent_column: bool = False) -> float | None:
    """A number a question states, as the cells store it: 10% is 0.1, $1,000 is 1000, 30M is 30000000. A bare 10 against a percent column reads 0.1, since percent cells hold fractions."""
    reading = parse_number(text.strip())
    if reading is None:
        return None
    if percent_column and not reading.percent and abs(reading.value) > 1:
        return reading.value / 100.0
    return reading.value


def share_forms(value: float) -> tuple[float, ...]:
    """A share may be stated either way: the fraction, and the percent it is."""
    return (value, value * 100.0)

