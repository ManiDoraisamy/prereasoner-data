"""Dates a sheet writes as text, typed as days once, when the workbook is loaded.

The sheet compiler types a cell as a day only when it is written in digits in a few forms (engine/sheet/values.py
parse_period); "March 23 2021", "25-Sep-2024", "Oct22", "1999/07/01" and "01/01/1947 12:00:00 AM" stay text, so no
date condition, range or order can be built on their column. A month's name is the model's to read, never a list here.
Closed questions to the Decisions API (engine/llm.py decide), at most two rounds per column:

  1. per text column not typed as days whose examples hold a digit: does it hold calendar dates written as text?
  2. per distinct word of such a column: which month does it name (01 to 12, or none)? Each cell is then parsed by rule:
     its one month word, its first four-digit group as the year, its first one- or two-digit group as the day.

A cell written in digits alone (three groups, one a four-digit year, before any time of day) is parsed by rule; where its
column never shows whether the month or the day comes first, the API is asked that once for the column. A column with
more distinct cells than VALUES_MOST is left as it is.

``typed_dates`` returns cell text -> the ISO day or month it names; engine/sheet compile_workbook(workbook, typed) then
reads every cell of that text as that period, keeping its own text (engine/program/answer.py prepare).
"""
from __future__ import annotations

import datetime as dt
import re

from engine.llm import decide
from engine.program.catalog import Catalog, CatalogTable

COLUMN_QUESTION = ("Does this column hold calendar dates or date-times written as text (a day, or a month with its year)? other for "
                   "anything else: names, codes, amounts, plain years, durations.")
COLUMN_OPTIONS = {"date": "calendar dates or date-times written as text", "other": "anything else"}
COLUMNS_MOST = 60          # text columns asked about per workbook
VALUES_MOST = 2000         # distinct cells of one column; more and the column is left as text
EXAMPLES = 5               # a column's examples in the column question and the month question
ORDER_EXAMPLES = 8         # a column's examples in the order question and the short-year question
EXAMPLE_CHARS = 40
HEADER_CHARS = 60

TIME_OF_DAY = re.compile(r"\d{1,2}:\d{2}(:\d{2})?")
DIGITS = re.compile(r"\d+")
WORD = re.compile(r"[^\W\d_]+")      # a run of letters of any script
ORDER_QUESTION = "These dates are written in digits with the year last. Which comes first in them: the month or the day of the month?"
ORDER_OPTIONS = {"month": "the month comes first", "day": "the day of the month comes first"}

WORDS_MOST = 80            # distinct words of one column asked which month they name
MONTH_QUESTION = "Which month of the year does this word name, in any language, in full or shortened? none if it names no month."
SHORT_YEAR_QUESTION = ("In these dates one two-digit number stands beside the month. What is it? y20 = a year of this century (20xx); "
                       "y19 = a year of the last century (19xx); day = the day of the month.")
SHORT_YEAR_OPTIONS = {"y20": "a year of 20xx", "y19": "a year of 19xx", "day": "the day of the month"}


def _examples(table: CatalogTable, position: int, most: int = EXAMPLES) -> list[str]:
    """A column's first distinct filled cells, clipped."""
    out: list[str] = []
    for text in table.texts[position]:
        if text and text not in out:
            out.append(str(text)[:EXAMPLE_CHARS])
        if len(out) >= most:
            break
    return out


def date_columns(catalog: Catalog) -> list[tuple[CatalogTable, int]]:
    """(table, position) of each text column the API reads as dates written as text: one call, one question per column
    not typed as days whose examples mix digits with letters or date marks."""
    columns: dict[str, tuple[CatalogTable, int, dict]] = {}
    questions: dict[str, dict] = {}
    for table in catalog.tables:
        for position, column in enumerate(table.columns):
            if any(table.days[position]) or len(columns) >= COLUMNS_MOST:
                continue
            examples = _examples(table, position)
            if len(examples) < 2 or not any(any(ch.isdigit() for ch in e) and any(ch.isalpha() or ch in "/-:" for ch in e) for e in examples):
                continue
            key = f"c{len(columns):03d}"
            columns[key] = (table, position, {"header": column.name[:HEADER_CHARS], "examples": examples})
            questions[key] = {"instructions": f"Column {key}. {COLUMN_QUESTION}", "criteria": dict(COLUMN_OPTIONS)}
    if not questions:
        return []
    fields = decide({"columns": {key: held[2] for key, held in columns.items()}}, questions).fields
    return [(columns[key][0], columns[key][1]) for key, p in fields.items() if p["date"] > p["other"]]


def _digit_parts(text: str) -> tuple[int, int, int, bool] | None:
    """(first, second, year, year first) of a date written in digits alone before any time of day: three groups with one
    four-digit year, or None. "01/01/1947 12:00:00 AM" is (1, 1, 1947, False); "1999/07/01" is (7, 1, 1999, True)."""
    date = text[:match.start()] if (match := TIME_OF_DAY.search(text)) else text
    if any(ch.isalpha() for ch in date):
        return None
    groups = DIGITS.findall(date)
    if len(groups) != 3 or sum(len(group) == 4 for group in groups) != 1 or len(groups[1]) == 4:
        return None
    first = len(groups[0]) == 4
    a, b = (int(groups[1]), int(groups[2])) if first else (int(groups[0]), int(groups[1]))
    return a, b, int(groups[0] if first else groups[2]), first


def digit_dates(texts: list[str], examples: list[str]) -> dict[str, str]:
    """{cell text: ISO day} for the cells of one column written in digits alone, parsed by rule. With the year first the
    month follows it. With the year last, the column's own cells decide the order where one shows it (a first number
    above 12 is a day), and the API is asked once for the column where none does."""
    parts = {text: held for text in texts if (held := _digit_parts(text)) is not None}
    if not parts:
        return {}
    last = [held for held in parts.values() if not held[3]]
    month_first = True
    if last:
        if any(a > 12 for a, _, _, _ in last):
            month_first = False
        elif not any(b > 12 for _, b, _, _ in last):
            order = decide({"column examples": list(examples)[:ORDER_EXAMPLES]},
                           {"order": {"instructions": ORDER_QUESTION, "criteria": dict(ORDER_OPTIONS)}}).fields["order"]
            month_first = order["month"] >= order["day"]
    out = {}
    for text, (a, b, year, first) in parts.items():
        month, day = (a, b) if first or month_first else (b, a)
        try:
            out[text] = dt.date(year, month, day).isoformat()
        except ValueError:
            continue
    return out


def worded_dates(texts: list[str], examples: list[str]) -> dict[str, str]:
    """{cell text: ISO day or month} for the cells of one column that write the month as a word, parsed by rule from one
    reading of the column's words: each distinct word of the column is asked once which month it names (01 to 12 or
    none), so the same word gives the same month in every cell. A cell with one month word reads its first four-digit
    group as the year and its first one- or two-digit group from 1 to 31 as the day (none: the month alone). Where the
    cells hold a month word and a single two-digit number and no four-digit year ("Oct22"), the API is asked once for the
    column whether that number is a year of 20xx, of 19xx, or a day. Asking the API every cell's month and day gave the
    wrong month in about 1 cell of 100 (9 of 1,377 and 8 of 749 against an independent parser, decisions-probe-20261118)."""
    dates = {text: (text[:match.start()] if (match := TIME_OF_DAY.search(text)) else text) for text in texts}
    words: dict[str, str] = {}
    for date in dates.values():
        for word in WORD.findall(date):
            words.setdefault(word.casefold(), word)
    if not words:
        return {}
    listed = list(words.items())[:WORDS_MOST]
    months = {f"o{month:02d}": f"{month:02d}" for month in range(1, 13)}
    # a question is named by letters and quotes its word: named "w05", the fifth word was read as month 05 whatever it
    # said (August as 05 on 249 cells of one sheet)
    keys = ["w" + chr(97 + at // 26) + chr(97 + at % 26) for at in range(len(listed))]
    questions = {key: {"instructions": f'The word "{shown}". {MONTH_QUESTION}', "criteria": {**months, "none": "no month"}}
                 for key, (_, shown) in zip(keys, listed)}
    fields = decide({"column examples": list(examples)[:EXAMPLES]}, questions).fields
    month_of = {}
    for key, (folded, _) in zip(keys, listed):
        p = fields[key]
        best = max(months, key=lambda option: p.get(option, 0.0))
        if p.get(best, 0.0) > p.get("none", 0.0):
            month_of[folded] = months[best]
    read = {}
    for text, date in dates.items():
        named = {month_of[word.casefold()] for word in WORD.findall(date) if word.casefold() in month_of}
        if len(named) != 1:
            continue                                   # no month word, or two (a range): left as text
        groups = DIGITS.findall(date)
        read[text] = (named.pop(), [group for group in groups if len(group) == 4], [group for group in groups if len(group) <= 2])
    century = None
    if any(not four and len(short) == 1 for _, four, short in read.values()):
        number = decide({"column examples": list(examples)[:ORDER_EXAMPLES]},
                        {"number": {"instructions": SHORT_YEAR_QUESTION, "criteria": dict(SHORT_YEAR_OPTIONS)}}).fields["number"]
        century = max(number, key=number.get)
    out = {}
    for text, (month, four, short) in read.items():
        days = [int(group) for group in short if 1 <= int(group) <= 31]
        if four:
            year = four[0]
        elif len(short) == 1 and century in ("y20", "y19"):
            year, days = century[1:] + short[0].zfill(2), []
        else:
            continue
        if not days:
            out[text] = f"{year}-{month}"
            continue
        try:
            out[text] = dt.date(int(year), int(month), days[0]).isoformat()
        except ValueError:
            out[text] = f"{year}-{month}"
    return out


def typed_dates(catalog: Catalog) -> dict[str, str]:
    """Cell text -> the ISO day or month it names, for the columns the API reads as dates written as text. Empty when the
    workbook writes none. Raises LLMUnavailable."""
    typed: dict[str, str] = {}
    for table, position in date_columns(catalog):
        distinct = [text for text in dict.fromkeys(str(text) for text in table.texts[position] if text) if text not in typed]
        if len(distinct) > VALUES_MOST:
            continue
        by_rule = digit_dates(distinct, _examples(table, position, ORDER_EXAMPLES))
        typed.update(by_rule)
        typed.update(worded_dates([text for text in distinct if text not in by_rule], _examples(table, position)))
    return typed


__all__ = ["typed_dates"]
