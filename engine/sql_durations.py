"""Durations a question compares: how long each row lasted, against a number of days, weeks, months or years.

"how many users wanted neartail for greater than 6 months" asked how long each subscription lasted, and the search
read "6 months" as a billing interval of 'month' and a quantity above 6 (2026-10-05). A duration phrase is a
comparison word, a number and a unit: "more than 6 months", "over a year", "at least 2 weeks", "less than 30
days", "no longer than a year", "6 months or more", "6+ months". It compares a DateSpan (engine/sql_ast.py): the
time from a row's start date to its end date, or to the date the question is asked while the row has none. The
phrase claims its words (engine/sql_search.py), so its number is no threshold on a numeric column and its unit no
value of a text column, and a query that does not compare the span the phrase states is not the question's
(engine/query_contract.py).

A span runs from a date column whose name says it starts to the date column of the same table whose name says it
ends and carries the same other words: "Start Date" to "Ended At", "Trial Start" to "Trial End", "Current Period
Start" to "Current Period End". A table with a start column and no end column runs it to the question's date
("customers with us for more than 2 years"), and so does a phrase that ends in "ago" ("signed up more than a year
ago"). Only columns whose every value is a calendar date take part, so SQLite, PostgreSQL and the Python program
read every one.

A window ("in the last 6 months", "within the past year") is no duration: no comparison word comes before it. A
bare length ("a 6-month subscription") is none either: it may name a plan.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
import re
from typing import Sequence

from engine.sql_ast import DAYS_PER_UNIT, Comparison, DateSpan, Literal, SQLType

_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
# The comparison words before the number, longest first, and the comparison each makes.
_BEFORE = (
    (("no", "more", "than"), "<="), (("no", "longer", "than"), "<="), (("no", "less", "than"), ">="),
    (("more", "than"), ">"), (("greater", "than"), ">"), (("longer", "than"), ">"), (("at", "least"), ">="),
    (("less", "than"), "<"), (("fewer", "than"), "<"), (("shorter", "than"), "<"), (("at", "most"), "<="),
    (("over",), ">"), (("above",), ">"), (("exceeding",), ">"), (("under",), "<"), (("below",), "<"),
)
# The words after the unit that make the length a bound: "6 months or more".
_AFTER = ((("or", "more"), ">="), (("or", "longer"), ">="), (("or", "less"), "<="), (("or", "fewer"), "<="),
          (("or", "shorter"), "<="))
_PLUS = re.compile(r"\b(\d+)\s*\+\s*(?:day|week|month|year)s?\b", re.I)

# The words of a column name that say a row's period starts or ends there, and the words any date column's name
# may carry besides ("Start Date (UTC)" starts with no other qualifier).
_START_WORDS = frozenset({
    "start", "started", "starts", "begin", "began", "beginning", "created", "creation", "open", "opened", "signed",
    "join", "joined", "hire", "hired", "registered", "registration", "enrolled", "subscribed", "since",
    "onboarded", "activated", "activation", "effective",
})
_END_WORDS = frozenset({
    "end", "ended", "ends", "ending", "close", "closed", "closing", "cancel", "canceled", "cancelled",
    "cancellation", "terminated", "termination", "finish", "finished", "expiry", "expired", "expiration",
    "expires", "resolved", "completed", "completion", "churned", "left", "until", "stop", "stopped",
    "deactivated",
})
_PLAIN_WORDS = frozenset({"date", "time", "at", "on", "utc", "timestamp", "datetime", "day", "dt", "of", "the"})
# The words that name a period's edge outright, rather than the event that sets one ("Canceled At").
_EDGE_WORDS = frozenset({"start", "started", "starts", "begin", "began", "beginning", "end", "ended", "ends",
                         "ending"})


@dataclass(frozen=True)
class DurationPhrase:
    """A comparison of how long rows lasted: the question's tokens [start, end), the comparison ``operator``,
    the ``amount`` of ``unit``s, and whether the length runs back from the question's date ("ago")."""
    start: int
    end: int
    operator: str
    amount: int
    unit: str
    ago: bool = False


def question_date() -> str:
    """The date a question is asked, in UTC: the span of a row that has not ended runs to it."""
    return datetime.now(timezone.utc).date().isoformat()


def _amount(tokens: Sequence[str], index: int) -> tuple[int | None, int]:
    """The whole number at ``index`` (digits, "a", "one" ... "twelve") and the index after it."""
    if index >= len(tokens):
        return None, index
    token = tokens[index]
    if token.isdigit():
        return int(token), index + 1
    return _NUMBERS.get(token), index + 1


def duration_phrases(question: str, tokens: Sequence[str]) -> tuple[DurationPhrase, ...]:
    """The duration comparisons of ``question`` over its tokens (engine.sql_expansion.tokens, whose units are
    singular), in question order and without overlap."""
    tokens = tuple(tokens)
    plus = {match.group(1) for match in _PLUS.finditer(question)}
    phrases: list[DurationPhrase] = []
    index = 0
    while index < len(tokens):
        found = None
        for words, operator in _BEFORE:
            if tokens[index:index + len(words)] == words:
                amount, unit_at = _amount(tokens, index + len(words))
                if amount is not None and unit_at < len(tokens) and tokens[unit_at] in DAYS_PER_UNIT:
                    end = unit_at + 1
                    ago = end < len(tokens) and tokens[end] == "ago"
                    found = DurationPhrase(index, end + ago, operator, amount, tokens[unit_at], ago)
                break
        if found is None:
            amount, unit_at = _amount(tokens, index)
            if amount is not None and unit_at < len(tokens) and tokens[unit_at] in DAYS_PER_UNIT:
                for words, operator in _AFTER:
                    if tokens[unit_at + 1:unit_at + 1 + len(words)] == words:
                        found = DurationPhrase(index, unit_at + 1 + len(words), operator, amount, tokens[unit_at])
                        break
                else:
                    if tokens[index] in plus:
                        found = DurationPhrase(index, unit_at + 1, ">=", amount, tokens[unit_at])
        if found is None:
            index += 1
            continue
        phrases.append(found)
        index = found.end
    return tuple(phrases)


def _words(name: str) -> frozenset[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return frozenset(word.lower() for word in re.findall(r"[A-Za-z]+", spaced))


def _calendar_dates(values) -> bool:
    """Whether every value is a calendar date: its ISO text's first ten characters name a real day."""
    seen = False
    for value in values:
        if value is None:
            continue
        text = (value.isoformat() if isinstance(value, date) else str(value)).strip()[:10]
        if not text:
            continue
        try:
            date.fromisoformat(text)
        except ValueError:
            return False
        seen = True
    return seen


def _pair_score(start: str, end: str | None) -> float:
    """How surely a span runs from column ``start`` to column ``end`` (None: to the question's date). The same
    other words on both ends make a period ("Trial Start" to "Trial End"), a period with no other word is the
    row's own ("Start Date" to "Ended At"), and a word that names the edge ("start", "ended") is surer than one
    that names an event ("created", "canceled")."""
    start_words = _words(start)
    score = 1.0 if start_words & _EDGE_WORDS else 0.5
    start_rest = start_words - _START_WORDS - _PLAIN_WORDS
    if end is None:
        return score + (1.0 if not start_rest else 0.0)
    end_words = _words(end)
    score += 1.0 if end_words & _EDGE_WORDS else 0.5
    end_rest = end_words - _END_WORDS - _PLAIN_WORDS
    if start_rest == end_rest:
        score += 3.0 if not start_rest else 2.0
    return score


def span_pairs(columns, *, ago: bool = False):
    """The (start, end) date-column pairs a span may run between, best first; ``end`` is None for a span that
    runs to the question's date. ``columns`` are the schema's columns (``ref`` and ``values``)."""
    dated = [column.ref for column in columns
             if column.ref.type == SQLType.DATE and _calendar_dates(column.values)]
    tables = list(dict.fromkeys(ref.table for ref in dated))
    pairs = []
    for table_index, table in enumerate(tables):
        refs = [ref for ref in dated if ref.table == table]
        starts = [ref for ref in refs if _words(ref.name) & _START_WORDS]
        ends = [ref for ref in refs if _words(ref.name) & _END_WORDS and ref not in starts]
        if ago:
            starts = starts or refs
        for start_index, start in enumerate(starts):
            if ago or not ends:
                pairs.append((-_pair_score(start.name, None), table_index, start_index, 0, start, None))
                continue
            for end_index, end in enumerate(ends):
                pairs.append((-_pair_score(start.name, end.name), table_index, start_index, end_index, start, end))
    pairs.sort(key=lambda item: item[:4])
    return tuple((start, end) for *_, start, end in pairs)


def span_comparisons(columns, phrase: DurationPhrase, until: str, limit: int = 4) -> tuple[Comparison, ...]:
    """The span comparisons ``phrase`` may state, best pair first."""
    return tuple(Comparison(DateSpan(start, end, phrase.unit, until), phrase.operator,
                            Literal(phrase.amount, SQLType.INTEGER))
                 for start, end in span_pairs(columns, ago=phrase.ago)[:limit])


def _span_tests(node):
    if isinstance(node, Comparison) and isinstance(node.left, DateSpan) and isinstance(node.right, Literal):
        yield node.operator, node.right.value, node.left.unit
    if is_dataclass(node):
        for field in fields(node):
            yield from _span_tests(getattr(node, field.name))
    elif isinstance(node, (tuple, list)):
        for item in node:
            yield from _span_tests(item)


def realizes_durations(query, phrases: Sequence[DurationPhrase]) -> bool:
    """Whether ``query`` compares a span the way each phrase states: its comparison, amount and unit."""
    stated = set(_span_tests(query))
    return all((phrase.operator, phrase.amount, phrase.unit) in stated for phrase in phrases)


def realized_duration_words(question: str, sql: str) -> frozenset[str]:
    """The words of ``question``'s duration phrases that ``sql`` compares a span by, lower-cased as they are
    written: a phrase is realized where the SQL divides a span by its unit's length in days and compares the
    quotient as the phrase states, as engine.sql_ast.date_span_sql renders it."""
    from engine.sql_expansion import tokens
    from engine.sql_schema import canon

    question_tokens = tokens(question)
    spoken: set[str] = set()
    for phrase in duration_phrases(question, question_tokens):
        if f"/ {DAYS_PER_UNIT[phrase.unit]!r}) {phrase.operator} {phrase.amount}" in (sql or ""):
            spoken.update(question_tokens[phrase.start:phrase.end])
    return frozenset(word for word in re.findall(r"[a-z0-9]+", question.lower()) if canon(word) in spoken)
