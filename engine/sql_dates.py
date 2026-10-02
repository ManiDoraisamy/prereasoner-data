"""Calendar phrases of a question: the dates and months it filters a date column by.

"How many transfers were signed in August?", "leads submitted after August 10, 2026" and "contracts
signed before July 10, 2026" were planned without their date (2026-10-02): the search read no month
name, and read "10" and "2026" as a number and a year. Each phrase here becomes typed comparisons on a
date column (engine/sql_search.py), and the coverage gate (KnowledgeQuery._uncovered) reads from the
same comparisons which month words a query realized.

A phrase is a month with an optional day and year ("August", "August 2026", "August 10, 2026",
"10 August 2026", "2026-08-10"), optionally after a cue: before, after, since, from, until, till,
through, on, in, during. A month without a year compares the month of each date (DatePart); a dated
phrase compares the date itself, so a timestamp later on the named day is still that day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Sequence

from engine.sql_ast import ColumnRef, Comparison, DatePart, Literal, SQLType, month_of_date_sql

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
# Also ordinary words ("may have", "march", "mar"): a month only beside a day or a year, or after a cue.
_AMBIGUOUS = frozenset({"may", "march", "mar"})
# The comparison each cue makes with the phrase's first day ("start") or the day after its last ("end").
_CUES = {
    "before": (("<", "start"),),
    "after": ((">=", "end"),),
    "since": ((">=", "start"),),
    "from": ((">=", "start"),),
    "until": (("<", "end"),),
    "till": (("<", "end"),),
    "through": (("<", "end"),),
    "on": ((">=", "start"), ("<", "end")),
    "in": ((">=", "start"), ("<", "end")),
    "during": ((">=", "start"), ("<", "end")),
}
_WITHIN = (">=", "start"), ("<", "end")
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")


@dataclass(frozen=True)
class DatePhrase:
    start: int                 # the first token, the cue's when there is one
    end: int                   # one past the last token
    cue: str | None
    month: int
    year: int | None = None
    day: int | None = None

    def comparisons(self, column: ColumnRef) -> tuple[Comparison, ...]:
        """The typed comparisons on ``column``, a date column, that keep the phrase's dates. A day
        without a year ("after August 10") names no date to compare with: none."""
        if self.year is None and self.day is not None:
            return ()
        if self.year is None:
            return (Comparison(DatePart("month", column), "=", Literal(self.month, SQLType.INTEGER)),)
        first = date(self.year, self.month, self.day or 1)
        if self.day is not None:
            after = first + timedelta(days=1)
        else:
            after = date(self.year + self.month // 12, self.month % 12 + 1, 1)
        bounds = {"start": first.isoformat(), "end": after.isoformat()}
        return tuple(Comparison(column, operator, Literal(bounds[side], SQLType.DATE))
                     for operator, side in _CUES.get(self.cue, _WITHIN))


def _number(token: str, low: int, high: int) -> int | None:
    if re.fullmatch(r"\d{1,4}", token) and low <= int(token) <= high:
        return int(token)
    return None


def date_phrases(question: str, tokens: Sequence[str]) -> tuple[DatePhrase, ...]:
    """The calendar phrases of ``question``, as spans of its lowercase ``tokens`` (the search's own).

    A month without a year is read only as a whole month ("in August"): "after August" names no year
    to compare with, and stays unread. A day without a year ("after August 10") is read and compares
    nothing, so the coverage gate asks about it.
    """
    tokens = tuple(tokens)
    phrases = []
    claimed: set[int] = set()
    for match in _ISO.finditer(question):
        year, month, day = (int(part) for part in match.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        parts = tuple(str(int(part)) if len(part) < 4 else part for part in match.groups())
        for index in range(len(tokens) - 2):
            if (index not in claimed and tuple(str(int(t)) if t.isdigit() and len(t) < 4 else t
                                               for t in tokens[index:index + 3]) == parts):
                phrases.append((index, index + 3, month, year, day))
                claimed.update(range(index, index + 3))
                break
    for index, token in enumerate(tokens):
        month = MONTHS.get(token)
        if month is None or index in claimed:
            continue
        start, end, year, day = index, index + 1, None, None
        following = tokens[index + 1:index + 3]
        if following and (day := _number(following[0], 1, 31)) is not None and len(following[0]) <= 2:
            year = _number(following[1], 1000, 9999) if len(following) > 1 else None
            # "August 10" names no year: the phrase is read, so "10" is no number, but filters nothing.
            end = index + 3 if year is not None else index + 2
        elif following and (year := _number(following[0], 1000, 9999)) is not None:
            day, end = None, index + 2
            if index > 0 and (before := _number(tokens[index - 1], 1, 31)) is not None:
                day, start = before, index - 1          # "10 August 2026"
        else:
            day = None
        cue = tokens[start - 1] if start > 0 and tokens[start - 1] in _CUES else None
        if (year is None and day is None
                and (cue not in (None, "in", "during") or (cue is None and token in _AMBIGUOUS))):
            continue
        if any(position in claimed for position in range(start, end)):
            continue
        claimed.update(range(start, end))
        phrases.append((start, end, month, year, day))
    out = []
    for start, end, month, year, day in sorted(phrases):
        if year is not None and day is not None:
            try:
                date(year, month, day)
            except ValueError:
                continue
        cue = tokens[start - 1] if start > 0 and tokens[start - 1] in _CUES else None
        out.append(DatePhrase(start - 1 if cue else start, end, cue, month, year, day))
    return tuple(out)


def realized_month_words(question: str, sql: str) -> frozenset[str]:
    """The month words of ``question`` whose comparisons ``sql`` carries, for the coverage gate."""
    from engine.sql_expansion import tokens as question_tokens

    tokens = question_tokens(question)
    realized = set()
    for phrase in date_phrases(question, tokens):
        comparisons = phrase.comparisons(ColumnRef("t", "c", SQLType.DATE))
        found = bool(comparisons)
        for comparison in comparisons:
            if isinstance(comparison.left, DatePart):
                pattern = re.escape(month_of_date_sql("§")).replace("§", ".+?")
                found = found and re.search(pattern + r"(?:\s*=\s*|,\s*)" + str(phrase.month) + r"\b",
                                            sql or "", re.I) is not None
            else:
                found = found and f"'{comparison.right.value}'" in (sql or "")
        if found:
            realized.update(token for token in tokens[phrase.start:phrase.end] if token in MONTHS)
    return frozenset(realized)
