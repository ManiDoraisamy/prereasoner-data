"""A compiled workbook as programs see it: every table (the compiler's tables, its families, each tab's notes as a one-line-per-note table) under a stable id, each column read once.

A catalog table holds its lines in sheet order, data and printed together, and per column the one reading of each cell (semantics.py): text, fold, number, day. The store loads those into SQLite and the decoder's index reads them,
so no second reading of a cell exists. Everything here is computed once per workbook at index time; nothing depends on a question.
"""
from __future__ import annotations

import hashlib
from functools import cached_property

from engine.program.language import (Col, Compare, CompareColumns, CompareLine, Condition, Contains, Equals, InPeriod, InYears, InSection, IsBlank, IsNumber, NotEquals, OnLine, OneOf, SECTION)
from engine.program.names import ANYWHERE, ENDS, EQ, GE, GT, LE, LT, NE, STARTS
from engine.program.semantics import cell_day, cell_fold, cell_number, cell_text, cell_written, duration_numbers, fold, is_filled
from engine.sheet.model import Cell, Column, Context, Line, SheetModel, Table
from engine.sheet.names import LABEL, NOTE, PRINTED_ROLES, RECORD, V_DATE, V_NUMBER, V_PERIOD, V_TEXT

KIND_TABLE = "table"
KIND_FAMILY = "family"
KIND_NOTES = "notes"
SECTION_NAME = "§"      # the band column's shown name: a sign, not a word a question could hold
PROSE_WORDS = 5         # a text column of prose: half its cells hold at least this many words
PROSE_LINES = 5         # ... over at least this many filled text cells



class CatalogTable:
    """One table a program can read. id is stable for a workbook: "t3" by compiled order, "f1" a family, "n2" a tab's notes."""

    __slots__ = ("id", "table", "kind", "adds_twice", "lines", "folds", "numbers", "days", "texts", "written", "__dict__")

    def __init__(self, table_id: str, table: Table, kind: str, adds_twice: bool = False):
        self.id = table_id
        self.table = table
        self.kind = kind
        self.adds_twice = adds_twice
        order = {}
        for line in (*table.lines, *table.printed):
            order.setdefault(line.tab, len(order))
        self.lines: list[Line] = sorted((*table.lines, *table.printed), key=lambda line: (order[line.tab], line.row))
        self.texts: list[list[str | None]] = []
        self.written: list[list[str | None]] = []      # each cell as an answer prints it: as the sheet wrote it (cell_written)
        self.folds: list[list[str | None]] = []
        self.numbers: list[list[float | None]] = []
        self.days: list[list[str | None]] = []
        self.durations: set[int] = set()      # the columns of times ("3:36", "2:05:31"), their numbers in seconds
        for position in range(len(table.columns)):
            cells = [line.cells.get(position) for line in self.lines]
            self.texts.append([cell_text(cell) for cell in cells])
            self.written.append([cell_written(cell) for cell in cells])
            self.folds.append([cell_fold(cell) for cell in cells])
            numbers = [cell_number(cell) for cell in cells]
            if table.columns[position].kind != V_NUMBER and not any(number is not None for number in numbers):
                times = duration_numbers(self.texts[-1])     # "35:00", "2:05:31": times read as numbers too
                if times is not None:
                    numbers = times
                    self.durations.add(position)
            self.numbers.append(numbers)
            self.days.append([cell_day(cell) for cell in cells])

    def __repr__(self) -> str:
        return f"CatalogTable({self.id}, {self.table.tab!r}, {len(self.table.columns)} columns, {len(self.lines)} lines)"

    @property
    def columns(self) -> list[Column]:
        return self.table.columns

    @property
    def tab(self) -> str:
        return self.table.tab

    def column_name(self, position: int) -> str:
        return SECTION_NAME if position == SECTION else self.table.columns[position].name

    def col(self, position: int) -> Col:
        return Col(self.id, position)

    @cached_property
    def is_data(self) -> list[bool]:
        return [line.role not in PRINTED_ROLES for line in self.lines]

    @cached_property
    def data_mask(self) -> int:
        return _mask(i for i, data in enumerate(self.is_data) if data)

    @cached_property
    def all_mask(self) -> int:
        return (1 << len(self.lines)) - 1

    @cached_property
    def section_texts(self) -> list[str | None]:
        return [" ".join(line.section[-1].split()) if line.section else None for line in self.lines]

    @cached_property
    def section_paths(self) -> list[str | None]:
        """Each line's bands, outermost first, folded, between markers: "| atlanta gas light | ibss |" holds a band whole only between two of them."""
        return ["| " + " | ".join(fold(band) for band in line.section) + " |" if line.section else None for line in self.lines]

    @cached_property
    def section_folds(self) -> list[str | None]:
        return [None if text is None else fold(text) for text in self.section_texts]

    @cached_property
    def prose_columns(self) -> tuple[int, ...]:
        """Text columns of prose: at least PROSE_LINES filled text cells, half of them PROSE_WORDS words or more (a post,
        a review, a comment). Their words are a number a question can rank or add ("the longest post")."""
        out = []
        for position, column in enumerate(self.table.columns):
            if column.kind == V_NUMBER:
                continue
            counts = sorted(len(text.split()) for text, number in zip(self.texts[position], self.numbers[position]) if text is not None and number is None)
            if len(counts) >= PROSE_LINES and counts[len(counts) // 2] >= PROSE_WORDS:
                out.append(position)
        return tuple(out)

    @cached_property
    def word_counts(self) -> dict[int, list[float | None]]:
        """Each prose column's cells as their number of words (texts are stored with spaces cleaned); None for a blank or a figure."""
        return {position: [None if text is None or number is not None else float(len(text.split())) for text, number in zip(self.texts[position], self.numbers[position])]
                for position in self.prose_columns}

    def words(self, position: int) -> list[float | None]:
        return self.word_counts[position]

    @cached_property
    def label_folds(self) -> list[str]:
        return [fold(line.label) for line in self.lines]

    @cached_property
    def printed_only_columns(self) -> frozenset[int]:
        """Columns that hold numbers only on printed lines: those lines are the column's data, since nothing else fills it."""
        out = set()
        for position, numbers in enumerate(self.numbers):
            on_data = any(number is not None and data for number, data in zip(numbers, self.is_data))
            on_printed = any(number is not None and not data for number, data in zip(numbers, self.is_data))
            if on_printed and not on_data:
                out.add(position)
        return frozenset(out)

    @cached_property
    def spelling(self) -> dict[int, dict[str, str]]:
        """Per column, fold -> the first stored spelling, so an answer keyed by the fold shows the sheet's words."""
        out: dict[int, dict[str, str]] = {}
        for position in range(len(self.table.columns)):
            words: dict[str, str] = {}
            for text, folded in zip(self.texts[position], self.folds[position]):
                if folded is not None and folded not in words:
                    words[folded] = text
            out[position] = words
        words = {}
        for text, folded in zip(self.section_texts, self.section_folds):
            if folded is not None and folded not in words:
                words[folded] = text
        out[SECTION] = words
        return out

    @cached_property
    def written_spelling(self) -> dict[int, dict[str, str]]:
        """Per column, fold -> the first cell as the sheet wrote it: what an answer keyed by the fold prints. spelling
        keeps the cleaned text, which a read-as line quotes."""
        out: dict[int, dict[str, str]] = {}
        for position in range(len(self.table.columns)):
            words: dict[str, str] = {}
            for text, folded in zip(self.written[position], self.folds[position]):
                if folded is not None and folded not in words:
                    words[folded] = text
            out[position] = words
        out[SECTION] = self.spelling[SECTION]
        return out

    @cached_property
    def column_order(self) -> tuple[int, ...]:
        """Positions in an order read from each column's words and distinct cells, not from where it stands. Two columns tying here hold the same words and cells, so either order reads the same."""
        keys = []
        for column in self.table.columns:
            cells = sorted({value for value in self.folds[column.position] if value is not None})
            keys.append((" ".join(column.name.split()).casefold(), column.kind, hashlib.blake2b("|".join(cells).encode("utf-8", "surrogatepass"), digest_size=8).hexdigest(), column.position))
        return tuple(key[-1] for key in sorted(keys))

    @cached_property
    def column_rank(self) -> dict[int, int]:
        """A column's place in column_order: what ties break on, so a tie never follows the sheet's order. The section sorts first."""
        return {SECTION: -1, **{position: at for at, position in enumerate(self.column_order)}}

    def is_date_column(self, position: int) -> bool:
        """A column conditions read as days: the dates and periods the file types as such (a date format, a written
        date). A column of plain numbers is not read as days by its header's words."""
        return self.table.columns[position].kind in (V_DATE, V_PERIOD)

    def condition_mask(self, condition: Condition) -> int:
        """The lines a condition keeps, as a bitmask over self.lines. The same reading the SQL makes: tests hold the two together."""
        return _mask(i for i in range(len(self.lines)) if self._holds(condition, i))

    def _holds(self, condition: Condition, i: int) -> bool:
        if isinstance(condition, Equals):
            if isinstance(condition.value, (int, float)):
                number = self.numbers[condition.column.position][i]
                return number is not None and number == condition.value
            return self._fold_of(condition.column.position, i) == fold(condition.value)
        if isinstance(condition, OneOf):
            if condition.numbers:
                return self.numbers[condition.column.position][i] in condition.values
            return self._fold_of(condition.column.position, i) in {fold(value) for value in condition.values}
        if isinstance(condition, NotEquals):
            text = self._fold_of(condition.column.position, i)
            return text is not None and text != fold(condition.value)
        if isinstance(condition, Contains):
            text = self._fold_of(condition.column.position, i)
            if text is None:
                return False
            want = fold(condition.text)
            return want in text if condition.anchor == ANYWHERE else text.startswith(want) if condition.anchor == STARTS else text.endswith(want) if condition.anchor == ENDS else False
        if isinstance(condition, InPeriod):
            day = self.days[condition.column.position][i]
            if day is None:
                return False
            span = condition.span
            if span.start is None:
                return int(day[5:7]) == span.month
            return span.start <= day <= (span.end or span.start)
        if isinstance(condition, InYears):
            number = self.numbers[condition.column.position][i]
            return number is not None and condition.first <= number <= condition.last
        if isinstance(condition, Compare):
            number = self.numbers[condition.column.position][i]
            return number is not None and _compared(number, condition.op, condition.number)
        if isinstance(condition, CompareColumns):
            a, b = self.numbers[condition.column.position][i], self.numbers[condition.other.position][i]
            return a is not None and b is not None and _compared(a, condition.op, b)
        if isinstance(condition, CompareLine):
            number, anchor = self.numbers[condition.column.position][i], self.line_number(condition)
            return number is not None and anchor is not None and self._fold_of(condition.key.position, i) != fold(condition.value) and _compared(number, condition.op, anchor)
        if isinstance(condition, IsBlank):
            filled = is_filled(self.lines[i].cells.get(condition.column.position))
            return filled if condition.filled else not filled
        if isinstance(condition, IsNumber):
            return self.numbers[condition.column.position][i] is not None
        if isinstance(condition, InSection):
            if condition.partial:
                return fold(condition.text) in (self.section_paths[i] or "")
            return f"| {fold(condition.text)} |" in (self.section_paths[i] or "")
        if isinstance(condition, OnLine):
            if condition.role is not None and self.lines[i].role != condition.role:
                return False
            want = fold(condition.text)
            return want in self.label_folds[i] if want else not self.label_folds[i]
        raise TypeError(f"unknown condition: {type(condition).__name__}")

    def line_number(self, condition: CompareLine) -> float | None:
        """The number a CompareLine compares with: its column's cell on the first data line whose key is its value."""
        want = fold(condition.value)
        for i, data in enumerate(self.is_data):
            if data and self._fold_of(condition.key.position, i) == want:
                return self.numbers[condition.column.position][i]
        return None

    def _fold_of(self, position: int, i: int) -> str | None:
        return self.section_folds[i] if position == SECTION else self.folds[position][i]


def _compared(a: float, op: str, b: float) -> bool:
    return a >= b if op == GE else a > b if op == GT else a <= b if op == LE else a < b if op == LT else a != b if op == NE else a == b if op == EQ else False


def _mask(indices) -> int:
    out = 0
    for i in indices:
        out |= 1 << i
    return out


def _notes_table(tab: str, notes: list, index: int) -> Table | None:
    """A tab's label: value notes as a table of two columns. The label cell traces to the note's address, the value to its own."""
    lines = []
    for note in notes:
        if note.value is None:
            continue
        address_letters = "".join(ch for ch in note.address if ch.isalpha())
        col = 0
        for ch in address_letters:
            col = col * 26 + ord(ch.upper()) - 64
        label = Cell(note.row, col, note.label, V_TEXT, note.label)
        lines.append(Line(tab=tab, row=note.row, role=RECORD, cells={0: label, 1: note.value}, label=note.label))
    if not lines:
        return None
    columns = [Column(position=0, col=0, letter="", name="", role=LABEL, kind=V_TEXT, filled=len(lines), distinct=len(lines)),
               Column(position=1, col=0, letter="", name="", role=NOTE, kind=V_TEXT, filled=len(lines), distinct=len(lines))]
    return Table(tab=tab, index=index, name=tab, columns=columns, lines=lines, printed=[], header_rows=(), first_row=lines[0].row, last_row=lines[-1].row, context=Context(tab=tab))


class Catalog:
    """Every table of one compiled workbook, in a stable order: compiled tables, then families, then notes.

    ``identities``: (table id, position) -> (table id, position) of a column that holds another column's values, the
    one it holds them from at the end of its chain: a key the request's links find and each column a key reaches
    (engine/program/connected.py connect). A compiled workbook has none."""

    def __init__(self, tables: list[CatalogTable], name: str = "", identities: dict | None = None):
        self.name = name
        self.tables = tables
        self.by_id = {table.id: table for table in tables}
        self.identities: dict[tuple[str, int], tuple[str, int]] = dict(identities or {})

    @classmethod
    def from_model(cls, model: SheetModel) -> "Catalog":
        tables: list[CatalogTable] = []
        count = 0
        for tab in model.tabs:
            for table in tab.tables:
                tables.append(CatalogTable(f"t{count}", table, KIND_TABLE))
                count += 1
        for at, family in enumerate(model.families):
            tables.append(CatalogTable(f"f{at}", family.table, KIND_FAMILY, bool(getattr(family, "adds_twice", False))))
        for at, tab in enumerate(model.tabs):
            notes = _notes_table(tab.name, tab.notes, 0)
            if notes is not None:
                tables.append(CatalogTable(f"n{at}", notes, KIND_NOTES))
        return cls(tables, model.name)

    def table(self, table_id: str) -> CatalogTable:
        return self.by_id[table_id]

    def column_kind(self, column: Col) -> str:
        if column.position == SECTION:
            return V_TEXT
        return self.table(column.table).columns[column.position].kind


__all__ = ["Catalog", "CatalogTable", "KIND_FAMILY", "KIND_NOTES", "KIND_TABLE"]
