"""What a catalog table can be asked about, computed once per table: its conditions (atoms) and the lines each keeps, and a word index over names, values, labels and bands.

An atom is one possible condition with the coordinates that score it. A cell value, a printed label and a band are atoms from the sheet alone; a contained phrase, a period and a stated number come from a reading's coordinates at question time (decoder.py). Lines kept are
Python integers used as bitmasks, so "these two conditions sit together on a line" is one AND.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from functools import cached_property

import numpy as np

from engine.program.catalog import CatalogTable
from engine.program.language import Col, Compare, CompareLine, Condition, Contains, Equals, InPeriod, InSection, IsBlank, IsNumber, OnLine, OneOf, condition_coordinates
from engine.program.names import ENDS, EQ, GE, GT, LE, LT, NE, STARTS
from engine.program.semantics import CURRENCY_CODES, fold
from engine.sheet.values import parse_number
from engine.sheet.names import AXIS_FLAG, AXIS_TOTALLED, F_DATE, PEER, ROW_TOTAL, V_NUMBER

# A low-cardinality number column's values are values too, up to this many (design/program_language.txt, section 3).
NUMBER_VALUES = 30
_TOKEN = re.compile(r"[a-z0-9]+")
_LINE_GROUP = -2
_SECTION_GROUP = -3


FUZZY_SHORTEST = 5
FUZZY_LONG = 8


def _within(a: str, b: str, most: int) -> int | None:
    """The edit distance between two words, a swap of neighbours counting one, or None past most."""
    previous2, previous = None, list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            cost = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y))
            if previous2 is not None and j > 1 and x == b[j - 2] and a[i - 2] == y:
                cost = min(cost, previous2[j - 2] + 1)
            current.append(cost)
        if min(current) > most:
            return None
        previous2, previous = previous, current
    return previous[-1] if previous[-1] <= most else None


def words_of(text: str) -> tuple[str, ...]:
    """A text's words: folded, letters and digits, a plural's s off."""
    return tuple(stem(word) for word in _TOKEN.findall(fold(text)))


def stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


@dataclass(frozen=True, slots=True)
class Atom:
    """One possible condition. group says which conditions cannot combine: two on one column, or two on a line's label, keep the policy's distinct-column rule."""
    condition: Condition
    group: int
    names: tuple[str, ...]


class _LazyArrays(dict):
    """Each column's numbers as an array with NaN for a blank, built when first read."""

    def __init__(self, numbers):
        super().__init__()
        self.numbers = numbers

    def __missing__(self, position: int) -> np.ndarray:
        self[position] = np.array([np.nan if number is None else number for number in self.numbers[position]], dtype=float)
        return self[position]


COMPARE_ARRAYS = {EQ: np.equal, GE: np.greater_equal, GT: np.greater, LE: np.less_equal, LT: np.less, NE: np.not_equal}


def _as_mask(flags: np.ndarray) -> int:
    return int.from_bytes(np.packbits(flags, bitorder="little").tobytes(), "little")


def bitmask(indices: list[int], size: int) -> int:
    bits = bytearray(size // 8 + 1)
    for i in indices:
        bits[i >> 3] |= 1 << (i & 7)
    return int.from_bytes(bits, "little")


def index_of(table: CatalogTable) -> "TableIndex":
    """A catalog table's one index, built when first read and kept with it."""
    held = table.__dict__.get("index")
    if held is None:
        held = table.__dict__["index"] = TableIndex(table)
    return held


class TableIndex:
    """The atoms and word index of one catalog table."""

    def __init__(self, table: CatalogTable):
        self.table = table
        self._masks: dict[Condition, int] = {}

    @cached_property
    def value_lines(self) -> dict[int, dict[str, list[int]]]:
        """Per column, fold -> the lines holding it."""
        out: dict[int, dict[str, list[int]]] = {}
        for position, folds in enumerate(self.table.folds):
            lines: dict[str, list[int]] = {}
            for i, folded in enumerate(folds):
                if folded is not None:
                    lines.setdefault(folded, []).append(i)
            out[position] = lines
        return out

    @cached_property
    def number_columns(self) -> tuple[int, ...]:
        return tuple(position for position, numbers in enumerate(self.table.numbers) if any(number is not None for number in numbers))

    @cached_property
    def few_numbers(self) -> dict[int, list[float]]:
        """Number columns holding at most NUMBER_VALUES distinct numbers (a year, a rating, a count of acts), each with
        its numbers in order: their values are conditions ("cars from 2020") and words a question names."""
        out = {}
        for position, column in enumerate(self.table.columns):
            if column.kind == V_NUMBER:
                numbers = sorted({number for number in self.table.numbers[position] if number is not None})
                if len(numbers) <= NUMBER_VALUES:
                    out[position] = numbers
        return out

    @cached_property
    def quantity_columns(self) -> frozenset[int]:
        """Number columns the sheet writes with a unit ("12 months"): at least 4 in 5 of their texts are not plain numbers.
        Their texts are values a question names too ("products that give 12 months")."""
        out = set()
        for position in self.number_columns:
            texts = [text for text in self.table.texts[position] if text is not None]
            if texts and sum(parse_number(text) is None for text in texts) * 5 >= len(texts) * 4:
                out.add(position)
        return frozenset(out)

    @cached_property
    def currency_column(self) -> int | None:
        """The first text column whose every filled cell is an ISO 4217 code, holding at least two: it names the currency
        of its line's amounts, and a figure over lines in different currencies is given per currency."""
        for position in self.text_columns:
            codes = set(self.value_lines.get(position, {}))
            if len(codes) >= 2 and codes <= CURRENCY_CODES:
                return position
        return None

    @cached_property
    def text_columns(self) -> tuple[int, ...]:
        return tuple(column.position for column in self.table.columns if column.kind != V_NUMBER and column.filled and not self.table.is_date_column(column.position))

    @cached_property
    def date_columns(self) -> tuple[int, ...]:
        return tuple(column.position for column in self.table.columns if self.table.is_date_column(column.position))

    @cached_property
    def mixed_columns(self) -> tuple[int, ...]:
        """Columns holding numbers and other cells too, where "is a number" is a real condition."""
        out = []
        for position in self.number_columns:
            if any(number is None and folded is not None for number, folded in zip(self.table.numbers[position], self.table.folds[position])):
                out.append(position)
        return tuple(out)

    @cached_property
    def blank_columns(self) -> tuple[int, ...]:
        """Columns filled on some data lines and blank on others, where "is blank" is a real condition."""
        out = []
        for position, folds in enumerate(self.table.folds):
            data = [folded is None for folded, is_data in zip(folds, self.table.is_data) if is_data]
            if any(data) and not all(data):
                out.append(position)
        return tuple(out)

    @cached_property
    def atoms(self) -> tuple[Atom, ...]:
        """Every condition the sheet alone can state: a cell's value on its column, a band, a printed line's label, a mixed column's numbers. In a stable order."""
        table = self.table
        out: list[Atom] = []
        for position in self.mixed_columns:                 # what a column's shape can say comes before its many values, so a slot's top k keeps it at equal scores
            out.append(self._atom(IsNumber(Col(table.id, position)), position))
        for position in self.blank_columns:
            out.append(self._atom(IsBlank(Col(table.id, position)), position))
        for position in range(len(table.columns)):
            column = table.columns[position]
            col = Col(table.id, position)
            lines = self.value_lines[position]
            if column.kind == V_NUMBER:
                out.extend(self._atom(Equals(col, number), position) for number in self.few_numbers.get(position, ()))
                continue
            if table.is_date_column(position) or column.format_kind == F_DATE:
                continue
            for folded in sorted(lines):
                out.append(self._atom(Equals(col, folded), position))
        seen_bands = set()
        for line in table.lines:
            for band in line.section:
                folded = fold(band)
                if folded and folded not in seen_bands:
                    seen_bands.add(folded)
                    out.append(self._atom(InSection(table.id, folded), _SECTION_GROUP))
        seen_labels = set()
        for line, data, label in zip(table.lines, table.is_data, table.label_folds):
            if data and label:
                continue
            key = label or line.role
            if key not in seen_labels:
                seen_labels.add(key)
                out.append(self._atom(OnLine(table.id, label) if label else OnLine(table.id, "", line.role if not data else None), _LINE_GROUP))
        return tuple(out)

    @cached_property
    def structural_atoms(self) -> tuple[Atom, ...]:
        """The conditions no word can reach and a question can still mean: what a column's shape says, and a printed line the sheet leaves unlabelled. Everything else comes through the reading's own coordinates."""
        table = self.table
        out = [self._atom(IsNumber(Col(table.id, position)), position) for position in self.mixed_columns]
        out.extend(self._atom(IsBlank(Col(table.id, position)), position) for position in self.blank_columns)
        roles: dict[str, None] = {}
        for line, data, label in zip(table.lines, table.is_data, table.label_folds):
            if not data and not label:
                roles.setdefault(line.role, None)
        out.extend(self._atom(OnLine(table.id, "", role), _LINE_GROUP) for role in roles)
        return tuple(out)

    def warm(self) -> None:
        """Every word index and shape a question reads."""
        _ = (self.name_counts, self.sheet_words, self.value_lines, self.number_columns, self.text_columns, self.date_columns, self.structural_atoms, self.value_words, self.sorted_words, self.name_words, self.title_words, self.printed_labels, self.bands, self.peer_sets, self.peer_marks, self.table.column_rank, self.day_lines)

    def atom_of(self, condition: Condition, group: int) -> Atom:
        return self._atom(condition, group)

    @staticmethod
    def _atom(condition: Condition, group: int) -> Atom:
        return Atom(condition, group, condition_coordinates(condition))

    def mask(self, condition: Condition) -> int:
        """The lines a condition keeps. The same answer as CatalogTable.condition_mask, by way of each column's distinct values: tests hold the two together."""
        held = self._masks.get(condition)
        if held is None:
            held = self._read_mask(condition)
            self._masks[condition] = held
        return held

    def _read_mask(self, condition: Condition) -> int:
        size = len(self.table.lines)
        if isinstance(condition, (Equals, Contains)) and condition.column.position >= 0:
            position = condition.column.position
            if isinstance(condition, Equals):
                if isinstance(condition.value, str):
                    return bitmask(self.value_lines[position].get(fold(condition.value), []), size)
                return _as_mask(self.number_arrays[position] == condition.value)
            want = fold(condition.text)
            found = (text.startswith(want) if condition.anchor == STARTS else text.endswith(want) if condition.anchor == ENDS else want in text for text in self.value_lines[position])
            lines = [line for text, hit in zip(self.value_lines[position], found) if hit for line in self.value_lines[position][text]]
            return bitmask(lines, size)
        if isinstance(condition, OneOf) and condition.column.position >= 0:
            lines = self.value_lines[condition.column.position]
            return bitmask([line for value in condition.values for line in lines.get(fold(value), [])], size)
        if isinstance(condition, Compare):
            return _as_mask(COMPARE_ARRAYS[condition.op](self.number_arrays[condition.column.position], condition.number) & ~np.isnan(self.number_arrays[condition.column.position]))
        if isinstance(condition, IsNumber):
            return _as_mask(~np.isnan(self.number_arrays[condition.column.position]))
        if isinstance(condition, CompareLine):
            anchor = self.table.line_number(condition)
            if anchor is None:
                return 0
            numbers = self.number_arrays[condition.column.position]
            named = bitmask(self.value_lines[condition.key.position].get(fold(condition.value), []), size)
            return _as_mask(COMPARE_ARRAYS[condition.op](numbers, anchor) & ~np.isnan(numbers)) & ~named
        if isinstance(condition, InPeriod):
            return self._period_mask(condition)
        return self.table.condition_mask(condition)

    def _period_mask(self, condition: InPeriod) -> int:
        span = condition.span
        lines = []
        for day, held in self.day_lines[condition.column.position].items():
            if int(day[5:7]) == span.month if span.start is None else span.start <= day <= (span.end or span.start):
                lines.extend(held)
        return bitmask(lines, len(self.table.lines))

    @cached_property
    def number_arrays(self) -> dict[int, np.ndarray]:
        return _LazyArrays(self.table.numbers)

    @cached_property
    def day_lines(self) -> dict[int, dict[str, list[int]]]:
        out: dict[int, dict[str, list[int]]] = {}
        for position, days in enumerate(self.table.days):
            lines: dict[str, list[int]] = {}
            for i, day in enumerate(days):
                if day is not None:
                    lines.setdefault(day, []).append(i)
            out[position] = lines
        return out

    # ------------------------------------------------------------------------------------ the word index
    @cached_property
    def name_words(self) -> dict[int, frozenset[str]]:
        return {column.position: frozenset(words_of(column.name)) for column in self.table.columns}

    @cached_property
    def name_counts(self) -> dict[str, int]:
        """How many of this table's columns hold a word in their names."""
        out: dict[str, int] = {}
        for words in self.name_words.values():
            for word in words:
                out[word] = out.get(word, 0) + 1
        return out

    @cached_property
    def sheet_words(self) -> dict[int, tuple[str, ...]]:
        """The words a question's misspelling may mean, by length: the table's names, then its text values."""
        by_length: dict[int, set[str]] = {}
        for word in (*self.name_counts, *self.value_words):
            if len(word) >= FUZZY_SHORTEST - 1:
                by_length.setdefault(len(word), set()).add(word)
        return {length: tuple(sorted(words)) for length, words in by_length.items()}

    def spelled_as(self, word: str) -> str | None:
        """The one word of this table a misspelled word means: one edit away, two from a long word, the first letter kept. None when none is, or when two are equally near."""
        if len(word) < FUZZY_SHORTEST or word.isdigit():
            return None
        most = 2 if len(word) >= FUZZY_LONG else 1
        found: list[tuple[int, str]] = []
        for length in range(len(word) - most, len(word) + most + 1):
            for held in self.sheet_words.get(length, ()):
                if held[0] == word[0]:
                    distance = _within(word, held, most)
                    if distance is not None:
                        found.append((distance, held))
        if not found:
            return None
        nearest = min(distance for distance, _ in found)
        held = [word for distance, word in found if distance == nearest]
        return held[0] if len(held) == 1 else None

    @cached_property
    def title_words(self) -> frozenset[str]:
        words = set(words_of(self.table.tab)) | set(words_of(self.table.table.name))
        for _, text in self.table.table.context.titles:
            words.update(words_of(text))
        return frozenset(words)

    @cached_property
    def value_words(self) -> dict[str, list[tuple[int, str]]]:
        """A word -> the (column, fold) of the text values holding it."""
        out: dict[str, list[tuple[int, str]]] = {}
        for position, lines in self.value_lines.items():
            if self.table.columns[position].kind == V_NUMBER and position not in self.quantity_columns and position not in self.few_numbers:
                continue
            for folded in lines:
                for word in set(words_of(folded)):
                    out.setdefault(word, []).append((position, folded))
        return out

    @cached_property
    def sorted_words(self) -> list[str]:
        return sorted(self.value_words)

    def words_with_prefix(self, prefix: str) -> list[str]:
        at = bisect.bisect_left(self.sorted_words, prefix)
        out = []
        while at < len(self.sorted_words) and self.sorted_words[at].startswith(prefix):
            out.append(self.sorted_words[at])
            at += 1
        return out

    @cached_property
    def printed_labels(self) -> dict[str, list[int]]:
        """A printed line's label -> its lines; the empty label is a printed line the sheet leaves unlabelled."""
        out: dict[str, list[int]] = {}
        for i, (data, label) in enumerate(zip(self.table.is_data, self.table.label_folds)):
            if not data:
                out.setdefault(label, []).append(i)
        return out

    @cached_property
    def bands(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for line in self.table.lines:
            for band in line.section:
                seen.setdefault(fold(band), None)
        return tuple(seen)

    @cached_property
    def peer_sets(self) -> tuple[tuple[int, ...], ...]:
        """The peer column sets a number is read across, stated ones first: each axis of figures; the number columns that are not row totals; each run of adjacent number columns, row totals and columns named total left out; and columns sharing a word of their names. An axis named only by a total beside it is read when a question asks for it (totalled_sets), and a flag axis counts marks (flag_sets)."""
        columns = self.table.columns
        out: list[tuple[int, ...]] = [tuple(axis.positions) for axis in self.table.table.axes if axis.kind not in (AXIS_FLAG, AXIS_TOTALLED)]
        rest = tuple(position for position in self.number_columns if columns[position].role != ROW_TOTAL)
        if len(rest) >= 2:
            out.append(rest)
        peers = tuple(column.position for column in columns if column.role == PEER)
        if len(peers) >= 2:
            out.append(peers)
        run: list[int] = []
        for position in range(len(columns) + 1):
            if position < len(columns) and position in self.number_columns and columns[position].role != ROW_TOTAL and "total" not in self.name_words[position]:
                run.append(position)
                continue
            if len(run) >= 3:
                out.append(tuple(run))
            run = []
        sharing: dict[str, list[int]] = {}
        for position in self.number_columns:
            for word in self.name_words[position]:
                sharing.setdefault(word, []).append(position)
        for word in sorted(sharing):
            if len(sharing[word]) >= 3:
                out.append(tuple(sharing[word]))
        apart = {tuple(axis.positions) for axis in self.table.table.axes if axis.kind in (AXIS_FLAG, AXIS_TOTALLED)}
        return tuple(held for held in dict.fromkeys(out) if held not in apart)

    @cached_property
    def stated_sets(self) -> frozenset[tuple[int, ...]]:
        """The peer sets an axis states, whatever its kind: a tie among peer sets goes to these."""
        return frozenset(tuple(axis.positions) for axis in self.table.table.axes)

    @cached_property
    def totalled_sets(self) -> tuple[tuple[int, ...], ...]:
        """Columns a column named total stands beside. That name is no proof they add up, so these are peers only where a question asks for them."""
        return tuple(tuple(axis.positions) for axis in self.table.table.axes if axis.kind == AXIS_TOTALLED)

    @cached_property
    def flag_sets(self) -> tuple[tuple[int, ...], ...]:
        """Text columns marking the same few values across: what a "most marked" rank counts over."""
        return tuple(tuple(axis.positions) for axis in self.table.table.axes if axis.kind == AXIS_FLAG)

    def mark_columns(self, mark: str) -> tuple[int, ...]:
        """The text columns holding a mark: the peers a "most marked" rank reads across."""
        return tuple(position for position, lines in self.value_lines.items() if mark in lines and self.table.columns[position].kind != V_NUMBER)

    @cached_property
    def peer_marks(self) -> tuple[str, ...]:
        """Short marks repeated across a table's text columns (Y, N, X): what a "most marked" column counts."""
        counts: dict[str, int] = {}
        for position, lines in self.value_lines.items():
            if self.table.columns[position].kind == V_NUMBER:
                continue
            for folded, held in lines.items():
                if len(folded) <= 3 and len(held) >= 2:
                    counts[folded] = counts.get(folded, 0) + len(held)
        return tuple(sorted(counts, key=lambda mark: (-counts[mark], mark))[:5])

