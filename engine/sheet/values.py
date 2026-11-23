"""The one parser for numbers and the one parser for dates and periods, plus what a number format says.

No word of any language is read here: a number is digits with its signs, currency marks, percent and grouping; a date
or a period is digits and separators (2019-04-30, 4/30/2019, 2019-04, 4/2019, 2019), and a date the file stores as a
serial day reads through its number format. A month or a quarter written in words stays text: its meaning is the
learned readers' to read. The two-digit year window is Excel's: 00-29 are 20xx and 30-99 are 19xx. A day is month first
(US), unless the first figure cannot be a month. Everything here is pure and deterministic.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from functools import lru_cache

from engine.sheet.names import (F_CURRENCY, F_DATE, F_DURATION, F_GENERAL, F_NUMBER, F_PERCENT, F_TEXT, F_TIME, P_DAY, P_MONTH, P_RANGE, P_YEAR)

_NUMBER = re.compile(r"^(?P<open>\(?)\s*(?P<lead>[-+−]?)\s*(?P<money>[$€£¥]?)\s*(?P<sign>[-+−]?)\s*(?P<body>\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE](?P<exp>[-+]?\d+))?\s*"
                     r"(?P<pct>%?)\s*(?P<trail>-?)\s*(?P<close>\)?)$")
_THOUSANDS_OK = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d*)?$")
_ERROR = re.compile(r"^#(?:NULL!|DIV/0!|VALUE!|REF!|NAME\?|NUM!|N/A|ERROR!|GETTING_DATA)$", re.I)     # the file format's own error codes
_MARKERS = frozenset({"-", "--", "---", "?", "??", "*", "**", "."})     # marks, not words: a filled cell saying there is no value
_LETTER = r"[^\W\d_]"     # a letter of any script
MINUS_SIGN = "−"     # the minus sign itself (U+2212), as typeset tables print a negative number
_WITH_WORD = re.compile(rf"^(?P<number>\(?[-+$]?\s*\d[\d,]*(?:\.\d+)?\)?%?)\s+\(?{_LETTER}[\w /.-]*\)?$")


@dataclass(frozen=True, slots=True)
class NumberReading:
    """A number read from text: value holds the sign and the magnitude, and a percent as its fraction."""
    value: float
    percent: bool = False
    currency: str = ""


def parse_number(text: str) -> NumberReading | None:
    """A cell's text as a number, or None. "1,23" and "12,34,5" are not numbers: thousands must group by three."""
    m = _NUMBER.match(text.strip())
    if m is None:
        return None
    body = m.group("body")
    if "," in body and not _THOUSANDS_OK.match(body):
        return None
    if bool(m.group("open")) != bool(m.group("close")):
        return None
    value = float(body.replace(",", "") + (f"e{m.group('exp')}" if m.group("exp") else ""))
    percent = bool(m.group("pct"))
    if percent:
        value /= 100.0
    negative = bool(m.group("open")) ^ any(mark in (m.group("lead"), m.group("sign"), m.group("trail")) for mark in ("-", MINUS_SIGN))
    if not math.isfinite(value):
        return None
    return NumberReading(-value if negative else value, percent, m.group("money"))


def is_error(text: str) -> bool:
    return bool(_ERROR.match(text.strip()))


def is_marker(text: str) -> bool:
    """A filled cell that says there is no value: a mark such as "-" or "?"."""
    return text.strip() in _MARKERS


def number_with_word(text: str) -> NumberReading | None:
    """A number with a word after it, as 300,000 (MAQ): the number, or None. The cell stays a stray of its column."""
    m = _WITH_WORD.match(text.strip())
    return parse_number(m.group("number")) if m else None


_ASIDE = re.compile(r"^(?P<number>[-+−]?\s*[$€£¥]?\d[\d,]*(?:\.\d+)?%?)\s*\((?P<aside>[^()]+)\)$")
_UNIT_ASIDE = re.compile(r"^(?P<before>[^()]*\S)\s*\([^()]*\d[^()]*\)$")     # what stands before a bracket holding a figure
_QUANTITY = re.compile(rf"^(?P<number>\(?[-+$]?\s*\d[\d,]*(?:\.\d+)?\)?)\s+(?P<unit>{_LETTER}[^\W\d_/.-]*[/.-]?(?:[ /.-]?{_LETTER}[^\W\d_/.-]*){{0,2}})$")


def aside_number(text: str) -> NumberReading | None:
    """A number with a second figure in brackets after it, as "41(11)" or "38 (100)": the bracket holds another reading
    of it (another unit) or a part of it, so the cell reads as its first number; None otherwise. Only figures: a bracket
    holding words ("4 (last: 1958)", "300,000 (MAQ)") is no aside of a number."""
    m = _ASIDE.match(" ".join(text.split()))
    if m is None or parse_number(m.group("aside")) is None:
        return None
    return parse_number(m.group("number"))


def quantity_of(text: str) -> tuple[float, str] | None:
    """A number with a unit after it, as "1,370 tons" or "20 knots", the same unit's figure in another unit after it in
    brackets or not ("1.90 m (6 ft 3 in)"): (the number, the unit folded: lower case, a final full stop dropped), or
    None. A column holding one unit throughout reads as numbers of it (compiler._type_column)."""
    text = " ".join(text.split())
    aside = _UNIT_ASIDE.match(text)
    m = _QUANTITY.match(aside.group("before") if aside else text)
    if m is None or len(m.group("unit")) > 20:
        return None
    reading = parse_number(m.group("number"))
    if reading is None:
        return None
    return reading.value, m.group("unit").casefold().rstrip(".")


# ------------------------------------------------------------------------------------------- periods
_TIME = r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"


def _rx(pattern: str) -> re.Pattern:
    return re.compile(rf"^{pattern}$")


_ISO = _rx(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ]\d{1,2}:\d{2}.*)?")
_MDY = _rx(rf"(\d{{1,2}})([/.\-])(\d{{1,2}})\2(\d{{2}}|\d{{4}}){_TIME}")
_MD = _rx(r"(\d{1,2})/(\d{1,2})")
_YEAR = _rx(r"((?:19|20)\d{2})")
_YEAR_MONTH = _rx(r"(?:(\d{4})[-/](\d{1,2})|(\d{1,2})/(\d{4}))")
_RANGE_SPLIT = re.compile(r"\s+[-–]\s+|(?<=\d)-(?=\d{1,2}/)")


@dataclass(frozen=True, slots=True)
class Period:
    """A stretch of time a cell names. start and end are ISO days, or None when the text gives no year; month, quarter and word then say what it does give."""
    kind: str
    start: str | None = None
    end: str | None = None
    month: int | None = None
    day: int | None = None
    quarter: int | None = None
    word: str | None = None


def full_year(text: str) -> int:
    year = int(text)
    if len(text) >= 4:
        return year
    return 2000 + year if year < 30 else 1900 + year


def _last_day(year: int, month: int) -> int:
    return (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)).day


def _span(kind: str, year: int, first_month: int, last_month: int, **named) -> Period | None:
    if not 1 <= year <= 9998:
        return None
    return Period(kind, f"{year:04d}-{first_month:02d}-01", f"{year:04d}-{last_month:02d}-{_last_day(year, last_month):02d}", **named)


def _day(year: int, month: int, day: int) -> Period | None:
    try:
        iso = dt.date(year, month, day).isoformat()
    except ValueError:
        return None
    return Period(P_DAY, iso, iso, month=month, day=day)


def _month_day_year(first: int, second: int, year: int) -> Period | None:
    return _day(year, second, first) if first > 12 else _day(year, first, second)


def _whole_range(left: Period, right: Period) -> Period | None:
    if left.start is None or right.end is None or left.start > right.end:
        return None
    return Period(P_RANGE, left.start, right.end)


@lru_cache(maxsize=65536)
def parse_period(text: str) -> Period | None:
    """A cell's text as a day or a period written in digits, or None. Only the whole cell counts: a sentence holding a date is not a date."""
    s = re.sub(r"\s+", " ", text.strip())
    if not s or len(s) > 48:
        return None
    if m := _ISO.match(s):
        return _day(int(m[1]), int(m[2]), int(m[3]))
    if m := _MDY.match(s):
        return _month_day_year(int(m[1]), int(m[3]), full_year(m[4]))
    if m := _MD.match(s):
        first, second = int(m[1]), int(m[2])
        month, day = (second, first) if first > 12 else (first, second)
        return Period(P_DAY, month=month, day=day) if 1 <= month <= 12 and 1 <= day <= 31 else None
    if m := _YEAR_MONTH.match(s):
        # "2019-04", "2019/4", "4/2019": a month; "2011-12" and "2001-02" are a season or a fiscal year (the month is the
        # next year's last two digits), as before
        year, month = (int(m[1]), int(m[2])) if m[1] else (int(m[4]), int(m[3]))
        if 1900 <= year <= 2100 and 1 <= month <= 12 and not (m[1] and month == (year + 1) % 100):
            return _span(P_MONTH, year, month, month, month=month)
    if m := _YEAR.match(s):
        return _span(P_YEAR, int(m[1]), 1, 12)
    if len(parts := _RANGE_SPLIT.split(s)) == 2:
        left, right = parse_period(parts[0]), parse_period(parts[1])
        if left and right and left.kind in (P_DAY, P_MONTH) and right.kind in (P_DAY, P_MONTH):
            return _whole_range(left, right)
    return None


# ------------------------------------------------------------------------------------------------- figures in a text
_FIGURE = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d])")
_SPAN_MARK = frozenset("-–—/")     # what joins a span written short ("1996–97", "2010/11")
_TIME_MARK = re.compile(r"\d:\d")
FIGURE_SHARE = 0.3                 # a cell is figures, not prose that holds one, when this share of its marks are digits


def figures_of(text: str) -> tuple[float, ...]:
    """The figures a text cell writes, in order, read by digits and marks only: "3–2" is (3, 2), "W 55-7" (55, 7),
    "12.9 (55.2)" (12.9, 55.2), "1st" (1,), "1,370" (1370,). A minus is a sign only where a figure opens the cell or a
    bracket; between two figures it joins them. A span written short ("1996–97", "2010/11") reads its second figure
    whole (1997, 2011), as a later year of the same century. A time ("2:05.19") holds no figures here: its reading is
    a duration's (engine/program/semantics.py duration_seconds)."""
    if _TIME_MARK.search(text):
        return ()
    out: list[float] = []
    previous = None
    for match in _FIGURE.finditer(text):
        body, start = match.group(1), match.start(1)
        value = float(body.replace(",", ""))
        before = text[start - 1] if start else ""
        if before in ("-", MINUS_SIGN) and (start == 1 or text[:start - 1].rstrip() == "" or text[start - 2] == "("):
            value = -value
        if previous is not None and len(out) == 1:
            first, between = previous, text[previous.end(1):start].strip()
            whole = first.group(1)
            if (between in _SPAN_MARK and len(whole) == 4 and whole.isdigit() and body.isdigit() and len(body) <= 2):
                century = int(whole[:2]) * 100
                value = float(century + int(body) if century + int(body) > int(whole) else century + 100 + int(body))
        out.append(value)
        previous = match
    return tuple(out)


_OPENING = re.compile(r"^[\s(]*[-−]?\s*\d")


def cell_figures(text: str) -> tuple[float, ...]:
    """The figures a cell is written in: figures_of where the cell opens with a figure ("1st", "65m", "3–2",
    "12.9 (55.2)") or writes two or more ("W 55-7", "September 26, 1959"); none for a code that only ends in one
    ("p1", "A-123", "Apollo 13"), whose figure names it rather than measures it."""
    figures = figures_of(text)
    return figures if len(figures) >= 2 or (figures and _OPENING.match(text)) else ()


def figure_share(text: str) -> float:
    """The share of a text's marks (spaces aside) that are digits."""
    marks = [mark for mark in text if not mark.isspace()]
    return sum(mark.isdigit() for mark in marks) / len(marks) if marks else 0.0


def year_number(value: float) -> bool:
    """A number that reads as a year when it sits where a label sits."""
    return float(value).is_integer() and 1900 <= value <= 2100


# ------------------------------------------------------------------------------------------- serial days and formats
_QUOTED = re.compile(r'"[^"]*"|\\.')
_BRACKET = re.compile(r"\[[^\]]*\]")
_ELAPSED = re.compile(r"\[(?:h+|m+|s+)\]", re.I)
_DATE_TOKEN = re.compile(r"[dmy]", re.I)
_TIME_TOKEN = re.compile(r"[hs]|am/pm|a/p", re.I)
_DIGIT_TOKEN = re.compile(r"[0#?]")
_FRACTION_OF_SECOND = re.compile(r"(?<=s)\.0+", re.I)


@lru_cache(maxsize=4096)
def format_kind(number_format: str | None) -> str:
    """What a number format says about the numbers under it."""
    if not number_format or number_format.strip().lower() == "general":
        return F_GENERAL
    fmt = number_format.split(";")[0] if ";" in number_format and not number_format.startswith(";") else number_format
    if _ELAPSED.search(fmt):
        return F_DURATION
    money = "$" in fmt or any(mark in fmt for mark in "€£¥") or "[$" in fmt
    body = _BRACKET.sub("", _QUOTED.sub("", fmt))
    if body.strip() == "@":
        return F_TEXT
    if not _DIGIT_TOKEN.search(_FRACTION_OF_SECOND.sub("", body)):
        has_date = bool(_DATE_TOKEN.search(body))
        has_time = bool(_TIME_TOKEN.search(body))
        if has_date and (not has_time or re.search(r"[dy]", body, re.I)):
            return F_DATE
        if has_time or has_date:
            return F_TIME
    if "%" in body:
        return F_PERCENT
    if money:
        return F_CURRENCY
    if re.search(r"[0#]", body):
        return F_NUMBER
    return F_GENERAL


def serial_to_date(serial: float, date1904: bool = False) -> dt.date | None:
    """A serial day as a date, in the workbook's epoch; Excel's phantom 1900-02-29 reads as the day after it."""
    if not math.isfinite(serial) or serial < 0 or serial > 2958465:
        return None
    base = dt.date(1904, 1, 1) if date1904 else dt.date(1899, 12, 30)
    return base + dt.timedelta(days=int(math.floor(serial)))


def period_of_serial(serial: float, number_format: str | None, date1904: bool = False) -> Period | None:
    """A date-formatted number as a day, or a month or year when its format shows no finer part ("mmm-yy" reads a month)."""
    date = serial_to_date(serial, date1904)
    if date is None:
        return None
    body = _BRACKET.sub("", _QUOTED.sub("", number_format or "")).lower()
    if "d" not in body and "m" in body and "y" in body:
        return _span(P_MONTH, date.year, date.month, date.month, month=date.month)
    if "d" not in body and "m" not in body and "y" in body:
        return _span(P_YEAR, date.year, 1, 12)
    return _day(date.year, date.month, date.day)
