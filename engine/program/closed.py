"""The closed questions: the ONE owner of what the Decisions API (engine/llm.py decide) is asked about a question and how
its probabilities come back into the engine. The API writes no text, no SQL and no program: every question is a closed
choice among options this module built from the sheet and the stock reading, and every answer is a probability per option.

Two records, both over the same description of the sheet (``sheet_state``: each table's tab, line count, and per column
its header, kind and three example cells; on a workbook of several tabs, the tab in view):

  reading record   one question per decoder slot the stock reading scored (form, aggregate, answer type, direction,
                   table, measure, key, shown, filter, value, line, section, quantifier, neighbour, comparison, blank,
                   calc_op, entity), its options exactly the coordinates the reading wrote for that slot plus the closed
                   lists only the API reads, so each probability maps back onto a coordinate the decoder reads; and the
                   added questions for what no slot offers: a number written as a word, a period written in words (on a
                   workbook with days, or with a column of year numbers), a day written with its month as a word and how a
                   date is restricted to it, a year compared with a column of year numbers, how many cell values are
                   named, and whether a line's figure is a percent of a rate. A second call asks which set of the value
                   question's likeliest values the question names. The said call, over the question alone, asks which
                   of its own words answer a versus when one figure is above the other and which when below. The order
                   call, over the question alone, asks whether it takes lines by where they stand in a table: the
                   first ones or the last.
  choice record    one question over the candidates that ran (at most CHOOSE_K, decoder order): each one's read-as
                   sentence and a preview of the answer it returns, and none.

``fused_reading`` turns the reading record's answers into the decoder's reading: on every slot the API answered, the
option's term W * (log p - log 1/n) replaces the stock score (the value slot is read against none: W * (log p - log
p(none))); an added question's option joins the reading only when the API holds it likelier than none. ``choose_answer``
is the decision: a candidate's probability is summed over the listed candidates that return the same answer; the
likeliest answer is given when none is not likelier and its probability reaches T_ERR, with a warning below T_FLAG, and
is most likely wrong (ERRED) otherwise: it is never asked back for (owner, 2026-11-21: "never ask back unless the
question is wrong"), it is retried (engine/retry.py) and, when the retry cannot answer, served marked as such.

W, T_ERR and T_FLAG are the CLEF path's own, fixed before the Decisions measurement (decisions-probe-20261118): W was
picked on Enron, the thresholds on factory validation; none is tuned for the API.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import combinations

from engine.named.backbone import WORD
from engine.named.names import CLOSED_HEADS, NONE
from engine.named.reader import NamedReading
from engine.program.catalog import KIND_FAMILY, KIND_TABLE, Catalog
from engine.program.decoder import table_reach
from engine.program.index import index_of
from engine.program.language import PeriodSpan, period_from_name
from engine.program.names import (BETWEEN, DEVIATION, EVERY_COLUMN, FIRST, LAST, MEDIAN, PERCENT_OP, RUN, SLOT_AGGREGATE, SLOT_COMPARISON, SAID_ABOVE, SAID_BELOW, SLOT_ANSWER_TYPE, SLOT_LISTING, SLOT_NUMBER, SLOT_ORDER, SLOT_PERIOD,
                                  SLOT_ROW_OP, SLOT_SAID, SLOT_SHOWN, coarse_type, coordinate)
from engine.program.scoring import Reading
from engine.program.semantics import fold, number_text
from engine.program.words import YEARS_FROM
from engine.sheet.names import P_DAY, P_MONTH, P_QUARTER, P_RANGE, P_YEAR

W = 2.0                    # an API probability's weight on the decoder's reading
T_ERR = 0.42               # below this, the likeliest answer is most likely wrong: retried, never asked back
T_FLAG = 0.575             # below this, the likeliest answer is given with a warning
CHOOSE_K = 50              # candidates the choice offers
# What a second judge's answer says of an answered answer (choose_answer): it picked another answer, it picked none, or
# it did not answer. Any of them warns.
DOUBT_ANOTHER, DOUBT_NONE, DOUBT_UNREAD = "another", "none", "unread"
UNJUDGED = "unjudged"      # the second judge's call failed: no key of its own
P_FLOOR = 1e-4             # a probability is read no smaller than this
SCORE_DIGITS = 4
ANSWERED, WARNED, ERRED = "answered", "warned", "erred"

# ------------------------------------------------------------------------------------------- the sheet's description
EXAMPLES, CELL_CHARS, MAX_COLUMNS = 3, 30, 60
FAMILY_TABS_NAMED = 3      # a family table merging more tabs is named by its first and last
FAMILY_TABS_LISTED = 12
TAB_IN_VIEW_CONVENTION = ("A question that names no tab, when tables on several tabs could answer it alike, asks about "
                          "the tab in view.")


def sheet_state(catalog: Catalog, question: str, active_sheet: str | None = None) -> dict:
    """STATE of both records: the question and each table's tab, line count and columns (id, header, kind, three example
    cells). A family table is named by all the tabs it merges, as the engine's read-as names it. A workbook whose data
    tables sit on two tabs or more also names the tab in view (the client's active sheet when it is one of them, else the
    tab of the first data table) and the convention production keeps among copies of one layout; a workbook on one tab
    is described without them. The request carries the tab in view (engine/prepared.py RequestDatabase.active_sheet)."""
    tables = []
    for table in catalog.tables:
        columns = []
        for position, column in enumerate(table.columns[:MAX_COLUMNS]):
            seen: list[str] = []
            for text in table.texts[position]:
                if text and text not in seen:
                    seen.append(text[:CELL_CHARS])
                if len(seen) >= EXAMPLES:
                    break
            columns.append({"id": f"{table.id}.{position}", "header": column.name, "kind": getattr(column, "kind", ""), "examples": seen})
        entry = {"table": table.id, "tab": table.tab, "lines": len(table.lines), "columns": columns}
        tabs = list(dict.fromkeys(line.tab for line in table.lines)) if table.kind == KIND_FAMILY else []
        if len(tabs) > 1:
            entry["tab"] = " + ".join(tabs) if len(tabs) <= FAMILY_TABS_NAMED else f"{tabs[0]} to {tabs[-1]} ({len(tabs)} tabs)"
            entry["merges tabs"] = tabs[:FAMILY_TABS_LISTED]
        tables.append(entry)
    state = {"question": question, "workbook": tables}
    data = [table for table in catalog.tables if table.kind in (KIND_TABLE, KIND_FAMILY) and table.lines]
    tabs = list(dict.fromkeys(line.tab for table in data for line in table.lines))
    if len(tabs) < 2:
        return state
    return {**state, "tab in view": active_sheet if active_sheet in tabs else data[0].tab, "convention": TAB_IN_VIEW_CONVENTION}


# ------------------------------------------------------------------------------------------------- the reading record
FIELDS = {
    "form": "What kind of answer does the question ask for? show = values or rows read from the sheet; agg = one number "
            "aggregated over the kept rows; rank = the top or bottom row, group or column; calc = a difference, sum or ratio of two numbers.",
    "aggregate": "Which aggregate does the question compute over the kept rows, if any?",
    "answer_type": "What type of thing is the answer?",
    "direction": "If the question ranks or orders, which end does it want?",
    "table": "Which table of the workbook holds the answer?",
    "measure": "Which column holds the numbers the question adds up, averages, counts distinct, or ranks by? none if no number column is computed.",
    "key": "Which column names the things the answer lists, groups by or ranks? none if the answer names no such things.",
    "shown": "Which column's values does the answer show? none if the answer shows no column's values.",
    "filter": "Which column does the question restrict to some rows (by a value, a number, a date or a period)? none if it keeps every row.",
    "value": "Which cell value does the question's restriction refer to? none if it names no cell value.",
    "line": "Which printed line (a total, subtotal or labelled line) does the question read? none if it reads no printed line.",
    "section": "Which section or band of the sheet does the question restrict to? none if none.",
    "quantifier": "If the question asks a yes or no, what does it ask of the kept rows? any = some row meets it; no = no row "
                  "does; every = every row it names also meets one more condition; versus = a number against a stated "
                  "number; is = the top row's value (the oldest one, the most common one) is a stated value or compares with a stated number. none if it does not ask a yes or no.",
    "neighbour": "Does the question read the line just below or just above the line it names (the next one, the one "
                 "after, the following one; the previous one, the one before, the preceding one)? after = the line "
                 "below it; before = the line above it. none if it reads the line it names itself.",
}
CONDITION_FIELDS = {
    "comparison": "Does the question restrict rows by comparing, or by excluding the value it names? != = every row "
                  "other than the named value (not, except, excluding, besides, non-); > = above a number or after a "
                  "date (more than, over, after, later than); < = below or before (less than, under, before, earlier "
                  "than); >= = at least; <= = at most. none if it only names values to keep.",
    "blank": "Does the question keep rows by whether a column has anything in it, without naming a value? filled = rows "
             "where the column has something (used a code, has a note, assigned, with a date); blank = rows where it "
             "has nothing (missing, without, unassigned, no date); number = rows where a mixed column holds a number. "
             "none if it names the values it keeps or keeps every row.",
    "calc_op": "If the answer is a calculation between two figures of the sheet, which one? - = the difference between "
               "them (change, gap, how much more, net of); / = one divided by the other (a ratio, a share, percent of, "
               "rate per); + = the two added together. none if the answer is one figure, read or aggregated.",
    "entity": "If the question asks for the top or bottom one, of what? line = a single row (the largest order, the "
              "oldest ticket); group = the rows that share a key, added or counted together (the customer with the "
              "most orders, the region with the highest total); peer = one column among columns of the same kind (which "
              "month was highest). none if it asks for no top or bottom one.",
}
READ_FIELDS = {**FIELDS, **CONDITION_FIELDS}
WITH_NONE = {"measure", "key", "shown", "filter", "value", "line", "section", "direction", "aggregate", "quantifier", "neighbour", *CONDITION_FIELDS}
# The closed lists only the API reads, each offered whole after every option the stock reading scored: a condition's
# comparison, filled or blank, the calculation and the entity ranked; the yes-or-no form, its quantifier and answer type;
# the neighbouring line and the quantifier same; the breakdown form; and every form.
CONDITION_OPTIONS = {"comparison": ("!=", ">", "<", ">=", "<="), "blank": ("filled", "blank", "number"),
                     "calc_op": ("-", "/", "+"), "entity": ("line", "group", "peer")}
ADDED_OPTIONS = ({"form": ("check",), "quantifier": ("any", "no", "every", "versus", "is"), "answer_type": ("yes_no",)},
                 {"neighbour": ("after", "before"), "quantifier": ("same",)},
                 {"form": ("breakdown",)},
                 {"form": ("show", "agg", "rank", "calc")})
FORM_INSTRUCTION = (FIELDS["form"][:-1] + "; check = a yes or no: whether any, no or every kept row meets a condition, or "
                    "whether a number is above, below or equal to a stated number.")[:-1] + (
                    "; breakdown = a figure (a total, an average, a lowest or highest, or a count) for each group of the key "
                    "column, one row per group (per, by, for each, split by, broken down by).")
QUANTIFIER_INSTRUCTION = FIELDS["quantifier"].replace(
    "versus = a number against a stated number;", "versus = a number against a stated number or another number of the sheet;").replace(
    " none if it does not ask", " same = the top one by one measure (the highest, the most) is also the top one by another. none if it does not ask")
OPTION_CHARS = 160         # an option names a cell value or a printed line by its text, clipped
OPTIONS_MOST = 80          # options of every slot but the value slot
VALUE_OPTIONS_MOST = 160
OFFERED_SCORE = -50.0      # an option the stock reading did not score sorts after every one it did
SIZE_STEP = 0.01           # per distinct text of its column (per place, for a line), an offered value sorts this much later
VALUES_PER_COLUMN = 20     # the stock reading's values kept per column, best first
EXPAND_VALUES = 30         # every distinct text of a column holding at most this many is offered as a value
FILTER_SCORE_BOUND = 40.0
LINES_PER_TABLE = 60


def _clipped(text: str) -> str:
    return text if len(text) <= OPTION_CHARS else text[:OPTION_CHARS] + "..."


def _describe(slot: str, rest: str, columns: Mapping[str, dict]) -> str:
    """How an option reads: a column by id and header, a value by its column's header and its text, a line or section by
    its label and table."""
    if slot in {"measure", "key", "shown", "filter"}:
        column = columns.get(rest)
        return f"column {rest}: {column['header']}" if column else f"column {rest}"
    if slot == "value" and "=" in rest:
        target, value = rest.split("=", 1)
        column = columns.get(target)
        return f"{column['header'] if column else target} = {_clipped(value)!r}"
    if slot in {"line", "section"} and "=" in rest:
        table, label = rest.split("=", 1)
        return f"{slot} {_clipped(label)!r} on {table}"
    return rest


def _offered_values(catalog: Catalog) -> list[tuple[str, int]]:
    """Every distinct cell text of each column holding two to EXPAND_VALUES distinct texts, or one text beside blanks (a
    mark: "Yes", "x"), as "<table>.<position>=<text>" with how many distinct texts its column holds. A value worded unlike
    the question can still be read; the stock reading proposes values only by likeness to the question's words."""
    out = []
    for table in catalog.tables:
        for position in range(len(table.columns)):
            texts, blank = [], False
            for text in table.texts[position]:
                if not text:
                    blank = True
                elif text not in texts:
                    texts.append(text)
                    if len(texts) > EXPAND_VALUES:
                        break
            if 1 < len(texts) <= EXPAND_VALUES or (len(texts) == 1 and blank):
                out.extend((f"{table.id}.{position}={text}", len(texts)) for text in texts)
    return out


def _slot_options(named: NamedReading, catalog: Catalog) -> dict[str, dict[str, float]]:
    """Each slot's options, slot -> {coordinate without its slot: the order it is offered in, higher first}."""
    by_slot: dict[str, dict[str, float]] = {}
    for name, score in named.reading.scores.items():
        slot, rest = name.split(":", 1)
        if slot in READ_FIELDS and (slot not in CONDITION_OPTIONS or rest in CONDITION_OPTIONS[slot]):
            by_slot.setdefault(slot, {})[rest] = score
    for options in (CONDITION_OPTIONS, *ADDED_OPTIONS):
        for slot, values in options.items():
            for value in values:
                by_slot.setdefault(slot, {}).setdefault(value, OFFERED_SCORE)
    held = by_slot.setdefault("line", {})
    for table in catalog.tables:       # each table's printed lines, then its labelled data lines, in sheet order
        printed = [label for label in index_of(table).printed_labels if label]
        labelled = [label for label in dict.fromkeys(table.label_folds) if label and label not in printed]
        if len(labelled) > LINES_PER_TABLE:     # a record table's labels are its rows, not lines a question names
            labelled = []
        for at, label in enumerate((printed + labelled)[:LINES_PER_TABLE]):
            held.setdefault(f"{table.id}={label}", OFFERED_SCORE - SIZE_STEP * at)
    if "value" in by_slot:             # a word matching a thousand names keeps VALUES_PER_COLUMN of one column
        counts: dict[str, int] = {}
        kept = {}
        for rest in sorted(by_slot["value"], key=lambda rest: (-by_slot["value"][rest], rest)):
            column = rest.split("=", 1)[0]
            if counts.get(column, 0) < VALUES_PER_COLUMN:
                counts[column] = counts.get(column, 0) + 1
                kept[rest] = by_slot["value"][rest]
        by_slot["value"] = kept
    values, filters = by_slot.setdefault("value", {}), by_slot.get("filter", {})
    for rest, size in _offered_values(catalog):     # those of the likelier filter columns first, fewer texts first
        column_score = max(-FILTER_SCORE_BOUND, min(FILTER_SCORE_BOUND, filters.get(rest.split("=", 1)[0], -FILTER_SCORE_BOUND)))
        values.setdefault(rest, OFFERED_SCORE + column_score - SIZE_STEP * size)
    return by_slot


def _one_value_each(questions: dict, options: dict) -> None:
    """Every value option on the coordinate a program reads ("value:<column>=<the value folded>",
    engine/program/language.py condition_coordinates), each value offered once: the better-ranked option stays, worded
    with the sheet's own spelling where one is offered."""
    mapping = options.get("value")
    if not mapping:
        return
    criteria = questions["value"]["criteria"]
    kept, texts, order = set(), {}, []
    for option, name in mapping.items():
        column, _, value = name.split(":", 1)[1].partition("=")
        canonical = f"value:{column}={fold(value)}"
        if canonical not in kept:
            kept.add(canonical)
            order.append(canonical)
            texts[canonical] = criteria[option]
        elif value != fold(value):
            texts[canonical] = criteria[option]
    merged = {f"o{at:02d}": texts[canonical] for at, canonical in enumerate(order)}
    if NONE in criteria:
        merged[NONE] = criteria[NONE]
    options["value"] = {f"o{at:02d}": canonical for at, canonical in enumerate(order)}
    questions["value"] = {**questions["value"], "criteria": merged}


# ---------------------------------------------------------------------------------------------- the added questions
STATED_NUMBERS = tuple(range(0, 21))
STATED_NUMBER_QUESTION = ("Which number does the question write as a word rather than in digits: how many it asks to list, or a "
                          "number it compares a count or an amount with? none if it writes no number as a word.")
STATED_PERIOD_QUESTION = ("Which period does the question restrict a date to, beyond the years and dates it writes in digits: a month, "
                          "a quarter, a range between two years or two dates, or everything before or after one? none if it "
                          "names no such period, or only a year or a date already written in digits.")
PERIOD_FIRST, PERIOD_LAST = "0001-01-01", "9999-12-31"
PERIODS_STATED_MOST = 2
WRITTEN_DAY_QUESTION = ("Which calendar days does the question write with the month as a word (July 10, 2026; 4 Aug 2026)? "
                        "none if it writes no day that way.")
DATE_RELATION_QUESTION = ("How does the question restrict a date to the day or month it names? on = on that day or within that month; "
                          "before = earlier, the named one not included; until = up to it, the named one included; from = from it on, the "
                          "named one included; after = later, the named one not included; between = from the first named to the second, "
                          "both included. none if it restricts no date that way.")
DATE_RELATIONS = ("on", "before", "until", "from", "after", "between")
DAY_NUMBERS_MOST = 4
# the whole numbers from 1 to 31 the question writes in digits; the word reader drops a number a comma follows ("July 10, 2026")
DAY_NUMBER = re.compile(r"(?<![\d.,/:-])(\d{1,2})(?![\d/:-]|[.,]\d)")
YEAR_QUESTION = ("Does the question compare a column of plain year numbers with a year it writes (built after 1940, listed "
                 "before 1974, since 2010)? Which year? none if it compares no such column with a year.")
YEAR_COLUMN_SHARE = 0.8    # a column of year numbers: at least this share of its numbers are whole numbers 1000 to 2999
YEARS_ASKED_MOST = PERIODS_STATED_MOST + 2
VALUE_COUNT_QUESTION = ("How many different values shown in the sheet's cells does the question name as the lines to keep, count "
                        "or compare? Count each value once. none if it names no cell value.")
VALUE_COUNTS = ("1", "2", "3", "4 or more")
ROW_FORMULA_QUESTION = ("Does the answer compute a figure on each line of a table from two of its columns, before the lines are "
                        "added, averaged, counted or ranked? Pick the formula, a and b being the figures the two columns hold "
                        "on one line. none if each line's figure is one column's own figure.")
# Options in symbols only; the three formulas over 100 read the engine's percent operator (PercentOf).
ROW_FORMULAS = ("a × b", "a − b", "a + b", "a ÷ b", "a × b ÷ 100", "a − a × b ÷ 100", "a + a × b ÷ 100")
PERCENT_FORMULAS = ROW_FORMULAS[4:]
VALUE_SET_QUESTION = ("Which cell values does the question keep, count or compare the lines by? Choose the option that holds "
                      "every value the question names among these and no other. none if it names none of them.")
VALUE_SET_MOST = 4
VALUE_SET_FLOOR = 0.005


def _stated_periods(scores: Mapping[str, float], kind: str) -> list[PeriodSpan]:
    return [span for span in (period_from_name(name.split(":", 1)[1]) for name, score in scores.items()
                              if name.startswith(f"{SLOT_PERIOD}:") and score > 0) if span.kind == kind]


def _has_days(catalog: Catalog) -> bool:
    return any(any(days) for table in catalog.tables for days in table.days)


def stated_number_question(questions: dict) -> dict[str, str]:
    """A number the question writes as a word: the word reader reads only digits (engine/program/words.py), so "which
    three states" keeps no count and a rank of three is never built. Options: the whole numbers 0 to 20, as digits, on the
    decoder's own number coordinates. Returns {option: coordinate}."""
    mapping = {f"o{at:02d}": coordinate(SLOT_NUMBER, number_text(n)) for at, n in enumerate(STATED_NUMBERS)}
    criteria = {option: name.split(":", 1)[1] for option, name in mapping.items()}
    criteria[NONE] = "no number is written as a word"
    questions["stated_number"] = {"instructions": STATED_NUMBER_QUESTION, "criteria": criteria}
    return mapping


def stated_period_question(questions: dict, scores: Mapping[str, float], catalog: Catalog, question: str = "") -> dict[str, str]:
    """A period the question writes in words. The word reader reads days, months and years written in digits; a month's
    name, a quarter, "between", "before" and "since" are read by nothing, though the language holds each (PeriodSpan)
    and the decoder builds a date condition from any positive period coordinate. Options from the years and days the
    question writes in digits: each year's months and quarters, everything before, through, from and after each year or
    day, the range between two of them, the ten years and, ending in 00, the hundred years that a year written with
    letters glued on begins ("the 1970s", "the 1800's": words.YEARS_FROM), and (with none stated) each month of any
    year. Asked of a workbook holding a
    column of days or a column of year numbers (a span of whole years tests those, InYears). Returns {option: coordinate}."""
    if not _has_days(catalog) and not any(index_of(table).year_columns for table in catalog.tables):
        return {}
    stated = sorted((name for name, score in scores.items() if name.startswith(f"{SLOT_PERIOD}:") and score > 0), key=lambda name: (-scores[name], name))
    spans = [period_from_name(name.split(":", 1)[1]) for name in stated]
    years = sorted({span.start[:4] for span in spans if span.kind == P_YEAR and span.start})[:PERIODS_STATED_MOST]
    days = sorted({span.start for span in spans if span.kind == P_DAY and span.start})[:PERIODS_STATED_MOST]
    decades = {match.group(1) for match in YEARS_FROM.finditer(question.lower())}
    options: list[tuple[str, str]] = []

    def ranged(first: str, last: str, words: str) -> None:
        options.append((PeriodSpan(P_RANGE, first, last).name, words))

    for year in years:
        y = int(year)
        options += [(f"{year}-{month:02d}", f"{year}-{month:02d}") for month in range(1, 13)]
        options += [(f"{year}-q{quarter}", f"{year}-Q{quarter}") for quarter in range(1, 5)]
        ranged(PERIOD_FIRST, f"{y - 1:04d}-12-31", f"before {year}, {year} not included")
        ranged(PERIOD_FIRST, f"{year}-12-31", f"up to {year}, {year} included")
        ranged(f"{year}-01-01", PERIOD_LAST, f"from {year} on, {year} included")
        ranged(f"{y + 1:04d}-01-01", PERIOD_LAST, f"after {year}, {year} not included")
        if year in decades:     # "the 1970s" names the ten years 1970 begins; "the 1800s" maybe the hundred 1800 begins
            ranged(f"{year}-01-01", f"{y + 9:04d}-12-31", f"the ten years {year} to {y + 9}, both included")
            if y % 100 == 0:
                ranged(f"{year}-01-01", f"{y + 99:04d}-12-31", f"the hundred years {year} to {y + 99}, both included")
    if len(years) == 2:
        ranged(f"{years[0]}-01-01", f"{years[1]}-12-31", f"{years[0]} to {years[1]}, both included")
    for day in days:
        d = dt.date.fromisoformat(day)
        ranged(PERIOD_FIRST, (d - dt.timedelta(days=1)).isoformat(), f"before {day}, that day not included")
        ranged(PERIOD_FIRST, day, f"up to {day}, that day included")
        ranged(day, PERIOD_LAST, f"from {day} on, that day included")
        ranged((d + dt.timedelta(days=1)).isoformat(), PERIOD_LAST, f"after {day}, that day not included")
    if len(days) == 2:
        ranged(days[0], days[1], f"{days[0]} to {days[1]}, both included")
    if not years and not days:
        options += [(f"m{month:02d}", f"month {month:02d} of any year") for month in range(1, 13)]
    mapping = {f"o{at:02d}": coordinate(SLOT_PERIOD, name) for at, (name, _) in enumerate(options)}
    criteria = {f"o{at:02d}": words for at, (_, words) in enumerate(options)}
    criteria[NONE] = "no such period"
    questions["stated_period"] = {"instructions": STATED_PERIOD_QUESTION, "criteria": criteria}
    return mapping


def written_day_questions(questions: dict, question: str, scores: Mapping[str, float], catalog: Catalog) -> dict[str, str]:
    """A day the question writes with its month as a word ("before July 10, 2026"): which day, and how the question
    restricts a date to it. The day's options are every month of each year the question writes in digits with each whole
    number from 1 to 31 its text writes in digits, as ISO days; the relation's options are DATE_RELATIONS. Asked when the
    workbook holds a column of days and the question writes a year and such a number. Returns {option: ISO day}."""
    if not _has_days(catalog):
        return {}
    years = sorted({int(span.start[:4]) for span in _stated_periods(scores, P_YEAR) if span.start})[:PERIODS_STATED_MOST]
    numbers = sorted({int(text) for text in DAY_NUMBER.findall(question) if 1 <= int(text) <= 31})[:DAY_NUMBERS_MOST]
    if not years or not numbers:
        return {}
    days = []
    for year in years:
        for month in range(1, 13):
            for day in numbers:
                try:
                    days.append(dt.date(year, month, day).isoformat())
                except ValueError:     # no 30th of February
                    continue
    mapping = {f"d{at:03d}": day for at, day in enumerate(days)}
    questions["written_day"] = {"instructions": WRITTEN_DAY_QUESTION, "criteria": {**mapping, NONE: "no day is written with its month as a word"}}
    questions["date_relation"] = {"instructions": DATE_RELATION_QUESTION, "criteria": {**{r: r for r in DATE_RELATIONS}, NONE: "no date is restricted that way"}}
    return mapping


def _year_columns(catalog: Catalog) -> bool:
    for table in catalog.tables:
        for numbers in table.numbers:
            held = [number for number in numbers if number is not None]
            if len(held) >= 3 and sum(1 for number in held if float(number).is_integer() and 1000 <= number <= 2999) >= YEAR_COLUMN_SHARE * len(held):
                return True
    return False


def stated_year_question(questions: dict, scores: Mapping[str, float], catalog: Catalog) -> dict[str, str]:
    """A year the question compares a column of plain year numbers with ("after 1940" over a 'Year built' column). The
    word reader reads a year written in digits as a period, never as a number, so no number comparison is built with it.
    Options: the years the question writes in digits; an answer joins the reading as the number coordinate. Asked when
    the workbook holds such a column. Returns {option: coordinate}."""
    years = sorted({int(span.start[:4]) for span in _stated_periods(scores, P_YEAR)})
    if not years or not _year_columns(catalog):
        return {}
    mapping = {f"y{chr(97 + at)}": coordinate(SLOT_NUMBER, number_text(year)) for at, year in enumerate(years[:YEARS_ASKED_MOST])}
    criteria = {option: name.split(":", 1)[1] for option, name in mapping.items()}
    questions["stated_year"] = {"instructions": YEAR_QUESTION, "criteria": {**criteria, NONE: "no such comparison"}}
    return mapping


def value_count_question(questions: dict, options: dict) -> bool:
    """How many cell values the question names. The value slot is one choice: a question naming two ("players from Lawton
    or Oklahoma City") splits the API's probability between them, and read against none each can fall below it. Options:
    counts as digits under letter keys. Asked when the value slot offers two values or more."""
    if len(options.get("value") or {}) < 2:
        return False
    criteria = {f"k{chr(97 + at)}": text for at, text in enumerate(VALUE_COUNTS)}
    questions["value_count"] = {"instructions": VALUE_COUNT_QUESTION, "criteria": {**criteria, NONE: "none"}}
    return True


def row_formula_question(questions: dict) -> tuple[str, ...]:
    """Whether a line's figure is computed from two of its columns, and by which formula; of the answer only the three
    formulas over 100 are read (a rate's percent of a measure, the engine's PERCENT_OP). Returns their option keys."""
    criteria = {f"o{at:02d}": formula for at, formula in enumerate(ROW_FORMULAS)}
    criteria[NONE] = "none"
    questions["row_formula"] = {"instructions": ROW_FORMULA_QUESTION, "criteria": criteria}
    return tuple(f"o{at:02d}" for at, formula in enumerate(ROW_FORMULAS) if formula in PERCENT_FORMULAS)


@dataclass(frozen=True)
class ReadingRecord:
    """The reading record: STATE, the questions (name -> {"instructions", "criteria"}), each slot's option -> coordinate,
    and the added questions' maps (empty where a question was not asked)."""
    state: dict
    questions: dict
    options: dict
    numbers: dict
    periods: dict
    days: dict
    years: dict
    counted: bool
    percent: tuple


def reading_record(question: str, named: NamedReading, catalog: Catalog, active_sheet: str | None = None) -> ReadingRecord:
    """The reading record of a question over the stock reading (engine/program/stock.py): one question per slot with two
    options or more, the stock reading's best first (at most OPTIONS_MOST; VALUE_OPTIONS_MOST for the value slot), then the
    added questions. Options are keyed o00, o01, ... in that order. active_sheet: the client's tab in view (sheet_state)."""
    state = sheet_state(catalog, question, active_sheet)
    columns = {c["id"]: c for t in state["workbook"] for c in t["columns"]}
    questions: dict[str, dict] = {}
    options: dict[str, dict[str, str]] = {}
    for slot, scored in sorted(_slot_options(named, catalog).items()):
        ids = sorted(scored, key=lambda rest: (-scored[rest], rest))[: VALUE_OPTIONS_MOST if slot == "value" else OPTIONS_MOST]
        criteria = {f"o{at:02d}": _describe(slot, rest, columns) for at, rest in enumerate(ids)}
        if slot in WITH_NONE:
            criteria[NONE] = "none of these"
        if len(criteria) < 2:
            continue
        instruction = FORM_INSTRUCTION if slot == "form" else QUANTIFIER_INSTRUCTION if slot == "quantifier" else READ_FIELDS[slot]
        questions[slot] = {"instructions": instruction, "criteria": criteria}
        options[slot] = {f"o{at:02d}": f"{slot}:{rest}" for at, rest in enumerate(ids)}
    _one_value_each(questions, options)
    scores = named.reading.scores
    numbers = stated_number_question(questions)
    periods = stated_period_question(questions, scores, catalog, question)
    days = written_day_questions(questions, question, scores, catalog)
    years = stated_year_question(questions, scores, catalog)
    counted = value_count_question(questions, options)
    percent = row_formula_question(questions)
    return ReadingRecord(state, questions, options, numbers, periods, days, years, counted, percent)


def value_set_question(record: ReadingRecord, fields: Mapping[str, Mapping[str, float]]) -> tuple[dict | None, list[str]]:
    """The second reading call: one choice over the sets of the values the value question's answer ranks highest (at most
    VALUE_SET_MOST, each above VALUE_SET_FLOOR): each alone, each pair, each triple, all four, and none. A question naming
    two values, or a value and a date, spreads the value slot's one choice over them; a yes-or-no question per value has
    no competing option and said yes to five to eight values of one climate table. Returns (the question or None, the
    value coordinates in order)."""
    values, mapping = fields.get("value") or {}, record.options.get("value") or {}
    criteria = record.questions.get("value", {}).get("criteria", {})
    ranked = sorted((option for option in mapping if values.get(option, 0.0) > VALUE_SET_FLOOR), key=lambda option: (-values.get(option, 0.0), option))[:VALUE_SET_MOST]
    if not ranked:
        return None, []
    sets = _value_sets(len(ranked))
    offered = {_set_key(at): " and ".join(criteria.get(ranked[i], mapping[ranked[i]]) for i in members) for at, members in enumerate(sets)}
    return {"instructions": VALUE_SET_QUESTION, "criteria": {**offered, NONE: "none of these"}}, [mapping[option] for option in ranked]


def _value_sets(n: int) -> list[tuple[int, ...]]:
    return [members for size in range(1, n + 1) for members in combinations(range(n), size)]


def _set_key(at: int) -> str:
    return f"s{chr(97 + at // 26)}{chr(97 + at % 26)}"


# ------------------------------------------------------------------------------------------ the answers, as a reading
def _log(p: float) -> float:
    return math.log(max(p, P_FLOOR))


def api_scores(scores: Mapping[str, float], record: ReadingRecord, fields: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    """The stock scores with every slot the API answered replaced by the API's term: W * (log p - log 1/n), the option's
    log odds against the uniform share; on the value slot W * (log p - log p(none)), its log odds against the question
    naming no value (a value at 1% of 160 options is above the uniform share, and lifted filtered candidates over
    unfiltered ones on questions that filter nothing). A coordinate of an answered slot the record did not offer reads
    one below every offered one."""
    out = dict(scores)
    for slot, mapping in record.options.items():
        probabilities = fields.get(slot)
        if not probabilities:
            continue
        base = _log(probabilities[NONE]) if slot == "value" and NONE in probabilities else math.log(1.0 / len(probabilities))
        terms = {name: W * (_log(probabilities.get(option, 0.0)) - base) for option, name in mapping.items()}
        floor = min(terms.values()) - 1.0
        for name in [name for name in out if name.split(":", 1)[0] == slot and name not in terms]:
            out[name] = floor
        out.update(terms)
        if slot == SLOT_SHOWN and NONE in probabilities:     # no column named: the listing of every column, against the uniform share
            out[coordinate(SLOT_LISTING, EVERY_COLUMN)] = W * (_log(probabilities[NONE]) - math.log(1.0 / len(probabilities)))
    return out


def percent_scores(scores: Mapping[str, float], keys: Sequence[str], probabilities: Mapping[str, float] | None) -> dict[str, float]:
    """The percent operator at W * (log P(percent) - log p(none)), P(percent) summed over the three formulas over 100; the
    four row operators stay as the stock reading left them. Without an answer no percent coordinate is set."""
    if not probabilities or not keys:
        return dict(scores)
    out, none = dict(scores), max(probabilities.get(NONE, 0.0), P_FLOOR)
    said = sum(probabilities.get(key, 0.0) for key in sorted(keys))
    out[coordinate(SLOT_ROW_OP, PERCENT_OP)] = round(W * (_log(said) - math.log(none)), SCORE_DIGITS)
    return out


def added_scores(scores: Mapping[str, float], mapping: Mapping[str, str], probabilities: Mapping[str, float] | None) -> dict[str, float]:
    """An added question's readings: an option the API holds likelier than none joins the reading at
    W * (log p - log p(none)). A coordinate the reading already holds keeps its score, and an option no likelier than none
    adds nothing: a number or a period decides whether a condition is built at all, so it is read against none (against
    1/n, a number at 5% of 22 options was positive on 48 of 176 questions the API read as naming none)."""
    out = dict(scores)
    if not probabilities or not mapping:
        return out
    none = max(probabilities.get(NONE, 0.0), P_FLOOR)
    for option, name in mapping.items():
        term = W * (_log(probabilities.get(option, 0.0)) - math.log(none))
        if term > 0 and name not in scores:
            out[name] = term
    return out


def value_set_scores(scores: Mapping[str, float], names: Sequence[str], probabilities: Mapping[str, float] | None,
                     linked: frozenset[tuple[str, str]] = frozenset()) -> dict[str, float]:
    """Each value the set question asked about at W * (log P(in) - log P(not in)), P(in) summed over the sets holding it,
    in place of its term against none; values not asked keep theirs. A key and the column it keys into hold the same
    values (linked: pairs of "<table>.<position>" column ids, both ways, engine/program/connected.py linked_columns): the
    API splits such a value between the two columns and each falls below one half though the question names it, so a
    value's P(in) sums the sets holding it or the same text in a column linked to its own. The same text in columns no
    key links stays two values."""
    out = dict(scores)
    if not probabilities or not names:
        return out
    sets = _value_sets(len(names))
    keys = [_set_key(at) for at in range(len(sets))]
    parts = [(name.split(":", 1)[-1].partition("=")[0], name.partition("=")[2]) for name in names]
    for i, name in enumerate(names):
        same = {j for j, (column, text) in enumerate(parts) if text == parts[i][1] and (j == i or (parts[i][0], column) in linked)}
        inside = sum(probabilities.get(key, 0.0) for key, members in zip(keys, sets) if same & set(members))
        inside = min(max(inside, P_FLOOR), 1 - P_FLOOR)
        out[name] = W * (math.log(inside) - math.log(1 - inside))
    return out


def counted_value_scores(scores: Mapping[str, float], record: ReadingRecord, fields: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    """When the count question's likeliest answer is two or more, the values the value question ranks highest, as many as
    counted and each likelier than the value slot's uniform share, are read as named at W * (log P(two or more) - log
    max(p(none), p(one))) when that is positive; a reading already higher keeps its score. Otherwise nothing changes."""
    counts, values = fields.get("value_count"), fields.get("value")
    mapping = record.options.get("value") or {}
    if not counts or not values or not mapping:
        return dict(scores)
    keys = [f"k{chr(97 + at)}" for at in range(len(VALUE_COUNTS))]
    best = max([NONE, *keys], key=lambda option: (counts.get(option, 0.0), option == NONE))
    if best in (NONE, keys[0]):
        return dict(scores)
    named = keys.index(best) + 1
    many = sum(counts.get(option, 0.0) for option in keys[1:])
    term = W * (_log(many) - _log(max(counts.get(NONE, 0.0), counts.get(keys[0], 0.0))))
    if term <= 0:
        return dict(scores)
    uniform = 1.0 / len(values)
    ranked = sorted((option for option in mapping if values.get(option, 0.0) > uniform), key=lambda option: (-values.get(option, 0.0), option))[:named]
    out = dict(scores)
    for option in ranked:
        if out.get(mapping[option], float("-inf")) < term:
            out[mapping[option]] = term
    return out


def composed_period_scores(scores: Mapping[str, float], record: ReadingRecord, fields: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    """The period the three date questions say together, as a coordinate the reading does not already hold. The named
    periods are the days read as written in words or, with none, the months and quarters the period question read. With
    no relation (or "on") each named day is a period of its own; with a relation likelier than none the period is
    everything before, up to, from or after the likeliest named one, or the range between the two likeliest, its term the
    smaller of the relation's and the named one's log odds against none."""
    day_p, relation_p, span_p = fields.get("written_day") or {}, fields.get("date_relation") or {}, fields.get("stated_period") or {}

    def term(p: Mapping[str, float], option: str) -> float:
        return W * (_log(p.get(option, 0.0)) - _log(p.get(NONE, 0.0)))

    named = [(term(day_p, option), PeriodSpan(P_DAY, day, day)) for option, day in record.days.items() if term(day_p, option) > 0]
    if not named:
        for option, name in record.periods.items():
            span = period_from_name(name.split(":", 1)[1])
            if span.kind in (P_MONTH, P_QUARTER) and span.start and term(span_p, option) > 0:
                named.append((term(span_p, option), span))
    if not named:
        return dict(scores)
    named.sort(key=lambda item: (-item[0], item[1].start))
    relation = max(DATE_RELATIONS, key=lambda r: relation_p.get(r, 0.0)) if relation_p else "on"
    held = term(relation_p, relation) if relation_p else 0.0
    added: dict[str, float] = {}
    if held <= 0 or relation == "on":
        added = {span.name: t for t, span in named if span.kind == P_DAY}
    elif relation == "between":
        if len(named) >= 2:
            first, last = sorted(named[:2], key=lambda item: item[1].start)
            added[PeriodSpan(P_RANGE, first[1].start, last[1].end or last[1].start).name] = min(held, first[0], last[0])
    else:
        t, span = named[0]
        start, end = dt.date.fromisoformat(span.start), dt.date.fromisoformat(span.end or span.start)
        one = dt.timedelta(days=1)
        first, last = {"before": (PERIOD_FIRST, (start - one).isoformat()), "until": (PERIOD_FIRST, end.isoformat()),
                       "from": (start.isoformat(), PERIOD_LAST), "after": ((end + one).isoformat(), PERIOD_LAST)}[relation]
        added[PeriodSpan(P_RANGE, first, last).name] = min(held, t)
    out = dict(scores)
    for name, t in added.items():
        if t > 0 and coordinate(SLOT_PERIOD, name) not in scores:
            out[coordinate(SLOT_PERIOD, name)] = t
    return out


def api_closed(record: ReadingRecord, fields: Mapping[str, Mapping[str, float]], closed: Mapping[str, Mapping[str, float]]) -> dict[str, dict[str, float]]:
    """The closed heads' probabilities the candidate order reads (engine/named/chooser.py), the API's where it answered the
    head: an answer type's coarse probability is shared among its fine classes; none's probability joins the class none."""
    out = {head: dict(classes) for head, classes in closed.items()}
    for head, classes in CLOSED_HEADS.items():
        probabilities, mapping = fields.get(head), record.options.get(head)
        if not probabilities or not mapping:
            continue
        said: dict[str, float] = {}
        for option, name in mapping.items():
            rest = name.split(":", 1)[1]
            said[rest] = said.get(rest, 0.0) + probabilities.get(option, 0.0)
        if head == SLOT_ANSWER_TYPE:
            share = {c: sum(coarse_type(x) == coarse_type(c) for x in classes) for c in classes}
            fine = {c: said.get(coarse_type(c), 0.0) / share[c] for c in classes}
        else:
            fine = {c: said.get(c, 0.0) for c in classes}
            if NONE in classes:
                fine[NONE] += probabilities.get(NONE, 0.0)
        total = sum(fine.values())
        if total > 0:
            out[head] = {c: round(p / total, SCORE_DIGITS) for c, p in fine.items()}
    return out


# ------------------------------------------------------------------------------------- the question's own two words
SAID_ABOVE_QUESTION = ("Does the question ask which of two outcomes of a comparison holds, its answer being one of the "
                       "question's own words for how one figure compares with another (more or less, higher or lower, "
                       "before or after, longer or shorter)? Choose the words of the question that answer it when what the "
                       "question asks about is more, higher, larger, later or longer than what it is compared with. none if "
                       "the question asks no such either-or, or if its alternatives are things or values it names rather "
                       "than how they compare.")
SAID_BELOW_QUESTION = ("Does the question ask which of two outcomes of a comparison holds, its answer being one of the "
                       "question's own words for how one figure compares with another (more or less, higher or lower, "
                       "before or after, longer or shorter)? Choose the words of the question that answer it when what the "
                       "question asks about is less, lower, smaller, earlier or shorter than what it is compared with. none "
                       "if the question asks no such either-or, or if its alternatives are things or values it names rather "
                       "than how they compare.")
SAID_SPAN_WORDS = 3          # a said word is a span of one to three of the question's words
SAID_OPTIONS_MOST = 254      # the API's 255 options, none among them


def said_spans(question: str) -> list[str]:
    """The question's own word spans of one to SAID_SPAN_WORDS words, as written (the readers' WORD pattern, case kept),
    each distinct span (folded) once, in the order it first stands; at most SAID_OPTIONS_MOST."""
    words = WORD.findall(question)
    seen, out = set(), []
    for size in range(1, SAID_SPAN_WORDS + 1):
        for at in range(len(words) - size + 1):
            span = " ".join(words[at:at + size])
            if fold(span) not in seen:
                seen.add(fold(span))
                out.append(span)
    return out[:SAID_OPTIONS_MOST]


def said_record(question: str) -> tuple[dict, dict, dict[str, str]]:
    """The said call: two closed questions over the question alone (STATE is the question; no sheet is needed to read its
    words): which of its spans answers when what it asks about is above what it is compared with, and which when below.
    Options are the spans under keys w000, w001, ... Returns (STATE, questions, {key: span}); all empty for a question
    with no word."""
    spans = said_spans(question)
    if not spans:
        return {}, {}, {}
    mapping = {f"w{at:03d}": span for at, span in enumerate(spans)}
    criteria = {**mapping, NONE: "none"}
    return ({"question": question},
            {"said_above": {"instructions": SAID_ABOVE_QUESTION, "criteria": criteria},
             "said_below": {"instructions": SAID_BELOW_QUESTION, "criteria": dict(criteria)}}, mapping)


def said_scores(scores: Mapping[str, float], mapping: Mapping[str, str], fields: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    """Each span the API holds likelier than none on a side joins the reading as said:<side>=<the span folded>, at
    W * (log p - log p(none)), as added_scores reads its questions; a coordinate already held keeps its score. The decoder
    builds a versus in the question's words only when both sides hold one (engine/program/decoder.py said_words)."""
    out = dict(scores)
    for side, name_of_field in ((SAID_ABOVE, "said_above"), (SAID_BELOW, "said_below")):
        probabilities = fields.get(name_of_field) or {}
        if not probabilities:
            continue
        none = max(probabilities.get(NONE, 0.0), P_FLOOR)
        for key, span in mapping.items():
            term = W * (_log(probabilities.get(key, 0.0)) - math.log(none))
            name = coordinate(SLOT_SAID, side, fold(span))
            if term > 0 and name not in scores:
                out[name] = max(out.get(name, 0.0), round(term, SCORE_DIGITS))
    return out


def fused_reading(named: NamedReading, record: ReadingRecord, fields: Mapping[str, Mapping[str, float]],
                  set_names: Sequence[str] = (), set_probabilities: Mapping[str, float] | None = None,
                  said_words: Mapping[str, str] | None = None, said_fields: Mapping[str, Mapping[str, float]] | None = None,
                  linked: frozenset[tuple[str, str]] = frozenset(), order_fields: Mapping[str, Mapping[str, float]] | None = None) -> NamedReading:
    """The stock reading with the API's answers in it, applied in order: the slots' terms, the percent operator, the
    stated number, period and year, the value set (a value read as one across the columns a key links), the counted
    values, the composed period, the question's own two words, the end of the table it takes its lines from; and the
    closed heads."""
    scores = api_scores(named.reading.scores, record, fields)
    scores = percent_scores(scores, record.percent, fields.get("row_formula"))
    scores = added_scores(scores, record.numbers, fields.get("stated_number"))
    scores = added_scores(scores, record.periods, fields.get("stated_period"))
    scores = added_scores(scores, record.years, fields.get("stated_year"))
    scores = value_set_scores(scores, set_names, set_probabilities, linked)
    if record.counted:
        scores = counted_value_scores(scores, record, fields)
    scores = composed_period_scores(scores, record, fields)
    if said_words and said_fields:
        scores = said_scores(scores, said_words, said_fields)
    if order_fields:
        scores = order_scores(scores, order_fields)
    reading = Reading(scores)
    read = replace(named.read, closed=api_closed(record, fields, named.read.closed))
    return replace(named, read=read, reading=reading, reach=table_reach(reading))


# --------------------------------------------------------------------------------------------------- the order call
ORDER_QUESTION = ("Does the question take rows by where they stand in the table as it is listed, rather than by a value "
                  "they hold: its first rows, or its last rows?")
ORDER_OPTIONS = {"o00": "its first rows, as the table lists them", "o01": "its last rows, as the table lists them"}
ORDER_ENDS_OF = {"o00": FIRST, "o01": LAST}
RUN_QUESTION = ("Does the question ask how many rows stand in an unbroken run, one right after another as the table lists "
                "them, all meeting what it says, rather than how many there are in all?")
RUN_OPTIONS = {"r00": "the most rows in an unbroken run, one right after another"}
FIGURE_QUESTION = ("Does the question ask for the median of a set of numbers (their middle value), or for their standard "
                   "deviation (how spread out they are), rather than a total, an average, a count, a lowest or a highest?")
FIGURE_OPTIONS = {"f00": "their median: the middle value", "f01": "their standard deviation"}
FIGURES_OF = {"f00": MEDIAN, "f01": DEVIATION}
RANGE_QUESTION = ("Does the question keep the rows whose number lies between two numbers it states, a range with a lower "
                  "and an upper end, rather than above or below one number?")
RANGE_OPTIONS = {"b00": "between two numbers the question states, both ends included"}


def order_record(question: str) -> tuple[dict, dict]:
    """The order call: closed questions over the question alone (no sheet is needed to read them): whether it takes
    the first or the last lines of a table as the sheet lists them ("the first venue", "the last 3 entries"), or
    neither; whether it counts the most lines in a row ("how many consecutive wins"), or not; whether it asks for
    a median or a standard deviation, figures the reading call's aggregate question does not offer; and whether it
    keeps rows whose number lies between two numbers it states, a comparison that question does not offer. It is a call of
    its own, so the reading call's body is the same whether it is asked or not; the endpoint answers each question of
    a call by itself (a question added here moved no other answer on 40 questions, 2026-11-26)."""
    return {"question": question}, {"line_order": {"instructions": ORDER_QUESTION, "criteria": {**ORDER_OPTIONS, NONE: "neither"}},
                                    "line_run": {"instructions": RUN_QUESTION, "criteria": {**RUN_OPTIONS, NONE: "neither"}},
                                    "series_figure": {"instructions": FIGURE_QUESTION, "criteria": {**FIGURE_OPTIONS, NONE: "neither"}},
                                    "number_range": {"instructions": RANGE_QUESTION, "criteria": {**RANGE_OPTIONS, NONE: "no"}}}


def order_scores(scores: Mapping[str, float], fields: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    """Each end the API holds likelier than neither joins the reading as order:first or order:last, and a run it holds
    likelier than neither as order:run, at W * (log p - log p(none)), as said_scores reads its spans; the decoder ranks
    lines by where they stand, or counts the most in a row, only where one is set (engine/program/decoder.py
    table_frames). A median or a standard deviation it holds likelier than neither joins the same way as
    aggregate:median or aggregate:stdev, and a range between two stated numbers as comparison:between, each of which
    the decoder offers only where set."""
    out = dict(scores)
    for field, slot, read in (("line_order", SLOT_ORDER, ORDER_ENDS_OF), ("line_run", SLOT_ORDER, {"r00": RUN}),
                              ("series_figure", SLOT_AGGREGATE, FIGURES_OF), ("number_range", SLOT_COMPARISON, {"b00": BETWEEN})):
        probabilities = fields.get(field) or {}
        if not probabilities:
            continue
        none = max(probabilities.get(NONE, 0.0), P_FLOOR)
        for key, value in read.items():
            term = W * (_log(probabilities.get(key, 0.0)) - math.log(none))
            if term > 0:
                out[coordinate(slot, value)] = round(term, SCORE_DIGITS)
    return out


# ------------------------------------------------------------------------------------------------ the convert record
CONVERT_INSTRUCTIONS = ("STATE describes the person's spreadsheet and their question. The sheet holds amounts of money, each row in its own currency. "
                        "Does the question ask for its answer expressed in one currency, whatever currencies the rows are in? Choose that currency. "
                        "Choose none when the question names a currency only to pick rows, or names no currency.")
CONVERT_NONE = "no conversion"
# The question's likeliest currency is offered from this probability: its converted columns join the catalog, and the
# reading and the choice decide between an amount as held and the amount converted.
CONVERT_OFFER = 0.1


def convert_record(catalog: Catalog, question: str, targets: Sequence[str], active_sheet: str | None = None) -> tuple[dict, dict, dict[str, str]]:
    """The convert question: which of ``targets`` (currency codes, options o00, o01, ...) the question wants its answer
    in, or none. Returns (STATE, questions, criteria)."""
    criteria = {f"o{at:02d}": code for at, code in enumerate(targets)}
    criteria[NONE] = CONVERT_NONE
    return sheet_state(catalog, question, active_sheet), {"convert": {"instructions": CONVERT_INSTRUCTIONS, "criteria": criteria}}, criteria


def convert_choice(probabilities: Mapping[str, float], criteria: Mapping[str, str]) -> str | None:
    """The likeliest currency (the first option among equals) when its probability reaches CONVERT_OFFER, else None."""
    best = max(sorted(key for key in probabilities if key != NONE), key=probabilities.__getitem__)
    return criteria[best] if probabilities[best] >= CONVERT_OFFER else None


# -------------------------------------------------------------------------------------------------- the choice record
CHOICE_INSTRUCTION = ("STATE describes the person's spreadsheet. Each option is one reading of the question as a computation "
                      "over it, listed in the order the program decoder ranked them, followed by the answer that computation "
                      "returns. Choose the reading whose answer answers the question exactly as asked, keeping every filter, "
                      "period and grouping it states. Choose none when no reading does.")
CHOICE_NONE = "None of these readings answers the question as asked."
READ_AS_CHARS = 300
PREVIEW_ROWS, PREVIEW_CELL, PREVIEW_CHARS = 3, 40, 160


def preview(rows: Sequence[Sequence]) -> str:
    """An answer as the choice shows it: its first rows' cells, clipped, and how many rows follow."""
    rows = list(rows)
    text = " | ".join(", ".join(str(cell)[:PREVIEW_CELL] for cell in row) for row in rows[:PREVIEW_ROWS])
    if len(rows) > PREVIEW_ROWS:
        text += f" (and {len(rows) - PREVIEW_ROWS} more rows)"
    return text[:PREVIEW_CHARS]


def choice_record(catalog: Catalog, question: str, listed: Sequence[tuple[str, Sequence[Sequence]]], active_sheet: str | None = None) -> tuple[dict, dict]:
    """(STATE, questions) of the choice over listed candidates, each (read-as sentence, rows), in decoder order: option
    r00 is the first, at most CHOOSE_K, and none. active_sheet: the client's tab in view (sheet_state)."""
    criteria = {f"r{at:02d}": f"{read_as[:READ_AS_CHARS]} -> answer: {preview(rows)}" for at, (read_as, rows) in enumerate(listed[:CHOOSE_K])}
    criteria[NONE] = CHOICE_NONE
    return sheet_state(catalog, question, active_sheet), {"reading": {"instructions": CHOICE_INSTRUCTION, "criteria": criteria}}


def judge_options(question: Mapping) -> list[tuple[str, str]]:
    """The choice question's options as the second judge reads them (engine/llm.py judge): each key and its text, in
    the decoder's order, none last."""
    criteria = question["criteria"]
    return [(key, criteria[key]) for key in criteria if key != NONE] + ([(NONE, criteria[NONE])] if NONE in criteria else [])


def answer_key(rows: Sequence[Sequence]) -> str:
    """An answer's identity: candidates returning the same rows give the same answer."""
    return hashlib.sha1(json.dumps([list(row) for row in rows], default=str, ensure_ascii=False).encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


@dataclass(frozen=True)
class AnswerChoice:
    """The decision over the choice's probabilities. pick: the listed candidate served for the likeliest answer, of the
    listings returning it the one the choice gave the most probability (the first among equals), so the read-as served
    is the reading the choice preferred; runner_up: the first listing of the next likeliest answer; mass: the likeliest
    answer's probability, summed over the candidates returning it; alike: the listing of another answer that reads word
    for word like a reading of this one (in the sheet's header words), or None; doubt: what the second judge said of an
    answer the choice alone would give unmarked (DOUBT_ANOTHER: it picked another answer, then the runner-up;
    DOUBT_NONE: it picked none; DOUBT_UNREAD: it did not answer), or None."""
    kind: str
    pick: int | None
    runner_up: int | None
    mass: float | None
    none: float
    alike: int | None = None
    doubt: str | None = None


def answer_masses(probabilities: Mapping[str, float] | None, answers: Sequence[str]) -> tuple[dict[str, float], dict[str, int]]:
    """(each answer's probability, summed over the listed candidates returning it; each answer's first listing) from the
    choice's probabilities, for the answers the choice offered (answers[i] is listing i's answer_key)."""
    mass: dict[str, float] = {}
    first: dict[str, int] = {}
    for key in sorted(key for key in (probabilities or {}) if key != NONE):
        answer = answers[int(key[1:])]
        mass[answer] = mass.get(answer, 0.0) + probabilities[key]
        first.setdefault(answer, int(key[1:]))
    return mass, first


def choose_answer(probabilities: Mapping[str, float] | None, answers: Sequence[str], lines: Sequence[str] = (),
                  judged: str | None = None) -> AnswerChoice:
    """Per answer, not per candidate: the API spreads its probability over readings that return the same answer (a right
    answer at ranks 0, 5, 9 and 12 read 0.33, 0.28 and 0.13 on three of them), so a candidate's probability is summed over
    the listed candidates whose answer (answers[i], answer_key) is the same (answer_masses). The likeliest answer, ties to
    the earlier listing, is most likely wrong (ERRED) when none is likelier or it is below T_ERR, warned below T_FLAG,
    and answered otherwise; it is served in every case, and ERRED with no answer when nothing was listed. It is warned
    too when a reading of it reads, word for word (``lines``: each listing's read-as in the sheet's own header words,
    output.in_header_words), like a reading the choice was offered of another answer (``alike``): "total Amount" on a
    sheet with two columns headed Amount read "the total of Amount" for both, 40 and 60, and the question cannot tell
    which it means; the served reading names its column by the sheet's letter ("Amount [C]").

    Two judges that must agree (owner, 2026-11-26): ``judged`` is the key a second judge picked for the same question
    (engine/llm.py judge; UNJUDGED when its call failed; None when it is not asked, as before the choice is known). An
    answer the choice would give unmarked stays unmarked only where the judge picked a listing of the same answer;
    another answer (then the runner-up), none, or no answer warns it (``doubt``). And an answer the choice warns only
    because its own probability is low (below T_FLAG, no reading alike) is given unmarked where the judge picked it
    too: the two agree, which the choice's probability alone could not say (the 896: 33 such answers right, 7 wrong;
    spider/results/RESULTS.md, 2026-11-27). The answer served is the choice's in every case; an answer with a reading
    alike, and a most-likely-wrong one, are as the choice left them. On unseen tables the unmarked wrong fell from
    13.6% and 12.8% to 6.7% and 5.8% (2026-11-26)."""
    probabilities = probabilities or {}
    none = probabilities.get(NONE, 0.0)
    mass, first = answer_masses(probabilities, answers)
    if not mass:
        return AnswerChoice(ERRED, None, None, None, none)
    ranked = sorted(mass, key=lambda answer: (-mass[answer], first[answer]))
    best = ranked[0]
    runner_up = first[ranked[1]] if len(ranked) > 1 else None
    offered = [(answer, lines[at]) for at, answer in enumerate(answers[:CHOOSE_K]) if at < len(lines)]
    worded = {line for answer, line in offered if answer == best}
    alike = next((at for at, (answer, line) in enumerate(offered) if answer != best and line in worded), None)
    if none > mass[best] or mass[best] < T_ERR:
        kind = ERRED
    elif alike is not None or mass[best] < T_FLAG:
        kind = WARNED
    else:
        kind = ANSWERED
    served = min((key for key in probabilities if key != NONE and answers[int(key[1:])] == best), key=lambda key: (-probabilities[key], int(key[1:])))
    other, doubt = alike if alike is not None else runner_up, None
    if judged is not None and alike is None and kind != ERRED:
        agreed = judged not in (UNJUDGED, NONE) and answers[int(judged[1:])] == best
        if kind == WARNED:
            kind = ANSWERED if agreed else WARNED
        elif not agreed:
            kind = WARNED
            if judged == UNJUDGED:
                doubt = DOUBT_UNREAD
            elif judged == NONE:
                doubt = DOUBT_NONE
            else:
                other, doubt = int(judged[1:]), DOUBT_ANOTHER
    return AnswerChoice(kind, int(served[1:]), other, mass[best], none, alike, doubt)


def awaits_judge(choice: AnswerChoice) -> bool:
    """Whether an answer's mark depends on the second judge (choose_answer): one the choice would give unmarked, or
    warned only because its own probability is low. An answer with a reading alike stays warned and a
    most-likely-wrong one is retried whatever the judge says, so neither waits for it."""
    return choice.alike is None and choice.kind in (ANSWERED, WARNED)


__all__ = ["ORDER_QUESTION", "order_record", "order_scores", "ANSWERED", "CHOOSE_K", "DOUBT_ANOTHER", "DOUBT_NONE", "DOUBT_UNREAD", "ERRED", "T_ERR", "T_FLAG", "UNJUDGED", "W", "WARNED",
           "AnswerChoice", "ReadingRecord", "answer_key", "answer_masses", "awaits_judge", "judge_options",
           "choice_record", "choose_answer", "convert_choice", "convert_record", "fused_reading", "reading_record", "said_record", "sheet_state",
           "value_set_question", "value_set_scores"]
