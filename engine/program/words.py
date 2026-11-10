"""A reading from the question's words alone: word index matches over names, values, labels and bands, parsed periods and numbers, and closed word lists for the closed heads. No model.

This fills the same coordinates the encoders will fill, so it is both the floor they must beat and what makes the decoder testable without them. Scores are in 0..1 and mean only "how much of this name the question's words cover". The closed lists hold generic
words of the language itself (how many, average, highest, difference), never a word of one question or one sheet.
"""
from __future__ import annotations

import re

from engine.program.catalog import Catalog
from engine.program.decoder import Decoder
from engine.program.index import TableIndex, stem, words_of
from engine.program.language import Col, PeriodSpan, TOGETHER, peers_name
from engine.program.names import (A_CODE, A_COMPANY, A_COUNT, A_DATE, A_LIST, A_MONEY, A_NUMBER, A_PERCENT, A_PERSON, A_PLACE, A_QUANTITY, AGG, AVG, BY_SIZE, CALC, COUNT, COUNT_DISTINCT, EACH_BY_SIZE, ENTITY_GROUP, ENTITY_LINE, ENTITY_PEER, GE, GT, HIGHEST, LE, LOWEST, LT, MAX, MIN, MINUS, NE, OVER, PERIOD_NAMED, PERIOD_REST_OF, PLUS, RANK, SHOW,
                                  SLOT_AGGREGATE, SLOT_ANSWER_TYPE, SLOT_BLANK, SLOT_CALC_LEFT, SLOT_CALC_OP, SLOT_COMPARISON, SLOT_CONDITIONS, SLOT_CONTAINS, SLOT_DIRECTION, SLOT_ENTITY, SLOT_FILTER, SLOT_FORM, SLOT_KEY, SLOT_LINE, SLOT_MEASURE, SLOT_NUMBER, SLOT_PEERS, SLOT_PERIOD, SLOT_ROW_OP,
                                  SLOT_SECTION, SLOT_SHOWN, SLOT_SIZE, SLOT_TABLE, SLOT_VALUE, STATED_BY_SIZE, SUM, TIMES, coarse_type, coordinate)
from engine.program.scoring import Reading
from engine.program.semantics import DURATION, duration_seconds, exact_wins, fold, number_text, read_period, read_sign, sign_word_of, stated_number
from engine.sheet.names import AXIS_FLAG, P_DAY, P_MONTH, P_QUARTER, P_RANGE, P_YEAR
from engine.sheet.values import parse_period

_STOP = frozenset(stem(word) for word in """a about across after all also am an and any are as at be been by can did do does each for from get give got had has have how i in into is it its made many me more most much my of on one or our out over per s show so tell than that the
their them there these they this to total under us was we were what when where which who whose will with would you""".split())
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_WORD = re.compile(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b")
_YEAR_WORD = re.compile(r"\b((?:19|20)\d{2})\b")
_DATE_FORMS = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}-[a-z]{3}-\d{2,4}\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4}\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[ '-]+\d{2,4}\b|\b[1-4]q\d{2,4}\b|\bq[1-4][ '-]*\d{2,4}\b|\bcalendar \d{4}\b")
_NUMBER = re.compile(r"(?<![\w/.-])[$]?\d[\d,]*(?:\.\d+)?\s*(?:%|percent\b|k\b|m\b|mm\b)?")
# An ordinal ("3rd place", "the 2nd highest") names a position, not an amount or how many: read as a number it made "the
# last year they came in 3rd" rank the 3 latest years (WikiTableQuestions dev, 2026-11-10). A cell holding it is a value.
_ORDINAL_AFTER = re.compile(r"(?:st|nd|rd|th)\b")
# A time a question states ("35:00", "2:05:31"), read by semantics.duration_seconds as the sheet's time cells are; a clock
# time with am or pm is not one.
_TIME_STATED = re.compile(r"(?<![\w.:])" + DURATION.pattern + r"(?![\w:])(?!\s*[ap]\.?m\b)")
# A number written as a word states it as its figure does ("eight or more goals"). "one" is a number only right after a
# comparison ("more than one", "at least one"): "which one", "one of the" name no number.
_NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
                 "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b(?!-)")
_COMPARED_BEFORE = re.compile(r"\b(?:more than|less than|fewer than|at least|at most|over|under|above|below|exactly)\s+$")
_COMPARE = ((GE, re.compile(r"\b(at least|or more|or better|or higher|or above|no less than|minimum of)\b")), (LE, re.compile(r"\b(at most|or less|or lower|or below|no more than|maximum of|up to)\b")),
            (GT, re.compile(r"\b(above|over|greater than|more than|exceeds?|exceeding|higher than)\b")), (LT, re.compile(r"\b(below|under|less than|lower than|fewer than)\b")), (NE, re.compile(r"\b(other than|excluding|except)\b")))
_SIGNED_ZERO = ((GT, re.compile(r"\b(positive|above zero|greater than zero|in the money|gains?)\b")), (LT, re.compile(r"\b(negative|below zero|less than zero|out of the money|losses)\b")), (NE, re.compile(r"\b(non-?zero|not zero)\b")))
_HIGH = re.compile(r"\b(most|highest|largest|biggest|top|peak|max(?:imum)?|greatest|best|busiest|furthest|latest|last|heaviest|longest)\b")
_LOW = re.compile(r"\b(least|lowest|smallest|min(?:imum)?|fewest|worst|earliest|first|shortest)\b")
_SUM = re.compile(r"\b(total|sum|altogether|combined|overall|net|aggregate|cumulative|how much)\b")
_AVG = re.compile(r"\b(average|avg|mean)\b")
_COUNT = re.compile(r"\b(how many|number of|count of|count)\b")
_DISTINCT = re.compile(r"\b(distinct|different|unique)\b")
_CALC_MINUS = re.compile(r"\b(difference|differ|net of|less|minus|over budget|versus|vs|spread|gap|more than|higher than|above|compared)\b")
_CALC_OVER = re.compile(r"\b(ratio|share|percent|percentage|fraction|portion|per|weighted|average price|margin|what part)\b|%")
_CALC_PLUS = re.compile(r"\b(plus|together|combined|added|sum of)\b")
_ROW_TIMES = re.compile(r"\b(weighted|times|multiplied|value of|expected)\b")
_ROW_MINUS = re.compile(r"\b(less|minus|above|over|difference|net|after)\b")
_ROW_PLUS = re.compile(r"\b(plus|and|together|combined|both|across)\b")
_ACROSS = re.compile(r"\b(across|every|all the|each of the)\b")
_PLACE_NOUNS = r"city|state|country|region|location|place|site|hub|plant|zone|office|point"
_MONEY_WORDS = re.compile(r"\b(dollars?|cost|costs|price|prices|paid|pay|revenue|expense|expenses|budget|amount|fee|fees|charge|charges|invoice|invoiced|salary|worth|spend|spent)\b|\$")
_BLANK = re.compile(r"\b(no|missing|blank|empty|without|unassigned|lack|lacking)\b")
_UNIT_WORDS = r"mw|mwh|barrels?|bbls?|mmbtu|mmbtus|dth|dths|mcf|dollars?|tons?|gallons?|kwh|megawatts?"
_COMPANY_NOUNS = r"counterpart(?:y|ies|y's)|compan(?:y|ies)|shippers?|customers?|producers?|suppliers?|vendors?|entit(?:y|ies)|owners?|utilit(?:y|ies)|banks?"
_TEXT_NOUNS = r"name|description|agreement|type|status|driver|code|title|address|location"
_DATE_NOUNS = r"day|date|month|hour|year|quarter|week"
_SUPERLATIVE = r"most|biggest|largest|highest|lowest|smallest|least|peak|top|furthest|busiest"


# A column's name in a question says little about a condition on it until a value is read there too.
FILTER_NAME_SHARE = 0.2
# A word only one column's name holds says which column it means, however long the name.
ONLY_NAME_WITH_WORD = 0.6
# A number with no comparison word near it ("500 kV") is weak evidence of a comparison.
NUMBER_ALONE = 0.3
# A rank with no word asking for one reads below any other form; arithmetic with no word for it, below its one column.
NO_RANK_WORD = -1.0
NO_ROW_WORD = -2.0
NO_CALC_WORD = -2.0
# Row arithmetic names two measures where a plain measure names one: its operator costs what the second name would add.
ROW_WORD = -1.0
# Each column shown beside the first costs this: most questions ask for one thing, and a word that asks for two reads the second's link higher than this.
SHOWN_MORE = 2.5
# A number the question states that a number column holds as it stands.
STATED_NUMBER_HELD = 0.8
# Of two named columns under - or /, the one named first reads as the left: a word order, breaking what would otherwise be a tie.
LEFT_BY_WORD_ORDER = 0.05
# A condition beyond what the question's words can read costs this; one they read and the program leaves out, this.
EXTRA_CONDITION = 1.0
UNREAD_CONDITION = 0.3


def predict_answer_type(question: str) -> str:
    """The answer's type from the question's words, by design/answer_location.txt's rule: 177 of 186 on the questions it was written after (optimistic), 90 and 106 of 129 on rewordings."""
    t = question.lower().replace("’", "'").strip()
    if re.search(r"\bhow many\b", t):
        return A_QUANTITY if re.search(rf"\bhow many ({_UNIT_WORDS})\b", t) else A_COUNT
    if re.search(r"\b(what share|share of|percent|percentage|fraction|portion)\b|%", t):
        return A_PERCENT
    if re.search(r"\bhow much\b", t):
        return A_MONEY if _MONEY_WORDS.search(t) else A_NUMBER
    if re.search(rf"^(in |on |at |by )?(what|which)( \w+)?( \w+)? ({_DATE_NOUNS})\b|^when\b", t) and not re.search(r"^(what's|what is|what was)\b", t):
        return A_DATE
    if t.startswith("where") or re.search(rf"^(what|which|in which|in what|at which)( \w+)? ({_PLACE_NOUNS})\b", t):
        return A_PLACE
    if re.search(r"^who\b|^who's\b|^whose\b", t):
        return A_PERSON if re.search(r"\b(coordinator|covers|in settlements|trader|manager|analyst|contact|person)\b", t) else A_COMPANY
    if re.search(r"^what (are|months)\b", t):
        return A_LIST
    if t.startswith("which") or re.search(r"^(in|on|for|at) which\b", t):
        plural = re.search(r"^which (\w+ )?(\w+ies|\w+[^s]s) (can|are|have|do|does|did|were)\b", t)
        if plural and not re.search(rf"\b({_SUPERLATIVE})\b", t):
            return A_LIST
        return A_COMPANY if re.search(rf"^which( \w+)?( \w+)? ({_COMPANY_NOUNS})\b", t) else A_CODE
    if re.search(rf"^(what's|what is|what was|what)( \w+){{0,4}} ({_TEXT_NOUNS})\b", t):
        return A_CODE
    return A_MONEY if _MONEY_WORDS.search(t) else A_NUMBER


def _closed(question: str, low: str, out: dict[str, float]) -> None:
    """The closed heads: form, aggregate, direction, size, entity, operators, answer type."""
    first = low.split(" ", 1)[0] if low else ""
    forms = {SHOW: 0.3, AGG: 0.3, RANK: NO_RANK_WORD, CALC: 0.05}
    if first in ("which", "who", "whose"):
        forms.update({RANK: 0.7, SHOW: 0.3, AGG: 0.0})
    elif low.startswith("how many") or low.startswith("how much"):
        forms.update({AGG: 0.8, SHOW: 0.2})
    elif first in ("where",):
        forms.update({SHOW: 0.8, AGG: 0.0})
    elif first in ("what", "what's"):
        forms.update({AGG: 0.5, SHOW: 0.5})
    high, lowest = bool(_HIGH.search(low)), bool(_LOW.search(low))
    if high or lowest:
        forms[RANK] = max(forms[RANK], 0.7)
    if _SUM.search(low) or _AVG.search(low):
        forms[AGG] = max(forms[AGG], 0.7)
    calc_minus, calc_over, calc_plus = bool(_CALC_MINUS.search(low)), bool(_CALC_OVER.search(low)), bool(_CALC_PLUS.search(low))
    forms[CALC] = 0.0 if calc_minus or calc_over or calc_plus else NO_CALC_WORD
    for form, score in forms.items():
        out[coordinate(SLOT_FORM, form)] = score
    out[coordinate(SLOT_CALC_OP, MINUS)] = 0.0 if calc_minus else NO_ROW_WORD
    out[coordinate(SLOT_CALC_OP, OVER)] = 0.0 if calc_over else NO_ROW_WORD
    out[coordinate(SLOT_CALC_OP, PLUS)] = 0.0 if calc_plus else NO_ROW_WORD
    sign = sign_word_of(question)
    read = read_sign(sign, stored_negative=True)
    if high or forms[RANK] >= 0.7:
        out[coordinate(SLOT_DIRECTION, HIGHEST)] = 0.8 if high else 0.3
    if lowest or read.direction == LOWEST:
        out[coordinate(SLOT_DIRECTION, LOWEST)] = 0.8
    if read.by_size:
        out[coordinate(SLOT_SIZE, BY_SIZE)] = 0.6
        out[coordinate(SLOT_SIZE, EACH_BY_SIZE)] = 0.6
    if read.magnitude:
        out[coordinate(SLOT_SIZE, STATED_BY_SIZE)] = 0.6
    out[coordinate(SLOT_AGGREGATE, SUM)] = 0.8 if _SUM.search(low) else 0.4
    out[coordinate(SLOT_AGGREGATE, AVG)] = 0.9 if _AVG.search(low) else 0.05
    out[coordinate(SLOT_AGGREGATE, COUNT)] = 0.9 if _COUNT.search(low) else 0.1
    out[coordinate(SLOT_AGGREGATE, COUNT_DISTINCT)] = 0.7 if _DISTINCT.search(low) and _COUNT.search(low) else 0.02
    out[coordinate(SLOT_AGGREGATE, MAX)] = 0.3 if high else 0.05
    out[coordinate(SLOT_AGGREGATE, MIN)] = 0.3 if lowest else 0.05
    if forms[RANK] >= 0.7:
        out[coordinate(SLOT_ENTITY, ENTITY_GROUP)] = 0.5
        out[coordinate(SLOT_ENTITY, ENTITY_LINE)] = 0.5
        out[coordinate(SLOT_ENTITY, ENTITY_PEER)] = 0.3
    out[coordinate(SLOT_ROW_OP, TIMES)] = ROW_WORD if _ROW_TIMES.search(low) else NO_ROW_WORD
    out[coordinate(SLOT_ROW_OP, MINUS)] = ROW_WORD if _ROW_MINUS.search(low) else NO_ROW_WORD
    out[coordinate(SLOT_ROW_OP, PLUS)] = ROW_WORD if _ROW_PLUS.search(low) else NO_ROW_WORD
    out[coordinate(SLOT_ROW_OP, OVER)] = ROW_WORD if calc_over else NO_ROW_WORD
    if _BLANK.search(low):
        out[coordinate(SLOT_BLANK, "blank")] = 0.3
    out[coordinate(SLOT_ANSWER_TYPE, coarse_type(predict_answer_type(question)))] = 0.5
    for count in (2, 3):
        out[coordinate(SLOT_SHOWN, TOGETHER, count)] = -SHOWN_MORE * (count - 1)


def _periods(low: str, out: dict[str, float]) -> None:
    """Periods from the question's own words; a period needs no value slot. "So far" adds nothing of its own; "the rest of May" is a status column, so its month is no condition."""
    reading = read_period(low)
    if reading is not None and reading.kind == PERIOD_REST_OF:
        return
    years = _YEAR_WORD.findall(low)
    for match in _DATE_FORMS.finditer(low):
        reading = read_period(match.group(0))
        if reading is not None and reading.kind == PERIOD_NAMED and reading.period is not None and reading.period.start is not None:
            _span_score(reading.period, out, 1.0)
    for match in _MONTH_WORD.finditer(low):
        word = match.group(1)
        if word == "may" and not re.search(r"\b(?:in|of|for|during) may\b|\bmay (?:\d|19|20)", low):
            continue
        month = _MONTHS.index(word[:3]) + 1
        out[coordinate(SLOT_PERIOD, f"m{month:02d}")] = 0.8
        for year in years:
            period = parse_period(f"{word[:3]} {year}")
            if period is not None:
                _span_score(period, out, 1.0)
    for year in years:
        out.setdefault(coordinate(SLOT_PERIOD, year), 0.9)


def _span_score(period, out: dict[str, float], score: float) -> None:
    span = None
    if period.kind in (P_DAY, P_MONTH, P_YEAR, P_QUARTER):
        span = PeriodSpan(period.kind, period.start, period.end)
    elif period.start is not None:
        span = PeriodSpan(P_RANGE, period.start, period.end)
    if span is not None:
        out[coordinate(SLOT_PERIOD, span.name)] = score


def _numbers(low: str, out: dict[str, float]) -> None:
    """Numbers the question states, with what compares them. A sign word states a comparison with zero: positive, negative, nonzero."""
    for op, pattern in _SIGNED_ZERO:
        if pattern.search(low):
            out[coordinate(SLOT_NUMBER, number_text(0.0))] = 1.0
            out[coordinate(SLOT_COMPARISON, op)] = 1.0
    asked = [op for op, pattern in _COMPARE if pattern.search(low)]
    strength = 1.0 if asked else NUMBER_ALONE
    found = False
    times = []
    for match in _TIME_STATED.finditer(low):     # "at least 35:00": a time, read as a sheet's time cells are
        found = True
        times.append(match.span())
        out[coordinate(SLOT_NUMBER, number_text(duration_seconds(match.group(0))))] = strength
    for match in _NUMBER.finditer(low):
        text = match.group(0).strip()
        if _ORDINAL_AFTER.match(low, match.end()) or any(start <= match.start() < end for start, end in times):
            continue
        value = stated_number(re.sub(r"\s*percent$", "%", text))
        if value is None or _YEAR_WORD.fullmatch(text):
            continue
        found = True
        out[coordinate(SLOT_NUMBER, number_text(value))] = strength
        if "%" in text or "percent" in text:
            out[coordinate(SLOT_NUMBER, number_text(value * 100))] = strength / 2
    for match in _NUMBER_WORD.finditer(low):     # "eight or more goals"; "one" only after a comparison ("more than one")
        word = match.group(1)
        if word == "one" and not _COMPARED_BEFORE.search(low, 0, match.start()):
            continue
        found = True
        out[coordinate(SLOT_NUMBER, number_text(float(_NUMBER_WORDS[word])))] = strength
    for op in asked:
        if found or op == NE:          # "other than EES" needs no number
            out[coordinate(SLOT_COMPARISON, op)] = 1.0


def _phrases(words: list[str]) -> list[tuple[str, ...]]:
    out = []
    for size in (4, 3, 2, 1):
        for at in range(len(words) - size + 1):
            phrase = tuple(words[at:at + size])
            if any(word not in _STOP for word in phrase) and phrase[0] not in _STOP and phrase[-1] not in _STOP:
                out.append(phrase)
    return out


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


def _table_words(table_id: str, index: TableIndex, question_words: set[str], raw_words: list[str], low: str, out: dict[str, float], *, periods: bool, comparison: bool) -> None:
    """Word index matches on one table: names, then values, then printed labels and bands."""
    table = index.table
    title = index.title_words
    spelled = [index.spelled_as(word) for word in question_words if word not in index.name_counts and word not in index.value_words and word not in _STOP]
    if any(spelled):                           # a misspelled word reads as the word of this sheet it means
        question_words = question_words | {word for word in spelled if word}
    if title:
        hits = (title & question_words)
        if hits:
            out[coordinate(SLOT_TABLE, table_id)] = len(hits) / max(3, len(title))
    for position, name in index.name_words.items():
        if not name:
            continue
        held = name & question_words
        share = len(held) / len(name)
        if any(index.name_counts[word] == 1 and word not in _STOP for word in held):
            share = max(share, ONLY_NAME_WITH_WORD)
        if share:
            col = Col(table_id, position).name
            for slot in (SLOT_MEASURE, SLOT_KEY, SLOT_SHOWN):
                out[coordinate(slot, col)] = share
            out[coordinate(SLOT_FILTER, col)] = share * FILTER_NAME_SHARE
    named = [(min((raw_words.index(word) for word in raw_words if stem(word) in name), default=len(raw_words)), position) for position, name in index.name_words.items() if name & question_words]
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
    # an axis: its name or its members' names in the question, and the word across. One named only by a total beside it, or a flag axis, needs a word of its own
    for axis in table.table.axes:
        names = [index.name_words.get(position, frozenset()) for position in axis.positions]
        share = sum(bool(name & question_words) for name in names) / max(1, len(names))
        across = bool(_ACROSS.search(low)) and axis.kind != AXIS_FLAG
        if share or across:
            out[coordinate(SLOT_PEERS, table_id, peers_name(tuple(axis.positions)))] = round(max(share, 0.3 if across else 0.0), 4)
    # values: every value holding a question word, by how much of it the question covers
    seen: set[tuple[int, str]] = set()
    value_words = index.value_words
    for word in question_words:
        for position, folded in value_words.get(word, ()):
            seen.add((position, folded))
    for position, folded in seen:
        text = words_of(folded)
        if all(word in _STOP for word in text):      # "a", "or", "in" as a cell: the question's small word is no reading of it
            continue
        share = sum(word in question_words for word in text) / len(text) if text else 0.0
        whole = re.search(rf"(?<!\w){re.escape(folded)}(?!\w)", low) is not None
        out[coordinate(SLOT_VALUE, Col(table_id, position).name, folded)] = 1.0 if whole else 0.8 * share
    # a phrase inside longer cells: the partial match, whole words first, then a word's prefix
    phrases = _phrases(raw_words)
    for position, lines in index.value_lines.items():
        if table.columns[position].kind == "number":
            continue
        col = Col(table_id, position).name
        for phrase in phrases:
            words = [stem(word) for word in phrase]
            if not all(word in value_words for word in words):
                continue
            text = " ".join(phrase)
            holders = {f for p, f in value_words[words[0]] if p == position}
            if any(text in folded for folded in holders) and not exact_wins(text, lines):
                out[coordinate(SLOT_CONTAINS, col, text)] = min(1.0, 0.5 + 0.1 * len(phrase))
    for word in set(raw_words) - _STOP:
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
    asked = len(columns) + (1 if periods and index.date_columns else 0) + (1 if comparison and index.number_columns else 0)
    asked += 1 if any(name.startswith((f"{SLOT_LINE}:{table_id}=", f"{SLOT_SECTION}:{table_id}=")) and score >= 0.8 for name, score in out.items()) else 0
    for count in range(7):
        out[coordinate(SLOT_CONDITIONS, table_id, count)] = -EXTRA_CONDITION * max(0, count - asked) - UNREAD_CONDITION * max(0, asked - count)


def read_question(question: str, catalog: Catalog, decoder: Decoder) -> Reading:
    """The question's reading from its words alone."""
    low = fold(question).replace("’", "'")
    raw_words = re.findall(r"[a-z0-9]+", low)
    question_words = {stem(word) for word in raw_words}
    out: dict[str, float] = {}
    _closed(question, low, out)
    _periods(low, out)
    _numbers(low, out)
    periods = any(name.startswith(f"{SLOT_PERIOD}:") for name in out)
    comparison = any(name.startswith(f"{SLOT_COMPARISON}:") for name in out)
    for table in catalog.tables:
        if table.lines:
            _table_words(table.id, decoder.indexes[table.id], question_words, raw_words, low, out, periods=periods, comparison=comparison)
    return Reading(out)


__all__ = ["predict_answer_type", "read_question"]
