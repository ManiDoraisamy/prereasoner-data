"""What a chosen program shows a person: the read-as sentence in the sheet's own words, the exact cells used, an equivalent spreadsheet formula, and the named contributions behind it.

The cells and the formula come from the same lines the query keeps, so a person can check the answer by eye or by pasting the formula. A section or a printed line has no plain criterion in a sheet, so a formula over such lines lists the kept cells; a printed line is cited by its own cell.
"""
from __future__ import annotations

from dataclasses import dataclass

from engine.program.catalog import Catalog, CatalogTable, KIND_FAMILY
from engine.program.index import index_of
from engine.program.language import (LineOrder, Words, Across, Agg, Breakdown, Calc, Check, Col, OneOf, Compare, CompareColumns, CompareLine, Condition, Contains, DateOf, Equals, InPeriod, InYears, InSection, IsBlank, IsNumber, Measure, NotEquals, PercentOf, Program, Rank, RowArithmetic, SECTION, Show, Size, addresses_printed, answer_type_of, measure_columns)
from engine.program.names import (A_PERCENT, AFTER, ANY, SAME, ANYWHERE, EQ, GRAIN_CALENDAR_MONTH, GRAIN_MONTH, GRAIN_YEAR, EVERY, IS, NO, AVG, COUNT, COUNT_DISTINCT, LONGEST_RUN, ENDS, ENTITY_GROUP, ENTITY_LINE, GE, GT, HIGHEST, LE, LT, MAX, MIN, MINUS, NE, OVER, PLUS, STARTS, SUM, TIMES)
from engine.program.scoring import ScoredProgram
from engine.program.semantics import fold, number_text, share_forms
from engine.program.store import RequestTables
from engine.sheet.formulas import column_letter

_GRAIN_WORDS = {GRAIN_MONTH: "month", GRAIN_YEAR: "year", GRAIN_CALENDAR_MONTH: "calendar month"}
_AGG_WORDS = {SUM: "total", AVG: "average", MIN: "lowest", MAX: "highest", COUNT: "number of lines", COUNT_DISTINCT: "number of different",
              LONGEST_RUN: "most lines in a row"}
_COMPARE_WORDS = {EQ: "equal to", GE: "at least", GT: "above", LE: "at most", LT: "below", NE: "other than"}
_OP_WORDS = {PLUS: "plus", MINUS: "minus", TIMES: "times", OVER: "over"}
_VERDICT_WORDS = {GT: "above", GE: "at least", LT: "below", LE: "at most", EQ: "equal to", NE: "different from"}
_VERDICT_FORMULA = {GT: ">", GE: ">=", LT: "<", LE: "<=", EQ: "=", NE: "<>"}
_SHEET_FUNCTION = {SUM: "SUM", AVG: "AVERAGE", MIN: "MIN", MAX: "MAX"}
_IFS = {SUM: "SUMIFS", AVG: "AVERAGEIFS", MIN: "MINIFS", MAX: "MAXIFS"}
_MAX_CELLS = 2000
_MAX_LINES_ADDED = 30


@dataclass(frozen=True, slots=True)
class Explanation:
    """The proof beside an answer."""
    read_as: str
    cells: tuple[str, ...]
    formula: str
    contributions: tuple[dict, ...]
    also_stated: tuple[str, ...] = ()      # a share may be stated either way: the fraction is the answer, this is its percent

    def record(self) -> dict:
        return {"read_as": self.read_as, "cells": list(self.cells), "formula": self.formula, "contributions": list(self.contributions), "also_stated": list(self.also_stated)}


# ----------------------------------------------------------------------------------------------------------- words
def _name(catalog: Catalog, column: Col) -> str:
    return catalog.table(column.table).column_name(column.position)


def header(catalog: Catalog, column: Col) -> str:
    """A column's header as an answer shows it: a column the request's key links reach, which no sheet column holds
    ("order_id → customer_id → customer_name", engine/program/connected.py), by the header of the sheet column whose
    values it holds (Catalog.identities); every other column, a sheet's own or one reached in the knowledgebase ("city →
    country"), by its name."""
    table = catalog.table(column.table)
    root = catalog.identities.get((column.table, column.position))
    if root is None or column.position == SECTION or table.table.columns[column.position].col >= 0:
        return table.column_name(column.position)
    return catalog.table(root[0]).column_name(root[1])


def _headers(catalog: Catalog, columns) -> tuple[str, ...]:
    """header() of each column, a header another column already shows replaced by the column's own name."""
    names: list[str] = []
    for column in columns:
        name = header(catalog, column)
        names.append(_name(catalog, column) if name in names else name)
    return tuple(names)


def _number_words(catalog: Catalog, column: Col, number: float) -> str:
    """A number a condition compares with, as its column writes it: a column of times as a time ("5:00", "1:02:05"),
    the seconds it counts in; any other as the number."""
    if column.position not in catalog.table(column.table).durations or number < 0:
        return f"{number:g}"
    hours, rest = divmod(number, 3600)
    minutes, seconds = divmod(rest, 60)
    tail = f"{seconds:05.2f}".rstrip("0").rstrip(".") if seconds % 1 else f"{int(seconds):02d}"
    if len(tail.split(".")[0]) < 2:
        tail = "0" + tail
    return f"{int(hours)}:{int(minutes):02d}:{tail}" if hours else f"{int(minutes)}:{tail}"


def _value_words(catalog: Catalog, column: Col, value: str | float) -> str:
    if isinstance(value, (int, float)):
        return f"{value:g}"
    words = catalog.table(column.table).spelling.get(column.position, {})
    return words.get(fold(value), str(value))


def _condition_words(catalog: Catalog, condition: Condition) -> str:
    if isinstance(condition, Equals):
        return f'{_name(catalog, condition.column)} is "{_value_words(catalog, condition.column, condition.value)}"'
    if isinstance(condition, OneOf):
        named = " or ".join(f'"{_value_words(catalog, condition.column, value)}"' for value in condition.values)
        return f"{_name(catalog, condition.column)} is {named}"
    if isinstance(condition, NotEquals):
        return f'{_name(catalog, condition.column)} is other than "{_value_words(catalog, condition.column, condition.value)}"'
    if isinstance(condition, Contains):
        where = {ANYWHERE: "holds", STARTS: "starts with", ENDS: "ends with"}[condition.anchor]
        return f'{_name(catalog, condition.column)} {where} "{condition.text}"'
    if isinstance(condition, InPeriod):
        return f"{_name(catalog, condition.column)} falls in {condition.span.name}"
    if isinstance(condition, InYears):
        years = (f"{condition.first}" if condition.first == condition.last else f"at most {condition.last}" if condition.first <= 1
                 else f"at least {condition.first}" if condition.last >= 9999 else f"from {condition.first} to {condition.last}")
        return f"{_name(catalog, condition.column)} is {years}"
    if isinstance(condition, Compare):
        return f"{_name(catalog, condition.column)} is {_COMPARE_WORDS[condition.op]} {_number_words(catalog, condition.column, condition.number)}"
    if isinstance(condition, CompareColumns):
        return f"{_name(catalog, condition.column)} is {_COMPARE_WORDS[condition.op]} {_name(catalog, condition.other)}"
    if isinstance(condition, CompareLine):
        named = f'the line where {_name(catalog, condition.key)} is "{_value_words(catalog, condition.key, condition.value)}"'
        if isinstance(condition.column, LineOrder):
            return f"listed {'before' if condition.op == LT else 'after'} {named}"
        name = _name(catalog, condition.column)
        return f'{name} is {_COMPARE_WORDS[condition.op]} the {name} of {named}'
    if isinstance(condition, IsBlank):
        return f"{_name(catalog, condition.column)} is {'filled' if condition.filled else 'blank'}"
    if isinstance(condition, IsNumber):
        return f"{_name(catalog, condition.column)} is a number"
    if isinstance(condition, InSection):
        return f'under "{condition.text}"'
    if condition.text:
        return f'on the line "{condition.text}"'
    return f"on the unlabelled {condition.role or 'printed'} line"


def _where_words(catalog: Catalog, where: tuple[Condition, ...]) -> str:
    return " and ".join(_condition_words(catalog, condition) for condition in where)


def _measure_words(catalog: Catalog, measure: Measure) -> str:
    if isinstance(measure, Col):
        return _name(catalog, measure)
    if isinstance(measure, LineOrder):
        return "where the line stands"
    if isinstance(measure, Words):
        return f"the number of words of {_name(catalog, measure.column)}"
    if isinstance(measure, DateOf):
        return _name(catalog, measure.column)
    if isinstance(measure, Size):
        return f"the size of {_measure_words(catalog, measure.measure)}"
    if isinstance(measure, PercentOf):
        return f"{_name(catalog, measure.rate)} % of {_measure_words(catalog, measure.measure)}"
    if isinstance(measure, Across):
        names = [_name(catalog, column) for column in measure.columns]
        return f"{names[0]} to {names[-1]} added across" if len(names) > 3 else " plus ".join(names)
    return f"{_measure_words(catalog, measure.left)} {_OP_WORDS[measure.op]} {_measure_words(catalog, measure.right)}"


FAMILY_TABS_NAMED = 3     # a family of more member tabs is named by its first and last


def _in_tab(table: CatalogTable) -> str:
    """Where a program reads. A family merging several tabs reads all of them, and says so: named by its first tab
    alone it read as that one tab ("SI" for SI and FF together)."""
    tabs = list(dict.fromkeys(line.tab for line in table.lines)) if table.kind == KIND_FAMILY else []
    if len(tabs) < 2:
        return f'on "{table.tab}"'
    if len(tabs) <= FAMILY_TABS_NAMED:
        return "on " + " and ".join(f'"{tab}"' for tab in tabs) + " together"
    return f'on the {len(tabs)} tabs "{tabs[0]}" to "{tabs[-1]}" together'


def _check_words(program: Check, catalog: Catalog) -> str:
    if program.quantifier in (ANY, NO, EVERY):
        table = catalog.table(program.left.table)
        place = _in_tab(table)
        if program.quantifier == EVERY:
            base = _where_words(catalog, program.right.where)
            extra = _where_words(catalog, tuple(c for c in program.left.where if c not in program.right.where))
            return f"whether every line {place}" + (f" where {base}" if base else "") + f" also has {extra}"
        where = _where_words(catalog, program.left.where)
        return f"whether there is {'any' if program.quantifier == ANY else 'no'} line {place}" + (f" where {where}" if where else "")
    if program.quantifier == SAME:
        return f"whether {read_as(program.left, catalog)} is also {read_as(program.right, catalog)}"
    if program.quantifier == IS and program.text is not None:
        return f'whether {read_as(program.left, catalog)} is {"not " if program.op == NE else ""}"{program.text}"'
    other = number_text(program.number) if program.number is not None else read_as(program.right, catalog)
    if program.said is not None:
        return f"'{program.said[0]}' if {read_as(program.left, catalog)} is {_VERDICT_WORDS[GT]} {other}, '{program.said[1]}' if {_VERDICT_WORDS[LT]}"
    return f"whether {read_as(program.left, catalog)} is {_VERDICT_WORDS[program.op]} {other}"


def _absent_words(program: Show, catalog: Catalog) -> str:
    """A listing's absence: whose key no line holds, on its own table or in the column of another that holds it."""
    absent = program.absent
    if absent is None:
        return ""
    other = "" if absent.by.table == program.table else f" {_in_tab(catalog.table(absent.by.table))}"
    held = "" if absent.by == absent.key else f" in {_name(catalog, absent.by)}"
    return f" whose {_name(catalog, absent.key)} no line{other} holds{held} where {_where_words(catalog, absent.where)}"


def absent_lines(program: Show, catalog: Catalog, kept: list[int]) -> list[int]:
    """The kept lines a listing's absence keeps: a filled key no data line of its table holding the conditions holds,
    as the query reads it (NOT IN over the folded keys)."""
    absent = program.absent
    if absent is None:
        return kept
    other = catalog.table(absent.by.table)
    index = index_of(other)
    mask = other.data_mask
    for condition in absent.where:
        mask &= index.mask(condition)
    held = {other.folds[absent.by.position][i] for i in range(len(other.lines)) if mask >> i & 1} - {None}
    keys = catalog.table(program.table).folds[absent.key.position]
    return [i for i in kept if keys[i] is not None and keys[i] not in held]


def in_header_words(program: Program, catalog: Catalog) -> str:
    """The program's read-as line in the sheet's own header words: a column the compiler named apart from an earlier
    one of the same header by its sheet letter ("Amount [C]", engine/sheet/compiler.py) by that header alone. Two
    readings that differ only in which of two same-headed columns they read then read alike, as a question naming the
    header reads them (closed.choose_answer asks it back)."""
    from engine.program.language import tables_of
    line = read_as(program, catalog)
    for table_id in tables_of(program):
        for column in catalog.table(table_id).table.columns:
            words = " ".join(column.path)
            if words and column.letter and column.name == f"{words} [{column.letter}]":
                line = line.replace(column.name, words)
    return line


def read_as(program: Program, catalog: Catalog) -> str:
    """The program in the sheet's own words."""
    if isinstance(program, Check):
        return _check_words(program, catalog)
    if isinstance(program, Calc):
        sentence = f"{read_as(program.left, catalog)}, {_OP_WORDS[program.op]} {read_as(program.right, catalog)}"
        return f"the size of ({sentence})" if program.magnitude else sentence
    table = catalog.table(program.table)
    place = _in_tab(table)
    if isinstance(program, Show):
        shown = ", ".join(_name(catalog, column) for column in program.shown)
        where = _where_words(catalog, program.where)
        if program.every:     # the wording the readers' ask-back used ("every column of subscriptions")
            return f"every column of {table.tab or table.id}, line by line" + (f" where {where}" if where else "")
        if program.listing and len(program.shown) > 1:
            pairs = " and ".join(_name(catalog, column) for column in program.shown)
            return f"the different combinations of {pairs} {place}" + (f" where {where}" if where else "")
        if program.listing:
            return f"the different {shown} {place}" + (f" where {where}" if where else "") + _absent_words(program, catalog)
        if program.step is not None:
            return f"{shown} {place} on the line {program.step} the line where {where}"
        return f"{shown} {place} where {where}"
    if isinstance(program, Breakdown):
        where = _where_words(catalog, program.where)
        head = "the number of lines" if program.function == COUNT else f"the {_AGG_WORDS[program.function]} of {_measure_words(catalog, program.measure)}"
        each = " and ".join(f"{_GRAIN_WORDS[program.grain]} of {_name(catalog, column)}" if program.grain is not None and at == 0 else _name(catalog, column) for at, column in enumerate(program.groups))
        return f"{head} for each {each} {place}" + (f" where {where}" if where else "")
    if isinstance(program, Agg):
        where = _where_words(catalog, program.where)
        if program.function in (COUNT, LONGEST_RUN):
            head = f"the {_AGG_WORDS[program.function]} {place}"
        elif program.function == COUNT_DISTINCT:
            head = f"the number of different {_name(catalog, program.measure)} {place}"
        else:
            head = f"the {_AGG_WORDS[program.function]} of {_measure_words(catalog, program.measure)} {place}"
        if program.magnitude:
            head = f"the size of {head}"
        return head + (f" where {where}" if where else "") + _units_added(program, table, catalog)
    way = "highest" if program.direction == HIGHEST else "lowest"
    where = _where_words(catalog, program.where)
    tail = f" where {where}" if where else ""
    if program.entity == ENTITY_LINE and not program.shown:
        return f"the {program.count} {way} of {_measure_words(catalog, program.measure)} {place}{tail}"
    if program.entity == ENTITY_LINE and isinstance(program.measure, LineOrder):     # the first or the last lines as the sheet lists them
        end = "first" if program.direction != HIGHEST else "last"
        shown = ", ".join(_name(catalog, column) for column in program.shown)
        lines = f"the {end} {program.count} lines" if program.count > 1 else f"the {end} line"
        return f"{shown} of {lines} {place}{tail}"
    if program.entity == ENTITY_LINE:
        shown = ", ".join(_name(catalog, column) for column in program.shown)
        lines = f"the {program.count} lines" if program.count > 1 else "the line"
        return f"{shown} of {lines} with the {way} {_measure_words(catalog, program.measure)} {place}{tail}"
    if program.entity == ENTITY_GROUP:
        what = "the most lines" if program.function == COUNT and program.direction == HIGHEST else "the fewest lines" if program.function == COUNT else f"the {way} {_AGG_WORDS[program.function]} of {_measure_words(catalog, program.measure)}"
        if program.by_size:
            what += " by size"
        keyed = f"{_GRAIN_WORDS[program.grain]} of {_name(catalog, program.key)}" if program.grain is not None else _name(catalog, program.key)
        which = f"the {program.count} values of the {keyed}" if program.count > 1 and program.grain is not None else f"the {program.count} values of {keyed}" if program.count > 1 else f"the {keyed}"
        return f"{which} with {what} {place}{tail}"
    names = [table.columns[position].name for position in program.peers]
    span = f"{names[0]} to {names[-1]}" if len(names) > 3 else ", ".join(names)
    if program.mark is not None:
        return f'which of {span} holds the most "{program.mark}" {place}{tail}'
    return f"which of {span} is {way}{' by size' if program.by_size else ''} {place}{tail}"


def _figure_name(program: Agg | Breakdown | Rank, catalog: Catalog) -> str:
    if program.function in (COUNT, LONGEST_RUN):
        return _AGG_WORDS[program.function]
    if program.function == COUNT_DISTINCT:
        return f"{_AGG_WORDS[COUNT_DISTINCT]} {_name(catalog, program.measure)}"
    return f"{_AGG_WORDS[program.function]} {_measure_words(catalog, program.measure)}"


def answer_columns(program: Program, catalog: Catalog) -> tuple[str, ...]:
    """The names of a program's answer columns, in the order its query shows them (engine/program/lowering.py), in the
    sheet's own words: a shown or key column by its header, the figure by what it computes."""
    if isinstance(program, (Check, Calc)):
        return ("answer",)
    if isinstance(program, Show):
        return _headers(catalog, program.shown)
    if isinstance(program, Agg):
        return (_figure_name(program, catalog),)
    if isinstance(program, Breakdown):
        keys = _headers(catalog, program.groups)
        return tuple(f"{_GRAIN_WORDS[program.grain]} of {key}" if program.grain is not None and at == 0 else key for at, key in enumerate(keys)) + (_figure_name(program, catalog),)
    if program.entity == ENTITY_LINE:
        if isinstance(program.measure, LineOrder):     # where the lines stand picked them; no figure of it is shown
            return _headers(catalog, program.shown)
        return _headers(catalog, program.shown) + (_measure_words(catalog, program.measure),)
    if program.entity == ENTITY_GROUP:
        key = header(catalog, program.key)
        return (f"{_GRAIN_WORDS[program.grain]} of {key}" if program.grain is not None else key, _figure_name(program, catalog))
    return ("column", f'count of "{program.mark}"' if program.mark is not None else "total")


# ----------------------------------------------------------------------------------------------------------- cells
def _units_added(program: Agg, table: CatalogTable, catalog: Catalog) -> str:
    """A figure over a table whose lines name their currency, no condition fixing one, adds amounts in several
    currencies together: the read-as says so, as the person and the chooser should both see it. A measure column that
    states its own currency (TableIndex.states_currency) adds none."""
    index = index_of(table)
    unit = index.currency_column
    if unit is None or program.function not in _SHEET_FUNCTION:
        return ""
    if program.measure is not None and index.stated_currency(program.measure) is not None:
        return ""
    if any(isinstance(condition, (Equals, OneOf)) and condition.column.position == unit for condition in program.where):
        return ""
    return f" (all {_name(catalog, Col(table.id, unit))} together)"


def kept_lines(program: Program, table: CatalogTable, index) -> list[int]:
    """The line positions a program's conditions keep, in sheet order: the same lines its query reads."""
    every_line = addresses_printed(program.where) or (isinstance(program, Show) and not program.listing) or (isinstance(getattr(program, "measure", None), Col) and program.measure.position in table.printed_only_columns)
    mask = table.all_mask if every_line else table.data_mask
    for condition in program.where:
        mask &= index.mask(condition)
    return [i for i in range(len(table.lines)) if mask >> i & 1]


def _read_line(program: Show, table: CatalogTable, kept: list[int]) -> int | None:
    """The line a read takes: the shortest matching label, then the first in sheet order; for a neighbouring line,
    the data line nearest the first kept line, below it (after) or above it (before), as its query reads."""
    if not kept:
        return None
    if program.step is not None:
        data = [i for i in range(len(table.lines)) if table.data_mask >> i & 1]
        beside = [i for i in data if i > kept[0]] if program.step == AFTER else [i for i in data if i < kept[0]][::-1]
        return beside[0] if beside else None
    if addresses_printed(program.where):
        return min(kept, key=lambda i: (len(table.label_folds[i]), i))
    return kept[0]


def _cell_address(table: CatalogTable, i: int, position: int) -> str | None:
    if position == SECTION:
        return None
    cell = table.lines[i].cells.get(position)
    return None if cell is None else cell.address


def _with_tab(table: CatalogTable, i: int, address: str) -> str:
    return f"'{table.lines[i].tab}'!{address}"


def cells_used(program: Program, tables: RequestTables, rows: tuple[tuple, ...]) -> tuple[str, ...]:
    """The exact cells behind an answer: the cell a read takes; the measure cells of the kept lines; for a rank, the top line's, the top group's or the top peer column's."""
    if isinstance(program, Calc):
        return tuple(dict.fromkeys(cells_used(program.left, tables, ()) + cells_used(program.right, tables, ())))
    if isinstance(program, Check):
        sides = (program.left,) + ((program.right,) if program.right is not None else ())
        return tuple(dict.fromkeys(cell for side in sides for cell in cells_used(side, tables, ())))
    catalog = tables.catalog
    table = catalog.table(program.table)
    index = index_of(table)
    kept = kept_lines(program, table, index)
    out: list[str] = []

    def add(i: int, position: int) -> None:
        address = _cell_address(table, i, position)
        if address is not None:
            out.append(_with_tab(table, i, address))

    if isinstance(program, Show):
        if program.every:
            for i in kept:
                for column in program.shown:
                    add(i, column.position)
        elif program.listing:
            for i in absent_lines(program, catalog, kept):
                if all(table.folds[column.position][i] is not None for column in program.shown):     # a line blank in a shown column is in no combination
                    for column in program.shown:
                        add(i, column.position)
        else:
            line = _read_line(program, table, kept)
            if line is not None:
                for column in program.shown:
                    add(line, column.position)
    elif isinstance(program, (Agg, Breakdown)):
        columns = measure_columns(program.measure) if isinstance(program.measure, (Col, RowArithmetic, PercentOf, Size, DateOf, Across, Words)) else ()
        if isinstance(program, Breakdown):      # a line with a blank key or unit is in no group
            folds = [table.days[column.position] if getattr(program, "grain", None) is not None and at == 0 else table.section_folds if column.position == SECTION else table.folds[column.position] for at, column in enumerate(program.groups)]
            kept = [i for i in kept if all(held[i] is not None for held in folds)]
        for i in kept:
            for column in columns:
                add(i, column.position)
            if not columns and table.lines[i].cells:
                add(i, min(table.lines[i].cells))      # a count: one cell of each line it counts
    elif program.entity == ENTITY_LINE and isinstance(program.measure, LineOrder):     # the first or last kept lines, as listed
        data = [i for i in kept if table.is_data[i]]
        for i in (data[:program.count] if program.direction != HIGHEST else data[-program.count:]):
            for column in program.shown:
                add(i, column.position)
    elif program.entity == ENTITY_LINE:
        columns = measure_columns(program.measure)
        reads = {column.position: table.words(column.position) if isinstance(program.measure, Words) else table.numbers[column.position] for column in columns}
        top = {str(row[-1]) for row in rows}
        for i in kept:
            if any(reads[column.position][i] is not None for column in columns) and _line_is_top(program, table, i, top):
                for column in (*program.shown, *columns):
                    add(i, column.position)
    elif program.entity == ENTITY_GROUP:
        winners = {fold(row[0]) for row in rows}
        folds = table.section_folds if program.key.position == SECTION else table.folds[program.key.position]
        columns = measure_columns(program.measure) if program.measure is not None else (program.key,)
        for i in kept:
            if folds[i] in winners:
                for column in columns:
                    add(i, column.position)
    else:
        winners = {str(row[0]) for row in rows}
        for position in program.peers:
            if table.columns[position].name in winners:
                for i in kept:
                    add(i, position)
    return tuple(dict.fromkeys(out))


def _line_is_top(program: Rank, table: CatalogTable, i: int, top: set[str]) -> bool:
    """Whether a line is one a line rank kept: its measure, read the way the query reads it, is the top value."""
    value = _line_value(program.measure, table, i)
    if value is None:
        return False
    for shown in top:
        try:
            if isinstance(value, str):
                if value == shown:
                    return True
            elif abs(float(shown) - value) <= 1e-9 * max(1.0, abs(value)):
                return True
        except ValueError:
            continue
    return False


def _line_value(measure: Measure, table: CatalogTable, i: int):
    if isinstance(measure, Col):
        return table.numbers[measure.position][i]
    if isinstance(measure, Words):
        return table.words(measure.column.position)[i]
    if isinstance(measure, DateOf):
        return table.days[measure.column.position][i]
    if isinstance(measure, Size):
        inner = _line_value(measure.measure, table, i)
        return None if inner is None else abs(inner)
    if isinstance(measure, PercentOf):
        inner, rate = _line_value(measure.measure, table, i), table.numbers[measure.rate.position][i]
        return None if inner is None or rate is None else inner * rate / 100.0
    if isinstance(measure, Across):
        held = [table.numbers[column.position][i] for column in measure.columns]
        return None if all(value is None for value in held) else sum(value or 0.0 for value in held)
    left, right = _line_value(measure.left, table, i), _line_value(measure.right, table, i)
    if measure.op in (PLUS, MINUS):
        if left is None and right is None:
            return None
        return (left or 0.0) + (right or 0.0) if measure.op == PLUS else (left or 0.0) - (right or 0.0)
    if left is None or right is None:
        return None
    return left * right if measure.op == TIMES else (left / right if right else None)


def compress(addresses: tuple[str, ...]) -> tuple[str, ...]:
    """Runs of one column as a range: 'T'!D5:D9. Stable and exact, so the list stays short where the lines are contiguous."""
    by_column: dict[tuple[str, str], list[int]] = {}
    order: list[tuple[str, str]] = []
    for address in addresses:
        tab, cell = address.rsplit("!", 1)
        letters = "".join(ch for ch in cell if ch.isalpha())
        key = (tab, letters)
        if key not in by_column:
            order.append(key)
            by_column[key] = []
        by_column[key].append(int(cell[len(letters):]))
    out: list[str] = []
    for key in order:
        tab, letters = key
        rows = sorted(set(by_column[key]))
        start = previous = rows[0]
        for row in rows[1:] + [None]:
            if row is not None and row == previous + 1:
                previous = row
                continue
            out.append(f"{tab}!{letters}{start}" if start == previous else f"{tab}!{letters}{start}:{letters}{previous}")
            start = previous = row
    return tuple(out)


# --------------------------------------------------------------------------------------------------------- formulas
def _range(table: CatalogTable, position: int) -> str:
    """A column's whole range on its tab, from its first line to its last."""
    column = table.columns[position]
    letter = column.letter or column_letter(column.col)
    rows = [line.row for line in table.lines if line.tab == table.tab] or [table.table.first_row]
    first, last = min(rows), max(rows)
    return f"'{table.tab}'!{letter}{first}:{letter}{last}"


def _has_printed_inside(table: CatalogTable) -> bool:
    return any(not data for data in table.is_data)


def _plain_range(table: CatalogTable) -> bool:
    """Whether a column's range from first line to last holds this table's data lines and nothing else: no printed line inside it, and not a family spread over tabs."""
    return table.kind != KIND_FAMILY and not _has_printed_inside(table)


def _criteria(catalog: Catalog, where: tuple[Condition, ...]) -> list[str] | None:
    """Conditions as SUMIFS criteria, or None when one has no plain criterion in a sheet."""
    out: list[str] = []
    for condition in where:
        if isinstance(condition, Equals):
            table = catalog.table(condition.column.table)
            range_ = _range(table, condition.column.position)
            value = f"{condition.value:g}" if isinstance(condition.value, (int, float)) else '"' + _value_words(catalog, condition.column, condition.value).replace('"', '""') + '"'
            out.extend((range_, value))
        elif isinstance(condition, Contains):
            table = catalog.table(condition.column.table)
            pattern = {ANYWHERE: f"*{condition.text}*", STARTS: f"{condition.text}*", ENDS: f"*{condition.text}"}[condition.anchor]
            out.extend((_range(table, condition.column.position), f'"{pattern}"'))
        elif isinstance(condition, NotEquals):
            return None                          # a sheet's "<>X" also passes a blank; this does not, so it has no plain criterion
        elif isinstance(condition, Compare):
            table = catalog.table(condition.column.table)
            op = "<>" if condition.op == NE else condition.op
            out.extend((_range(table, condition.column.position), f'"{op}{condition.number:g}"'))
        elif isinstance(condition, InPeriod) and condition.span.start is not None:
            table = catalog.table(condition.column.table)
            range_ = _range(table, condition.column.position)
            out.extend((range_, f'">={condition.span.start}"', range_, f'"<={condition.span.end or condition.span.start}"'))
        elif isinstance(condition, InYears):
            range_ = _range(catalog.table(condition.column.table), condition.column.position)
            out.extend((range_, f'">={condition.first}"', range_, f'"<={condition.last}"'))
        elif isinstance(condition, IsBlank):
            table = catalog.table(condition.column.table)
            out.extend((_range(table, condition.column.position), '"<>"' if condition.filled else '""'))
        elif isinstance(condition, CompareLine) and not isinstance(condition.column, LineOrder):      # "<>X" passes a blank key, as the line kept out is only X's
            table = catalog.table(condition.column.table)
            range_, keys = _range(table, condition.column.position), _range(table, condition.key.position)
            named = _value_words(catalog, condition.key, condition.value).replace('"', '""')
            op = "<>" if condition.op == NE else condition.op
            out.extend((range_, f'"{op}"&INDEX({range_},MATCH("{named}",{keys},0))', keys, f'"<>{named}"'))
        else:
            return None
    return out


def formula(program: Program, tables: RequestTables) -> str:
    """An equivalent spreadsheet formula a user can paste. A printed line read is its own cell. Where the kept lines cannot be stated as plain criteria (a band, printed lines inside the range, a month of any year, row arithmetic), it adds the kept cells themselves, which is the same number on these cells."""
    catalog = tables.catalog
    if isinstance(program, Check):
        left = formula(program.left, tables).lstrip("=")
        if program.quantifier in (ANY, NO):
            return f"=({left}){'>' if program.quantifier == ANY else '='}0"
        right = number_text(program.number) if program.number is not None else f'"{program.text}"' if program.text is not None else f"({formula(program.right, tables).lstrip('=')})"
        if program.said is not None:
            return f'=IF(({left})>{right},"{program.said[0]}",IF(({left})<{right},"{program.said[1]}",""))'
        return f"=({left}){'=' if program.quantifier in (EVERY, SAME) else _VERDICT_FORMULA[program.op]}{right}"
    if isinstance(program, Calc):
        left, right = formula(program.left, tables).lstrip("="), formula(program.right, tables).lstrip("=")
        body = f"({left}) {program.op} ({right})"
        return f"=ABS({body})" if program.magnitude else f"={body}"
    table = catalog.table(program.table)
    if isinstance(program, Show):
        if not program.listing:
            cells = cells_used(program, tables, ())
            return f"={cells[0]}" if len(cells) == 1 else "={" + ",".join(cells) + "}" if cells else "=NA()"
        if program.every:     # every kept line: the table's columns side by side, filtered by the conditions
            tests = _filter_array(catalog, program.where)
            if tests is None or not _plain_range(table):
                return "=NA()"
            body = "HSTACK(" + ",".join(_range(table, column.position) for column in program.shown) + ")"
            return f"=FILTER({body},{tests})" if tests else f"={body}"
        if len(program.shown) > 1:     # the combinations: the shown ranges side by side, the lines filled in all and kept
            tests = _filter_array(catalog, program.where)
            if tests is None or program.absent is not None or not _plain_range(table) or any(column.position == SECTION for column in program.shown):
                return "=NA()"
            ranges = [_range(table, column.position) for column in program.shown]
            keep = "*".join([f'({range_}<>"")' for range_ in ranges] + ([tests] if tests else []))
            return f"=UNIQUE(FILTER(HSTACK({','.join(ranges)}),{keep}))"
        cells = compress(cells_used(program, tables, ()))
        return "=UNIQUE(VSTACK(" + ",".join(cells) + "))" if cells else "=NA()"
    if isinstance(program, Agg):
        return _agg_formula(program, table, tables)
    if isinstance(program, Breakdown):
        return _breakdown_formula(program, table, tables)
    return _rank_formula(program, table, tables)


def _filter_array(catalog: Catalog, where: tuple[Condition, ...]) -> str | None:
    """Conditions as one array of 1s and 0s over a table's ranges (GROUPBY's filter), or None when one has no plain test."""
    tests = []
    for condition in where:
        if isinstance(condition, (Equals, Compare, Contains, InPeriod, InYears, IsBlank)):
            range_ = _range(catalog.table(condition.column.table), condition.column.position)
        if isinstance(condition, Equals):
            value = f"{condition.value:g}" if isinstance(condition.value, (int, float)) else '"' + _value_words(catalog, condition.column, condition.value).replace('"', '""') + '"'
            tests.append(f"({range_}={value})")
        elif isinstance(condition, Compare):
            tests.append(f"({range_}{_VERDICT_FORMULA[condition.op]}{condition.number:g})")
        elif isinstance(condition, Contains):
            tests.append(f'ISNUMBER(SEARCH("{condition.text}",{range_}))')
        elif isinstance(condition, InPeriod) and condition.span.start is not None:
            tests.append(f'({range_}>=DATEVALUE("{condition.span.start}"))*({range_}<=DATEVALUE("{condition.span.end or condition.span.start}"))')
        elif isinstance(condition, InYears):
            tests.append(f"({range_}>={condition.first})*({range_}<={condition.last})")
        elif isinstance(condition, IsBlank):
            tests.append(f'({range_}{"<>" if condition.filled else "="}"")')
        else:
            return None
    return "*".join(tests)


def _breakdown_formula(program: Breakdown, table: CatalogTable, tables: RequestTables) -> str:
    """GROUPBY over the groups' ranges and the measure's, the kept lines as its filter; a band, a printed line inside the
    range or a measure computed line by line has no plain range, and is stated by its read-as and cells."""
    tests = _filter_array(tables.catalog, program.where)
    if tests is None or not _plain_range(table) or any(column.position == SECTION for column in program.groups) or program.grain is not None:
        return "=NA()"
    if program.function == COUNT:
        values, function = _range(table, program.groups[0].position), "COUNTA"
    elif isinstance(program.measure, Col):
        values, function = _range(table, program.measure.position), _SHEET_FUNCTION[program.function]
    else:
        return "=NA()"
    ranges = [_range(table, column.position) for column in program.groups]
    fields = ranges[0] if len(ranges) == 1 else "HSTACK(" + ",".join(ranges) + ")"
    return f"=GROUPBY({fields},{values},{function},0,0" + (f",,{tests})" if tests else ")")


def _agg_formula(program: Agg, table: CatalogTable, tables: RequestTables) -> str:
    catalog = tables.catalog
    if program.function == LONGEST_RUN:     # the longest run of kept lines: FREQUENCY counts the kept rows between misses
        tests = _filter_array(catalog, program.where)
        if not tests or not _plain_range(table):
            return "=NA()"
        rows = f"ROW({_range(table, 0)})"
        return f"=MAX(FREQUENCY(IF({tests},{rows}),IF(NOT({tests}),{rows})))"
    criteria = _criteria(catalog, program.where)
    plain = criteria is not None and _plain_range(table)
    if program.function == COUNT:
        if plain and criteria:
            return f"=COUNTIFS({','.join(criteria)})"
        cells = compress(cells_used(program, tables, ()))
        return "=COUNTA(" + ",".join(cells) + ")" if cells else "=0"
    if program.function == COUNT_DISTINCT:
        cells = compress(cells_used(Show(program.table, (program.measure,), program.where, listing=True), tables, ()))
        return "=COUNTA(UNIQUE(VSTACK(" + ",".join(cells) + ")))" if cells else "=0"
    function = program.function
    if isinstance(program.measure, Col) and plain:
        target = _range(table, program.measure.position)
        body = f"{_IFS[function]}({target},{','.join(criteria)})" if criteria else f"{_SHEET_FUNCTION[function]}({target})"
    elif isinstance(program.measure, (RowArithmetic, PercentOf, Size, Across, Words)):
        body = _arithmetic_formula(program, table, tables)
    else:
        cells = compress(cells_used(program, tables, ()))
        body = f"{_SHEET_FUNCTION[function]}({','.join(cells)})" if cells else "0"
    return f"=ABS({body})" if program.magnitude else f"={body}"


def _arithmetic_formula(program: Agg, table: CatalogTable, tables: RequestTables) -> str:
    """Row arithmetic adds line by line: the kept lines written out, one term each, so it reads as the sheet does."""
    terms = []
    for i in kept_lines(program, table, index_of(table))[:_MAX_CELLS]:
        term = _line_term(program.measure, table, i)
        if term is not None:
            terms.append(term)
    function = _SHEET_FUNCTION[program.function]
    return f"{function}({','.join(terms)})" if terms else "0"


def _words_formula(cells: str) -> str:
    """A cell's number of words as a sheet counts them: its spaces, trimmed, plus one."""
    return f'(LEN(TRIM({cells}))-LEN(SUBSTITUTE(TRIM({cells})," ",""))+1)'


def _line_term(measure: Measure, table: CatalogTable, i: int) -> str | None:
    if isinstance(measure, Words):
        address = _cell_address(table, i, measure.column.position)
        return None if address is None or table.words(measure.column.position)[i] is None else _words_formula(_with_tab(table, i, address))
    if isinstance(measure, Col):
        address = _cell_address(table, i, measure.position)
        return None if address is None or table.numbers[measure.position][i] is None else _with_tab(table, i, address)
    if isinstance(measure, Size):
        inner = _line_term(measure.measure, table, i)
        return None if inner is None else f"ABS({inner})"
    if isinstance(measure, PercentOf):
        inner, rate = _line_term(measure.measure, table, i), _line_term(measure.rate, table, i)
        return None if inner is None or rate is None else f"({inner}*{rate}/100)"
    if isinstance(measure, DateOf):
        return _line_term(measure.column, table, i)
    if isinstance(measure, Across):
        terms = [term for term in (_line_term(column, table, i) for column in measure.columns) if term is not None]
        return "(" + "+".join(terms) + ")" if terms else None
    left, right = _line_term(measure.left, table, i), _line_term(measure.right, table, i)
    if measure.op in (PLUS, MINUS):
        if left is None and right is None:
            return None
        return f"({left or 0}{measure.op}{right or 0})"
    return None if left is None or right is None else f"({left}{measure.op}{right})"


def _range_terms(measure: Measure, table: CatalogTable) -> str:
    """Row arithmetic over whole columns, for a rank's MATCH: the measure's columns as ranges under its operators."""
    if isinstance(measure, (Col, DateOf)):
        return _range(table, measure_columns(measure)[0].position)
    if isinstance(measure, Words):
        return _words_formula(_range(table, measure.column.position))
    if isinstance(measure, Size):
        return f"ABS({_range_terms(measure.measure, table)})"
    if isinstance(measure, Across):
        return "(" + "+".join(_range(table, column.position) for column in measure.columns) + ")"
    if isinstance(measure, PercentOf):
        return f"({_range_terms(measure.measure, table)}*{_range(table, measure.rate.position)}/100)"
    return f"({_range_terms(measure.left, table)}{measure.op}{_range_terms(measure.right, table)})"


def _rank_formula(program: Rank, table: CatalogTable, tables: RequestTables) -> str:
    way = "MAX" if program.direction == HIGHEST else "MIN"
    if program.entity == ENTITY_LINE and not program.shown:      # the figures themselves, the top ones first
        return f"=TAKE(SORT({_range_terms(program.measure, table)},1,{-1 if program.direction == HIGHEST else 1}),{program.count})"
    if program.entity == ENTITY_LINE and isinstance(program.measure, LineOrder):     # the first or last lines of the range
        if program.where or program.shown[0].position == SECTION or not _plain_range(table):
            return "=NA()"
        shown = _range(table, program.shown[0].position)
        return f"=TAKE({shown},{program.count if program.direction != HIGHEST else -program.count})"
    if program.entity == ENTITY_LINE:
        shown = _range(table, program.shown[0].position) if program.shown[0].position != SECTION else "Section"
        if isinstance(program.measure, Col):
            measure = _range(table, program.measure.position)
            return f"=INDEX({shown},MATCH({way}({measure}),{measure},0))"
        if isinstance(program.measure, DateOf):
            measure = _range(table, program.measure.column.position)
            return f"=INDEX({shown},MATCH({way}({measure}),{measure},0))"
        if isinstance(program.measure, Size):
            measure = _range(table, measure_columns(program.measure)[0].position)
            return f"=INDEX({shown},MATCH(MAX(ABS({measure})),ABS({measure}),0))"
        if isinstance(program.measure, Across):
            added = "+".join(_range(table, column.position) for column in program.measure.columns)
            return f"=INDEX({shown},MATCH({way}({added}),{added},0))"
        if isinstance(program.measure, RowArithmetic) and isinstance(program.measure.left, Col) and isinstance(program.measure.right, Col):
            a, b = (_range(table, column.position) for column in measure_columns(program.measure)[:2])
            return f"=INDEX({shown},MATCH({way}({a}{program.measure.op}{b}),{a}{program.measure.op}{b},0))"
        ranged = _range_terms(program.measure, table)
        return f"=INDEX({shown},MATCH({way}({ranged}),{ranged},0))"
    if program.entity == ENTITY_GROUP and program.grain is not None:     # a date key's month or year: stated by its read-as and cells
        return "=NA()"
    if program.entity == ENTITY_GROUP:
        key = _range(table, program.key.position) if program.key.position != SECTION else "Section"
        criteria = _criteria(tables.catalog, program.where) or []
        tail = "," + ",".join(criteria) if criteria else ""
        order = "-1" if program.direction == HIGHEST else "1"
        if program.function == COUNT:
            inner = f"COUNTIFS({key},UNIQUE({key}){tail})"
        elif isinstance(program.measure, Words):
            return "=NA()"      # words have no range a MAXIFS reads: the read-as and the cells state it
        else:
            columns = measure_columns(program.measure)
            inner = f"{_IFS[program.function]}({_range(table, columns[0].position)},{key},UNIQUE({key}){tail})"
            if program.by_size:
                inner = f"ABS({inner})"
        return f"=INDEX(SORTBY(UNIQUE({key}),{inner},{order}),1)"
    first, last = program.peers[0], program.peers[-1]
    head = table.columns[first], table.columns[last]
    header = table.table.header_rows[-1] if table.table.header_rows else table.table.first_row
    head_range = f"'{table.tab}'!{head[0].letter}{header}:{head[1].letter}{header}"
    line = _peer_line(program, table, tables)
    if program.mark is not None:
        return f'=INDEX({head_range},MATCH(MAX(COUNTIF(column,"{program.mark}")),COUNTIF(column,"{program.mark}"),0))'
    if program.by_size:
        return f"=INDEX({head_range},MATCH(MAX(ABS({line})),ABS({line}),0))"
    return f"=INDEX({head_range},MATCH({way}({line}),{line},0))"


def _peer_line(program: Rank, table: CatalogTable, tables: RequestTables) -> str:
    """The row the peers are read across: one kept line's cells, several lines' added cell by cell, many lines' a column total per peer."""
    kept = kept_lines(program, table, index_of(table))
    first, last = table.columns[program.peers[0]].letter, table.columns[program.peers[-1]].letter
    if len(kept) <= _MAX_LINES_ADDED:
        return "+".join(f"'{table.lines[i].tab}'!{first}{table.lines[i].row}:{last}{table.lines[i].row}" for i in kept) or "NA()"
    rows = [table.lines[i].row for i in kept]
    return f"BYCOL('{table.tab}'!{first}{min(rows)}:{last}{max(rows)},LAMBDA(c,SUM(c)))"


def explain(scored: ScoredProgram, tables: RequestTables, rows: tuple[tuple, ...]) -> Explanation:
    """The proof for a chosen program and its answer."""
    program = scored.program
    also = ()
    if answer_type_of(program, tables.catalog.column_kind) == A_PERCENT and len(rows) == 1 and len(rows[0]) == 1:
        also = (f"{share_forms(float(rows[0][0]))[1]:g}%",)
    return Explanation(read_as(program, tables.catalog), compress(cells_used(program, tables, rows)), formula(program, tables), tuple(term.record() for term in scored.contributions), also)


__all__ = ["Explanation", "answer_columns", "cells_used", "compress", "explain", "formula", "header", "in_header_words", "read_as"]
