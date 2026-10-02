"""Calendar phrases of a question: the dates and months it filters a date column by.

"How many transfers were signed in August?", "leads submitted after August 10, 2026" and "contracts
signed before July 10, 2026" were planned without their date (2026-10-02): the search read no month
name, and read "10" and "2026" as a number and a year. Each phrase here becomes typed comparisons on a
date column (engine/sql_search.py), the served selection prefers a query that realizes them
(TableQuery.select_query), and the coverage gate (KnowledgeQuery._uncovered) reads from the same
comparisons which calendar words a query realized.

A phrase names a day ("August 10, 2026", "August 10th, 2026", "10 August 2026", "the 10th of August
2026", "2026-08-10"), a month ("August", "August 2026") or a quarter ("Q3 2026", "the third quarter of
2026"), optionally after a cue: before, after, since, from, until, till, through, on, in, during. Two of
them make a range: "between A and B", "from A to B", "A through B", "A-B"; a year one end names is the
other's too ("from March to May 2026"). A list of months is the range it covers when they follow one
another ("June, July and August 2026"); a list with a gap ("January and March") is no range and compares
nothing, so the coverage gate asks instead of answering for no month. A month without a year compares
the month of each date (DatePart); a dated phrase compares the date itself, so a timestamp later on the
named day is still that day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Sequence

from engine.sql_ast import (
    BooleanExpr,
    ColumnRef,
    Comparison,
    DatePart,
    Literal,
    SelectQuery,
    SQLType,
    month_of_date_sql,
)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
QUARTERS = {"q1": 1, "q2": 2, "q3": 3, "q4": 4}
_ORDINAL_QUARTERS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
# Also ordinary words ("may have", "march", "mar"): a month only beside a day or a year, after a cue, or
# in a range or list with another month.
_AMBIGUOUS = frozenset({"may", "march", "mar"})
# The comparison each cue makes with the phrase's first day ("start") or the day after its last ("end").
# "from" alone is the period itself ("orders from August 2026"); "from A to B" is a range and "from A
# onwards" reads as "since".
_CUES = {
    "before": (("<", "start"),),
    "after": ((">=", "end"),),
    "since": ((">=", "start"),),
    "from": ((">=", "start"), ("<", "end")),
    "until": (("<", "end"),),
    "till": (("<", "end"),),
    "through": (("<", "end"),),
    "on": ((">=", "start"), ("<", "end")),
    "in": ((">=", "start"), ("<", "end")),
    "during": ((">=", "start"), ("<", "end")),
    "between": ((">=", "start"), ("<", "end")),
}
_WITHIN = (">=", "start"), ("<", "end")
# A month without a year compares the month of each date only for these cues: "before August" names no
# year to compare with.
_YEARLESS_CUES = frozenset({None, "in", "during", "from", "between"})
# What joins the two ends of a range ("and" only after "between"), and what joins a list.
_RANGE_JOINS = frozenset({"to", "through", "thru", "till", "until"})
_LIST_JOINS = ((), ("and",), ("or",))
_ONWARD = frozenset({"onward", "onwards"})
_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DAY = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?")
_DASHED = re.compile(r"\b([a-z0-9]+)\s*[-–—]\s*([a-z0-9]+)\b")


@dataclass(frozen=True)
class DatePhrase:
    start: int                 # the first token, the cue's when there is one
    end: int                   # one past the last token
    cue: str | None
    month: int
    year: int | None = None
    day: int | None = None
    # A range's last day as (year, month, day): "between July 1 and July 10, 2026", "from March to May
    # 2026", a quarter, a list of consecutive months. A missing day is the whole month.
    until: tuple[int | None, int, int | None] | None = None
    # A list of months no single range covers ("January and March"): no comparison keeps them.
    scattered: bool = False

    def comparisons(self, column: ColumnRef) -> tuple[Comparison, ...]:
        """The typed comparisons on ``column``, a date column, that keep the phrase's dates. A day
        without a year ("after August 10"), a yearless range across the year's end and a scattered list
        name no span to compare with: none."""
        if self.scattered:
            return ()
        year, month, day = self.until or (self.year, self.month, self.day)
        if self.year is None or year is None:
            if self.day is not None or day is not None or self.month > month:
                return ()
            part = DatePart("month", column)
            if self.month == month:
                return (Comparison(part, "=", Literal(self.month, SQLType.INTEGER)),)
            return (Comparison(part, ">=", Literal(self.month, SQLType.INTEGER)),
                    Comparison(part, "<=", Literal(month, SQLType.INTEGER)))
        try:
            first = date(self.year, self.month, self.day or 1)
            if day is not None:
                after = date(year, month, day) + timedelta(days=1)
            else:
                after = date(year + month // 12, month % 12 + 1, 1)
        except ValueError:                                       # "between February 30 and ..."
            return ()
        if after <= first:
            return ()
        bounds = {"start": first.isoformat(), "end": after.isoformat()}
        return tuple(Comparison(column, operator, Literal(bounds[side], SQLType.DATE))
                     for operator, side in _CUES.get(self.cue, _WITHIN))


@dataclass(frozen=True)
class _Mention:
    """One day, month or quarter the question names, before cues, ranges and lists are read."""
    start: int
    end: int
    month: int
    year: int | None
    day: int | None
    last: int                  # the last month it covers: a quarter's third, else its own
    word: str                  # the month or quarter token


def _number(token: str, low: int, high: int) -> int | None:
    if re.fullmatch(r"\d{1,4}", token) and low <= int(token) <= high:
        return int(token)
    return None


def _day(token: str) -> int | None:
    match = _DAY.fullmatch(token)
    return int(match.group(1)) if match and 1 <= int(match.group(1)) <= 31 else None


def _year(tokens: Sequence[str], index: int) -> int | None:
    return _number(tokens[index], 1000, 9999) if index < len(tokens) else None


def _mentions(question: str, tokens: tuple[str, ...]) -> list[_Mention]:
    found: list[_Mention] = []
    claimed: set[int] = set()
    for match in _ISO.finditer(question):
        year, month, day = (int(part) for part in match.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        parts = tuple(str(int(part)) if len(part) < 4 else part for part in match.groups())
        for index in range(len(tokens) - 2):
            if (index not in claimed and tuple(str(int(t)) if t.isdigit() and len(t) < 4 else t
                                               for t in tokens[index:index + 3]) == parts):
                found.append(_Mention(index, index + 3, month, year, day, month, match.group(0)))
                claimed.update(range(index, index + 3))
                break
    for index, token in enumerate(tokens):
        if index in claimed:
            continue
        if token in MONTHS:
            month, start, end, year, day = MONTHS[token], index, index + 1, None, None
            following = tokens[index + 1:index + 2]
            if following and (day := _day(following[0])) is not None:
                # "August 10" names no year: the phrase is read, so "10" is no number, but filters nothing.
                year = _year(tokens, index + 2)
                end = index + 3 if year is not None else index + 2
            else:
                year = _year(tokens, index + 1)
                end = index + 2 if year is not None else index + 1
                if index > 1 and tokens[index - 1] == "of" and (before := _day(tokens[index - 2])) is not None:
                    day, start = before, index - 2              # "the 10th of August 2026"
                elif (index > 0 and (before := _day(tokens[index - 1])) is not None
                      and (year is not None or not tokens[index - 1].isdigit())):
                    day, start = before, index - 1              # "10 August 2026", "10th August"
            found.append(_Mention(start, end, month, year, day, month, token))
        elif token in QUARTERS or (token in _ORDINAL_QUARTERS and tokens[index + 1:index + 2] == ("quarter",)):
            quarter = QUARTERS.get(token) or _ORDINAL_QUARTERS[token]
            end = index + 1 if token in QUARTERS else index + 2
            if tokens[end:end + 1] == ("of",) and _year(tokens, end + 1) is not None:
                end += 1                                         # "the third quarter of 2026"
            year = _year(tokens, end)
            found.append(_Mention(index, end + (year is not None), 3 * quarter - 2, year, None, 3 * quarter,
                                  token))
    found.sort(key=lambda mention: mention.start)
    out: list[_Mention] = []
    for mention in found:                                        # overlapping reads: the earlier one stands
        if out and mention.start < out[-1].end:
            continue
        out.append(mention)
    return out


def _cue(tokens: tuple[str, ...], start: int) -> tuple[str | None, int]:
    """The cue before a phrase that starts at ``start``, past one "the" ("after the 10th of August"),
    and where the phrase then starts."""
    index = start - 1
    if index > 0 and tokens[index] == "the":
        index -= 1
    if index >= 0 and tokens[index] in _CUES:
        return tokens[index], index
    return None, start


def _range(first: _Mention, last: _Mention, start: int, end: int) -> DatePhrase:
    """The range from ``first`` to ``last``; an end without a year takes the other's, the year before
    or after when the range crosses a year's end ("from November to February 2026")."""
    first_year, last_year = first.year, last.year
    ordered = (first.month, first.day or 0) <= (last.last, last.day or 31)
    if first_year is None and last_year is not None:
        first_year = last_year if ordered else last_year - 1
    elif last_year is None and first_year is not None:
        last_year = first_year if ordered else first_year + 1
    return DatePhrase(start, end, "between", first.month, first_year, first.day,
                      (last_year, last.last, last.day))


def _opening_day(tokens: tuple[str, ...], mention: _Mention, dashed: set[tuple[str, str]],
                 free_from: int) -> int | None:
    """The token of a bare day that opens a range ``mention`` closes, its month the mention's: "from
    5th to 10th August 2026", "between the 5th and the 10th of August", "5-10 August 2026". Tokens
    before ``free_from`` belong to an earlier phrase."""
    if mention.day is None:
        return None
    index = mention.start - 1
    if index > 0 and tokens[index] == "the":
        index -= 1
    if index < free_from:
        return None
    if _day(tokens[index]) is not None and (tokens[index], tokens[mention.start]) in dashed:
        return index
    join, index = tokens[index], index - 1
    if index < free_from or (join not in _RANGE_JOINS and join != "and") or _day(tokens[index]) is None:
        return None
    cue, _ = _cue(tokens, index)
    return index if cue == "between" or (cue == "from" and join != "and") else None


def _dashed(question: str) -> set[tuple[str, str]]:
    """The word pairs the question joins with a dash: "March-May 2026" is one range, which the search's
    tokens, without punctuation, cannot tell from the list "March, May"."""
    return {(left, right) for left, right in _DASHED.findall(question.lower())}


def date_phrases(question: str, tokens: Sequence[str]) -> tuple[DatePhrase, ...]:
    """The calendar phrases of ``question``, as spans of its lowercase ``tokens`` (the search's own).

    A month without a year is read only as a whole month ("in August"): "after August" names no year
    to compare with, and stays unread. A day without a year ("after August 10") is read and compares
    nothing, so the coverage gate asks about it.
    """
    tokens = tuple(tokens)
    mentions = _mentions(question, tokens)
    dashed = _dashed(question)
    out: list[DatePhrase] = []
    index = 0
    while index < len(mentions):
        mention = mentions[index]
        opening = _opening_day(tokens, mention, dashed, mentions[index - 1].end if index else 0)
        if opening is not None:
            first = _Mention(opening, opening + 1, mention.month, None, _day(tokens[opening]), mention.month,
                             tokens[opening])
            out.append(_range(first, mention, _cue(tokens, opening)[1], mention.end))
            index += 1
            continue
        cue, start = _cue(tokens, mention.start)
        following = mentions[index + 1] if index + 1 < len(mentions) else None
        between = tokens[mention.end:following.start] if following else None
        # A range: "between A and B", "from A to B", "A through B", "A-B".
        if following is not None and (
                (len(between) == 1 and between[0] in _RANGE_JOINS)
                or (between == ("and",) and cue == "between")
                or (between == () and (cue in ("from", "between")
                                       or (tokens[mention.end - 1], tokens[following.start]) in dashed))):
            out.append(_range(mention, following, start, following.end))
            index += 2
            continue
        # A list of months: "June, July and August 2026", "in July or August".
        listed = [mention]
        while (index + len(listed) < len(mentions)
               and listed[-1].day is None and mentions[index + len(listed)].day is None
               and tokens[listed[-1].end:mentions[index + len(listed)].start] in _LIST_JOINS):
            listed.append(mentions[index + len(listed)])
        if len(listed) > 1 and mention.day is None:
            out.append(_listed(listed, start, cue))
            index += len(listed)
            continue
        index += 1
        if cue == "between":                                     # a "between" without its second end
            continue
        end = mention.end
        if cue == "from" and tokens[end:end + 1] and tokens[end] in _ONWARD:
            cue, end = "since", end + 1                          # "from August 2026 onwards"
        if mention.year is None and mention.day is None and (
                cue not in _YEARLESS_CUES or (cue is None and mention.word in _AMBIGUOUS)):
            continue
        if mention.year is not None and mention.day is not None:
            try:
                date(mention.year, mention.month, mention.day)
            except ValueError:
                continue
        until = (mention.year, mention.last, None) if mention.last != mention.month else None
        out.append(DatePhrase(start, end, cue, mention.month, mention.year, mention.day, until))
    return tuple(out)


def _listed(listed: list[_Mention], start: int, cue: str | None) -> DatePhrase:
    """A list of months as one phrase: the range they cover when each follows the one before, else a
    scattered list. A year the list names closes it, so it is every earlier month's too."""
    years: list[int | None] = [mention.year for mention in listed]
    for position in range(len(listed) - 2, -1, -1):
        if years[position] is None and years[position + 1] is not None:
            later = years[position + 1]
            years[position] = later if listed[position].last < listed[position + 1].month else later - 1
    for position in range(1, len(listed)):                      # "August 2026 and September"
        if years[position] is None and years[position - 1] is not None:
            earlier = years[position - 1]
            years[position] = earlier if listed[position].month > listed[position - 1].last else earlier + 1
    consecutive = all(
        (current.month == previous.last % 12 + 1)
        and (year is None) == (previous_year is None)
        and (year is None or year == previous_year + (previous.last == 12))
        for previous, current, previous_year, year in zip(listed, listed[1:], years, years[1:]))
    first, last = listed[0], listed[-1]
    return DatePhrase(start, last.end, cue if cue in _YEARLESS_CUES else None, first.month, years[0], None,
                      (years[-1], last.last, None), scattered=not consecutive)


def served_date_phrases(question: str, tokens: Sequence[str], schema) -> tuple[DatePhrase, ...]:
    """The phrases a query over ``schema`` (a SchemaGraph) can realize: only where a date column takes
    their comparisons, and a lone month that is a value of the data ("the first name April", a text
    month column's "may") stays that value."""
    if not any(column.ref.type == SQLType.DATE for column in schema.columns):
        return ()
    return tuple(phrase for phrase in date_phrases(question, tokens)
                 if phrase.year is not None or phrase.day is not None
                 or not any(schema.value_index.get(token) for token in tokens[phrase.start:phrase.end]
                            if token in MONTHS or token in QUARTERS))


def _comparison_key(comparison: Comparison):
    left = comparison.left
    side = (("month",) + (left.operand.table, left.operand.name) if isinstance(left, DatePart)
            else (left.table, left.name) if isinstance(left, ColumnRef) else None)
    right = comparison.right
    value = str(right.value) if isinstance(right, Literal) else None
    return side, comparison.operator, value


def _where_comparisons(predicate):
    if isinstance(predicate, Comparison):
        yield predicate
    elif isinstance(predicate, BooleanExpr) and predicate.operator == "AND":
        for term in predicate.terms:
            yield from _where_comparisons(term)


def realizes_dates(query, phrases: Sequence[DatePhrase]) -> bool:
    """Whether ``query`` keeps the rows every phrase names: its WHERE holds each phrase's comparisons on
    one date column, however a proposer typed the literal."""
    if not phrases or not isinstance(query, SelectQuery):
        return False
    present = {_comparison_key(comparison) for comparison in _where_comparisons(query.where)}
    columns = {comparison.left.operand if isinstance(comparison.left, DatePart) else comparison.left
               for comparison in _where_comparisons(query.where)
               if isinstance(comparison.left, (ColumnRef, DatePart))}
    return all(
        any((wanted := phrase.comparisons(column))
            and all(_comparison_key(comparison) in present for comparison in wanted)
            for column in columns)
        for phrase in phrases
    )


def realized_month_words(question: str, sql: str) -> frozenset[str]:
    """The calendar words of ``question`` (months, quarters) whose comparisons ``sql`` carries, for the
    coverage gate."""
    from engine.sql_expansion import tokens as question_tokens

    tokens = question_tokens(question)
    realized = set()
    month_of = re.escape(month_of_date_sql("§")).replace("§", ".+?")
    for phrase in date_phrases(question, tokens):
        comparisons = phrase.comparisons(ColumnRef("t", "c", SQLType.DATE))
        found = bool(comparisons)
        for comparison in comparisons:
            if isinstance(comparison.left, DatePart):
                operator = r"(?:\s*=\s*|,\s*)" if comparison.operator == "=" else (
                    r"\s*" + re.escape(comparison.operator) + r"\s*")
                found = found and re.search(month_of + operator + str(comparison.right.value) + r"\b",
                                            sql or "", re.I) is not None
            else:
                found = found and f"'{comparison.right.value}'" in (sql or "")
        if found:
            realized.update(token for token in tokens[phrase.start:phrase.end]
                            if token in MONTHS or token in QUARTERS or token == "quarter")
    return frozenset(realized)
