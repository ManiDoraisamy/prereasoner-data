"""The one parser for numbers and the one parser for dates and periods, plus what a number format says.

parse_number("(1,234.50)") reads parentheses, currency marks, percent, thousands and a magnitude letter; a percent is a
fraction, as a percent cell stores it. parse_period("Jul-Sep 02") reads days, months, quarters, seasons, years and ranges
written as text. The two-digit year window is Excel's: 00-29 are 20xx and 30-99 are 19xx. A day is month first (US), unless
the first figure cannot be a month. Everything here is pure and deterministic.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from functools import lru_cache

from engine.sheet.names import (F_CURRENCY, F_DATE, F_DURATION, F_GENERAL, F_NUMBER, F_PERCENT, F_TEXT, F_TIME, P_DAY, P_HALF, P_HOUR, P_MONTH, P_QUARTER, P_RANGE, P_RELATIVE,
                                P_SEASON, P_WEEK, P_WEEKDAY, P_YEAR)

_NUMBER = re.compile(r"^(?P<open>\(?)\s*(?P<lead>[-+]?)\s*(?P<money>[$€£¥]?)\s*(?P<sign>[-+]?)\s*(?P<body>\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE](?P<exp>[-+]?\d+))?\s*"
                     r"(?P<mag>[kKmM]{0,2})\s*(?P<pct>%?)\s*(?P<trail>-?)\s*(?P<close>\)?)$")
_THOUSANDS_OK = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d*)?$")
_ERROR = re.compile(r"^#(?:NULL!|DIV/0!|VALUE!|REF!|NAME\?|NUM!|N/A|ERROR!|GETTING_DATA)$", re.I)
_MARKERS = frozenset({"-", "--", "---", "n/a", "na", "#n/a", "tbd", "tba", "none", "nil", "?", "??", "n.a.", "nm", "*", "**", ".", "nan"})
_WITH_WORD = re.compile(r"^(?P<number>\(?[-+$]?\s*\d[\d,]*(?:\.\d+)?\)?%?)\s+\(?[A-Za-z][\w /.-]*\)?$")


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
    magnitude = m.group("mag").lower()
    if magnitude not in ("", "k", "m", "mm"):
        return None
    value = float(body.replace(",", "") + (f"e{m.group('exp')}" if m.group("exp") else ""))
    value *= {"": 1.0, "k": 1e3, "m": 1e6, "mm": 1e6}[magnitude]
    percent = bool(m.group("pct"))
    if percent:
        value /= 100.0
    negative = bool(m.group("open")) ^ ("-" in (m.group("lead"), m.group("sign"), m.group("trail")))
    if not math.isfinite(value):
        return None
    return NumberReading(-value if negative else value, percent, m.group("money"))


def is_error(text: str) -> bool:
    return bool(_ERROR.match(text.strip()))


def is_marker(text: str) -> bool:
    """A filled cell that says there is no value."""
    return text.strip().lower() in _MARKERS


def number_with_word(text: str) -> NumberReading | None:
    """A number with a word after it, as 300,000 (MAQ): the number, or None. The cell stays a stray of its column."""
    m = _WITH_WORD.match(text.strip())
    return parse_number(m.group("number")) if m else None


# ------------------------------------------------------------------------------------------- periods
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_TIME = r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?)?"
_ORD = r"(?:st|nd|rd|th)?"


def _rx(pattern: str) -> re.Pattern:
    return re.compile(rf"^{pattern}$", re.I)


_ISO = _rx(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ]\d{1,2}:\d{2}.*)?")
_MDY = _rx(rf"(\d{{1,2}})([/.\-])(\d{{1,2}})\2(\d{{2}}|\d{{4}}){_TIME}")
_MD = _rx(r"(\d{1,2})/(\d{1,2})")
_D_MON_Y = _rx(rf"(\d{{1,2}}){_ORD}[\s\-]+({_MONTH})[\s,\-]*(\d{{2}}|\d{{4}}){_TIME}")
_MON_D_Y = _rx(rf"({_MONTH})[\s\-]*(\d{{1,2}}){_ORD}[\s,\-]+(\d{{2}}|\d{{4}}){_TIME}")
_D_MON = _rx(rf"(\d{{1,2}}){_ORD}[\s\-]+({_MONTH})")
_MON_D = _rx(rf"({_MONTH})\s+([1-9]|[12]\d|3[01]){_ORD}")
_MON_Y = _rx(rf"({_MONTH})[\s'’,.\-]*(\d{{2}}|\d{{4}})")
_MON = _rx(_MONTH)
_MON_RANGE = _rx(rf"({_MONTH})\s*(?:-|–|/|to|thru|through)\s*({_MONTH})[\s'’,\-]*(\d{{2}}|\d{{4}})?")
_MON_Y_RANGE = _rx(rf"({_MONTH})[\s'’\-]*(\d{{2}}|\d{{4}})\s*(?:-|–|/|to|thru|through)\s*({_MONTH})[\s'’\-]*(\d{{2}}|\d{{4}})")
_RANGE_SPLIT = re.compile(r"\s+(?:-|–|to|thru|through)\s+|(?<=\d)-(?=\d{1,2}/)", re.I)
_Q_FIRST = _rx(r"q(?:tr)?\.?\s*([1-4])(?:[\s'’\-]*(\d{2}|\d{4}))?")
_Q_AFTER = _rx(r"([1-4])q(?:tr)?[\s'’\-]*(\d{2}|\d{4})?")
_Q_WORD = _rx(r"([1-4])(?:st|nd|rd|th)\s+(?:quarter|qtr)\.?[\s'’\-]*(\d{2}|\d{4})?")
_Q_WORD_FIRST = _rx(r"(?:quarter|qtr)\.?\s+([1-4])(?:[\s'’\-]+(\d{2}|\d{4}))?")
_HALF = _rx(r"(?:([12])h[\s'’\-]*(\d{2}|\d{4})|h([12])[\s'’\-]+(\d{2}|\d{4}))")
_YEAR = _rx(r"((?:19|20)\d{2})")
_CAL = _rx(r"(?:cal|cy|fy)[\s'’\-]*(\d{2}|\d{4})")
_SEASON_YEAR = _rx(r"(sum(?:mer)?|win(?:ter)?|spr(?:ing)?|fall|aut(?:umn)?)[\s'’\-]*(\d{2}|\d{4})")
_SEASON = _rx(r"(summer|winter|spring|fall|autumn)")
_RELATIVE = _rx(r"(ytd|mtd|qtd|bom|prompt|year[\s\-]to[\s\-]date|month[\s\-]to[\s\-]date)")
_WEEKDAY = _rx(r"(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day)?|tuesday|wednesday|thursday|saturday")
_WEEK = _rx(r"(?:week|wk)\.?\s*#?\s*(\d{1,2})")
_HOUR = _rx(r"(?:he|hour|hr)\.?\s*(\d{1,2})")
_SEASON_WORDS = {"sum": "summer", "win": "winter", "spr": "spring", "aut": "fall", "fall": "fall"}


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


def _month_number(word: str) -> int:
    return next(i for i, name in enumerate(_MONTHS, 1) if name.startswith(word.strip(".").lower()[:3]))


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
    """A cell's text as a day or a period, or None. Only the whole cell counts: a sentence holding a date is not a date."""
    s = re.sub(r"\s+", " ", text.strip())
    if not s or len(s) > 48:
        return None
    if m := _ISO.match(s):
        return _day(int(m[1]), int(m[2]), int(m[3]))
    if m := _MDY.match(s):
        return _month_day_year(int(m[1]), int(m[3]), full_year(m[4]))
    if m := _D_MON_Y.match(s):
        return _day(full_year(m[3]), _month_number(m[2]), int(m[1]))
    if m := _MON_D_Y.match(s):
        return _day(full_year(m[3]), _month_number(m[1]), int(m[2]))
    if m := _MD.match(s):
        first, second = int(m[1]), int(m[2])
        month, day = (second, first) if first > 12 else (first, second)
        return Period(P_DAY, month=month, day=day) if 1 <= month <= 12 and 1 <= day <= 31 else None
    if m := _D_MON.match(s):
        return Period(P_DAY, month=_month_number(m[2]), day=int(m[1])) if 1 <= int(m[1]) <= 31 else None
    if m := _MON_D.match(s):
        # "Mar 22" is a day; a leading zero or a hyphen ("Jan 02", "Jan-02") reads as a month and year below.
        return Period(P_DAY, month=_month_number(m[1]), day=int(m[2]))
    if m := _MON_Y.match(s):
        month = _month_number(m[1])
        return _span(P_MONTH, full_year(m[2]), month, month, month=month)
    if _MON.match(s):
        return Period(P_MONTH, month=_month_number(s))
    if m := _MON_Y_RANGE.match(s):
        first, last = _month_number(m[1]), _month_number(m[3])
        left, right = _span(P_MONTH, full_year(m[2]), first, first), _span(P_MONTH, full_year(m[4]), last, last)
        return _whole_range(left, right) if left and right else None
    if m := _MON_RANGE.match(s):
        first, last = _month_number(m[1]), _month_number(m[2])
        if not m[3]:
            return Period(P_RANGE, month=first, word=f"{_MONTHS[first - 1]}-{_MONTHS[last - 1]}")
        year = full_year(m[3])
        left, right = _span(P_MONTH, year - (first > last), first, first), _span(P_MONTH, year, last, last)
        return _whole_range(left, right) if left and right else None
    for pattern in (_Q_FIRST, _Q_AFTER, _Q_WORD, _Q_WORD_FIRST):
        if m := pattern.match(s):
            quarter = int(m[1])
            if not m[2]:
                return Period(P_QUARTER, quarter=quarter)
            return _span(P_QUARTER, full_year(m[2]), 3 * quarter - 2, 3 * quarter, quarter=quarter)
    if m := _HALF.match(s):
        half, year = int(m[1] or m[3]), full_year(m[2] or m[4])
        return _span(P_HALF, year, 6 * half - 5, 6 * half)
    if m := _YEAR.match(s):
        return _span(P_YEAR, int(m[1]), 1, 12)
    if m := _CAL.match(s):
        return _span(P_YEAR, full_year(m[1]), 1, 12)
    if m := _SEASON_YEAR.match(s):
        word = _SEASON_WORDS[m[1][:3].lower() if not m[1].lower().startswith("fall") else "fall"]
        year = full_year(m[2])
        return Period(P_SEASON, f"{year:04d}-01-01", f"{year:04d}-12-31", word=word)
    if m := _SEASON.match(s):
        return Period(P_SEASON, word=_SEASON_WORDS[m[1][:3].lower() if m[1].lower() != "fall" else "fall"])
    if m := _RELATIVE.match(s):
        return Period(P_RELATIVE, word=m[1].lower())
    if _WEEKDAY.match(s):
        return Period(P_WEEKDAY, word=s[:3].lower())
    if m := _WEEK.match(s):
        return Period(P_WEEK, word=f"week {int(m[1])}")
    if m := _HOUR.match(s):
        return Period(P_HOUR, word=f"hour {int(m[1])}")
    if len(parts := _RANGE_SPLIT.split(s)) == 2:
        left, right = parse_period(parts[0]), parse_period(parts[1])
        if left and right and left.kind in (P_DAY, P_MONTH) and right.kind in (P_DAY, P_MONTH):
            return _whole_range(left, right)
    return None


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
