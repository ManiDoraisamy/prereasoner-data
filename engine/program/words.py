"""A reading of the question from the sheet's own text and the question's digits. No word of any language is written here.

What it fills, from what holds in any language:
- a column, a stored value, a contained phrase, a printed line's label, a band, a table, a group of peer columns: where
  the question's own characters hold the sheet's own text (whole words of any script, case aside), scored by how much
  of that text the question covers; a question word one or two edits from a word of the sheet reads as that word;
- numbers and times the question writes in digits (signs, currency marks, the percent sign, grouping);
- a day, a month or a year the question writes in digits (2019, 2019-04, 4/30/2019).
How common a word is, is read from the sheet itself: a word most of a table's names or values hold says little.
What a question means beyond its digits and the sheet's own words (its form, its aggregate, a comparison, a negation, a
period written in words, a synonym) is the learned readers' to read; this reading is their floor and the decoder's test
harness. Scores are in 0..1.
"""
from __future__ import annotations

import re

from engine.program.catalog import Catalog
from engine.program.decoder import Decoder
from engine.program.index import TableIndex, words_of
from engine.program.language import Col, PeriodSpan, TOGETHER, peers_name
from engine.program.names import (SLOT_CALC_LEFT, SLOT_CONDITIONS, SLOT_CONTAINS, SLOT_FILTER, SLOT_KEY, SLOT_LINE, SLOT_MEASURE, SLOT_NUMBER, SLOT_PEERS, SLOT_PERIOD,
                                  SLOT_SECTION, SLOT_SHOWN, SLOT_TABLE, SLOT_VALUE, coordinate)
from engine.program.scoring import Reading
from engine.program.semantics import DURATION, duration_seconds, exact_wins, fold, number_text, stated_number
from engine.sheet.names import P_DAY, P_MONTH, P_RANGE, P_YEAR
from engine.sheet.values import parse_period

_TOKEN = re.compile(r"[^\W_]+")      # a word of any script, as index.words_of reads the sheet's words
_YEAR = re.compile(r"(?<![\w.,/:-])((?:19|20)\d{2})(?![\w.,/:-])")
_DATE_FORMS = re.compile(r"(?<![\w/.-])(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2})|\d{4}[-/]\d{1,2}|\d{1,2}/\d{4})(?![\w/.-])")
_NUMBER = re.compile(r"(?<![\w/.-])[$€£¥]?\d[\d,]*(?:\.\d+)?\s*%?")
_TIME_STATED = re.compile(r"(?<![\w.:])" + DURATION.pattern + r"(?![\w:])")

# A column's name in a question says little about a condition on it until a value is read there too.
FILTER_NAME_SHARE = 0.2
# A word only one column's name holds says which column it means, however long the name.
ONLY_NAME_WITH_WORD = 0.6
# A number the question states is weak evidence of a condition until a reader says which.
NUMBER_ALONE = 0.3
# Each column shown beside the first costs this: most questions ask for one thing, and a word that asks for two reads the second's link higher than this.
SHOWN_MORE = 2.5
# A number the question states that a number column holds as it stands.
STATED_NUMBER_HELD = 0.8
# Of two named columns under - or /, the one named first reads as the left: a word order, breaking what would otherwise be a tie.
LEFT_BY_WORD_ORDER = 0.05
# A condition beyond what the question's words can read costs this; one they read and the program leaves out, this.
EXTRA_CONDITION = 1.0
UNREAD_CONDITION = 0.3
# A word held by more than this share of a table's distinct values (and by more than COMMON_FEWEST of them), or by at least
# this share of its column names (and by COMMON_FEWEST), is common in that table and alone reads nothing.
COMMON_SHARE = 0.2
COMMON_FEWEST = 3


def _periods(low: str, out: dict[str, float]) -> None:
    """Days, months and years the question writes in digits."""
    for match in _DATE_FORMS.finditer(low):
        period = parse_period(match.group(0))
        if period is not None and period.start is not None:
            _span_score(period, out, 1.0)
    for match in _YEAR.finditer(low):
        out.setdefault(coordinate(SLOT_PERIOD, match.group(1)), 0.9)


def _span_score(period, out: dict[str, float], score: float) -> None:
    kind = period.kind if period.kind in (P_DAY, P_MONTH, P_YEAR) else P_RANGE
    out[coordinate(SLOT_PERIOD, PeriodSpan(kind, period.start, period.end).name)] = score


def _numbers(low: str, out: dict[str, float]) -> None:
    """Numbers and times the question writes in digits. A date's digits and a year state no amount."""
    taken = [match.span() for match in _DATE_FORMS.finditer(low)] + [match.span() for match in _YEAR.finditer(low)]
    for match in _TIME_STATED.finditer(low):     # "35:00": a time, read as a sheet's time cells are
        taken.append(match.span())
        out[coordinate(SLOT_NUMBER, number_text(duration_seconds(match.group(0))))] = NUMBER_ALONE
    for match in _NUMBER.finditer(low):
        if any(start <= match.start() < end for start, end in taken):
            continue
        text = match.group(0).strip()
        value = stated_number(text)
        if value is None:
            continue
        out[coordinate(SLOT_NUMBER, number_text(value))] = NUMBER_ALONE
        if text.endswith("%"):
            out[coordinate(SLOT_NUMBER, number_text(value * 100))] = NUMBER_ALONE / 2


def _common_values(index: TableIndex) -> frozenset[str]:
    distinct = sum(len(lines) for lines in index.value_lines.values())
    most = max(COMMON_FEWEST, COMMON_SHARE * distinct)
    return frozenset(word for word, held in index.value_words.items() if len(held) > most)


def _common_names(index: TableIndex) -> frozenset[str]:
    most = max(COMMON_FEWEST, COMMON_SHARE * len(index.name_words))
    return frozenset(word for word, count in index.name_counts.items() if count >= most)


def _phrases(words: list[str], common: frozenset[str]) -> list[tuple[str, ...]]:
    out = []
    for size in (4, 3, 2, 1):
        for at in range(len(words) - size + 1):
            phrase = tuple(words[at:at + size])
            if phrase[0] not in common and phrase[-1] not in common:
                out.append(phrase)
    return out


def _is_word_character(character: str) -> bool:
    """What a regular expression's \\w matches in a str: a letter or digit of any script, or the underscore."""
    return character.isalnum() or character == "_"


def stands_whole(text: str, held: str) -> bool:
    """held stands in text as a whole: at some place where no word character touches it on either side. The same answer
    as searching (?<!\\w)held(?!\\w), without building and compiling a pattern for each of a table's values: on a table of
    20,776 lines that took 47 of a question's 54 seconds (31,314 patterns past the regular expressions' cache)."""
    at = text.find(held)
    while at != -1:
        end = at + len(held)
        if (at == 0 or not _is_word_character(text[at - 1])) and (end == len(text) or not _is_word_character(text[end])):
            return True
        at = text.find(held, at + 1)
    return False


def _covered(text: str, question_words: set[str]) -> float:
    """How much of a label the question's words cover: a whole word counts 1, a label word a question word begins (Transco, Transcontinental) counts half."""
    words = words_of(text)
    if not words:
        return 0.0
    total = 0.0
    for word in words:
        if word in question_words:
            total += 1.0
        elif any(len(asked) >= 4 and word.startswith(asked) for asked in question_words):
            total += 0.5
    return total / len(words)


def _table_words(table_id: str, index: TableIndex, question_words: set[str], raw_words: list[str], low: str, out: dict[str, float], *, periods: bool, numbers: bool) -> None:
    """The sheet's own words in the question, on one table: names, then values, then printed labels and bands."""
    table = index.table
    title = index.title_words
    spelled = [index.spelled_as(word) for word in question_words if word not in index.name_counts and word not in index.value_words]
    if any(spelled):                           # a misspelled word reads as the word of this sheet it means
        question_words = question_words | {word for word in spelled if word}
    common_names, common_values = _common_names(index), _common_values(index)
    if title:
        hits = (title & question_words)
        if hits:
            out[coordinate(SLOT_TABLE, table_id)] = len(hits) / max(3, len(title))
    for position, name in index.name_words.items():
        telling = name - common_names or name
        held = telling & question_words
        if not held:
            continue
        share = len(held) / len(telling)
        if any(index.name_counts[word] == 1 for word in held):
            share = max(share, ONLY_NAME_WITH_WORD)
        col = Col(table_id, position).name
        for slot in (SLOT_MEASURE, SLOT_KEY, SLOT_SHOWN):
            out[coordinate(slot, col)] = share
        out[coordinate(SLOT_FILTER, col)] = share * FILTER_NAME_SHARE
    named = [(min((raw_words.index(word) for word in raw_words if word in name), default=len(raw_words)), position) for position, name in index.name_words.items() if (name - common_names) & question_words]
    for place, (_, position) in enumerate(sorted(named, key=lambda item: item[0])):
        if place == 0 and len(named) >= 2:
            out[coordinate(SLOT_CALC_LEFT, coordinate(SLOT_MEASURE, Col(table_id, position).name))] = LEFT_BY_WORD_ORDER
    # a number the question states, standing in a number column: a key such as an id or a year may be asked for as itself
    stated = [name.split(":", 1)[1] for name in out if name.startswith(f"{SLOT_NUMBER}:")]
    for text in stated:
        value = float(text)
        for position in index.number_columns:
            if value and any(number == value for number in table.numbers[position]):
                out[coordinate(SLOT_VALUE, Col(table_id, position).name, number_text(value))] = STATED_NUMBER_HELD
    # an axis: its members' names in the question
    for axis in table.table.axes:
        names = [index.name_words.get(position, frozenset()) for position in axis.positions]
        share = sum(bool((name - common_names) & question_words) for name in names) / max(1, len(names))
        if share:
            out[coordinate(SLOT_PEERS, table_id, peers_name(tuple(axis.positions)))] = round(share, 4)
    # values: every value holding a telling question word, by how much of it the question covers; a whole value always reads
    seen: set[tuple[int, str]] = set()
    value_words = index.value_words
    for word in question_words:
        for position, folded in value_words.get(word, ()):
            seen.add((position, folded))
    for position, folded in seen:
        text = words_of(folded)
        whole = stands_whole(low, folded)
        telling = [word for word in text if word not in common_values]
        if not whole and not any(word in question_words for word in telling):
            continue
        share = sum(word in question_words for word in telling) / len(telling) if telling else 0.0
        out[coordinate(SLOT_VALUE, Col(table_id, position).name, folded)] = 1.0 if whole else 0.8 * share
    # a phrase inside longer cells: the partial match, whole words first, then a word's prefix
    phrases = _phrases(raw_words, common_values)
    for position, lines in index.value_lines.items():
        if table.columns[position].kind == "number":
            continue
        col = Col(table_id, position).name
        for phrase in phrases:
            if not all(word in value_words for word in phrase):
                continue
            text = " ".join(phrase)
            holders = {f for p, f in value_words[phrase[0]] if p == position}
            if any(text in folded for folded in holders) and not exact_wins(text, lines):
                out[coordinate(SLOT_CONTAINS, col, text)] = min(1.0, 0.5 + 0.1 * len(phrase))
    for word in set(raw_words) - common_values:
        if len(word) < 3:
            continue
        for longer in index.words_with_prefix(word):
            if longer == word:
                continue
            for position, folded in value_words[longer]:
                name = coordinate(SLOT_CONTAINS, Col(table_id, position).name, word)
                if word in folded and not exact_wins(word, index.value_lines[position]):
                    out.setdefault(name, 0.4)
    # printed labels and bands
    for label in index.printed_labels:
        if label:
            share = _covered(label, question_words)
            if share:
                out[coordinate(SLOT_LINE, table_id, label)] = share
    for band in index.bands:
        share = _covered(band, question_words)
        if share:
            out[coordinate(SLOT_SECTION, table_id, band)] = share
    # how many conditions this table can read from the question: a program with more says what was not asked, with fewer leaves words unread
    prefixes = (f"{SLOT_VALUE}:{table_id}.", f"{SLOT_CONTAINS}:{table_id}.")
    columns = {name.split(".", 1)[1].split("=", 1)[0] for name, score in out.items() if name.startswith(prefixes) and score >= 0.8}
    asked = len(columns) + (1 if periods and index.date_columns else 0) + (1 if numbers and index.number_columns else 0)
    asked += 1 if any(name.startswith((f"{SLOT_LINE}:{table_id}=", f"{SLOT_SECTION}:{table_id}=")) and score >= 0.8 for name, score in out.items()) else 0
    for count in range(7):
        out[coordinate(SLOT_CONDITIONS, table_id, count)] = -EXTRA_CONDITION * max(0, count - asked) - UNREAD_CONDITION * max(0, asked - count)


def read_question(question: str, catalog: Catalog, decoder: Decoder) -> Reading:
    """The question's reading from its digits and the sheet's own words."""
    low = fold(question)
    raw_words = _TOKEN.findall(low)
    question_words = set(raw_words)
    out: dict[str, float] = {coordinate(SLOT_SHOWN, TOGETHER, count): -SHOWN_MORE * (count - 1) for count in (2, 3)}
    _periods(low, out)
    _numbers(low, out)
    periods = any(name.startswith(f"{SLOT_PERIOD}:") for name in out)
    numbers = any(name.startswith(f"{SLOT_NUMBER}:") for name in out)
    for table in catalog.tables:
        if table.lines:
            _table_words(table.id, decoder.indexes[table.id], question_words, raw_words, low, out, periods=periods, numbers=numbers)
    return Reading(out)


__all__ = ["read_question"]
