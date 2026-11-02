"""The program language: show, agg, rank and calc over a compiled sheet, with size and row arithmetic in the measure.

Each form is a typed, immutable dataclass over the SheetModel's tables and columns. Same slots, same program: every meaning (signs, blanks, printed lines, partial match, periods, ties) is fixed in semantics.py and lowering.py, none
is a choice a program makes. coordinates() names what a program fills, one coordinate per slot; the additive score in scoring.py reads only those names.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from engine.program.names import (A_COUNT, A_DATE, A_LIST, A_NAME, A_NUMBER, A_PERCENT, AGG, AGGREGATES, ANCHORS, ANYWHERE, BY_SIZE, CALC, CALC_OPS, COMPARISONS, COUNT, COUNT_DISTINCT, DIRECTIONS, EACH_BY_SIZE, ENTITY_GROUP, ENTITY_LINE, ENTITY_PEER, HIGHEST,
                                  MINUS, NE, NUMBER_AGGREGATES, OVER, PLUS, RANK, RANK_ENTITIES, ROW_OPS, SHOW, SLOT_AGGREGATE, SLOT_ANSWER_TYPE, SLOT_BLANK, SLOT_CALC_LEFT, SLOT_CALC_OP, SLOT_COMPARISON, SLOT_CONDITIONS, SLOT_CONTAINS, SLOT_DIRECTION, SLOT_ENTITY, SLOT_FILTER, SLOT_FORM, SLOT_KEY, SLOT_LINE,
                                  SLOT_LISTING, SLOT_MEASURE, SLOT_NUMBER, SLOT_PEERS, SLOT_PERIOD, SLOT_ROW_OP, SLOT_SECTION, SLOT_SHOWN, SLOT_SIZE, SLOT_TABLE, SLOT_VALUE, STATED_BY_SIZE, coordinate)
from engine.program.semantics import fold, number_text
from engine.sheet.names import P_DAY, P_MONTH, P_QUARTER, P_RANGE, P_YEAR, PERIOD_KINDS

SECTION = -1     # the position of a table's section, which behaves as a text column
TOGETHER = "together"


@dataclass(frozen=True, slots=True, order=True)
class Col:
    """A column of a catalog table; position SECTION reads the band above the line."""
    table: str
    position: int

    @property
    def name(self) -> str:
        return f"{self.table}.{self.position}"


# --------------------------------------------------------------------------------------------------------- measures
@dataclass(frozen=True, slots=True)
class RowArithmetic:
    """Two measures of one line under an operator. In + and - a blank side counts 0; in * and / it leaves the line out."""
    left: "Measure"
    op: str
    right: "Measure"

    def __post_init__(self):
        if self.op not in ROW_OPS:
            raise ValueError(f"unknown row operator: {self.op}")


@dataclass(frozen=True, slots=True)
class Size:
    """A measure with its sign dropped, line by line."""
    measure: "Measure"


@dataclass(frozen=True, slots=True)
class DateOf:
    """A date column read as its day: "the latest fix" ranks by it."""
    column: Col


@dataclass(frozen=True, slots=True)
class Across:
    """Peer columns of one line added: "total across the months". A blank column counts 0, and a line blank in all of them is not a line. Two or more columns of one table, in sheet order."""
    columns: tuple[Col, ...]

    def __post_init__(self):
        if len(self.columns) < 2 or len({column.table for column in self.columns}) != 1:
            raise ValueError("across adds two or more columns of one table")


Measure: TypeAlias = Col | RowArithmetic | Size | DateOf | Across


def measure_columns(measure: Measure) -> tuple[Col, ...]:
    if isinstance(measure, Col):
        return (measure,)
    if isinstance(measure, DateOf):
        return (measure.column,)
    if isinstance(measure, Across):
        return measure.columns
    if isinstance(measure, Size):
        return measure_columns(measure.measure)
    return measure_columns(measure.left) + measure_columns(measure.right)


# ------------------------------------------------------------------------------------------------------- conditions
@dataclass(frozen=True, slots=True)
class Equals:
    """A cell is this value: case and spacing do not count, a number compares as a number."""
    column: Col
    value: str | float


@dataclass(frozen=True, slots=True)
class NotEquals:
    """A text cell is some other value: "other than EES". A blank is no other value and never passes, as with a number's != (Compare)."""
    column: Col
    value: str


@dataclass(frozen=True, slots=True)
class Contains:
    """A cell holds this text, the partial value match. An exact cell wins: see semantics.exact_wins."""
    column: Col
    text: str
    anchor: str = ANYWHERE

    def __post_init__(self):
        if self.anchor not in ANCHORS:
            raise ValueError(f"unknown anchor: {self.anchor}")


@dataclass(frozen=True, slots=True, order=True)
class PeriodSpan:
    """A stretch of time: start and end are ISO days; a month of any year has only month."""
    kind: str
    start: str | None = None
    end: str | None = None
    month: int | None = None

    def __post_init__(self):
        if self.kind not in PERIOD_KINDS:
            raise ValueError(f"unknown period kind: {self.kind}")
        if self.start is None and self.month is None:
            raise ValueError("a period names days or a month")

    @property
    def name(self) -> str:
        if self.start is None:
            return f"m{self.month:02d}"
        if self.kind == P_DAY:
            return self.start
        if self.kind == P_MONTH:
            return self.start[:7]
        if self.kind == P_YEAR:
            return self.start[:4]
        if self.kind == P_QUARTER:
            return f"{self.start[:4]}-q{(int(self.start[5:7]) + 2) // 3}"
        return f"{self.start}..{self.end}"


def period_from_name(name: str) -> PeriodSpan:
    """The period a coordinate names: "2002", "2001-02", "2001-11-29", "m02" (a month of any year), "2001-q4", or "first..last"."""
    if name.startswith("m"):
        return PeriodSpan(P_MONTH, month=int(name[1:]))
    if ".." in name:
        first, last = name.split("..")
        return PeriodSpan(P_RANGE, first, last)
    if "-q" in name:
        year, quarter = name.split("-q")
        first = (int(quarter) - 1) * 3 + 1
        return PeriodSpan(P_QUARTER, f"{year}-{first:02d}-01", f"{year}-{first + 2:02d}-{(31, 30, 30, 31)[int(quarter) - 1]}")
    parts = name.split("-")
    if len(parts) == 1:
        return PeriodSpan(P_YEAR, f"{name}-01-01", f"{name}-12-31")
    if len(parts) == 2:
        year, month = int(parts[0]), int(parts[1])
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        last = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
        return PeriodSpan(P_MONTH, f"{name}-01", f"{name}-{last:02d}")
    return PeriodSpan(P_DAY, name, name)


@dataclass(frozen=True, slots=True)
class InPeriod:
    """A date cell falls in a period."""
    column: Col
    span: PeriodSpan


@dataclass(frozen=True, slots=True)
class Compare:
    """A number cell compares with a number the question states; text and blanks never pass."""
    column: Col
    op: str
    number: float

    def __post_init__(self):
        if self.op not in COMPARISONS:
            raise ValueError(f"unknown comparison: {self.op}")


@dataclass(frozen=True, slots=True)
class CompareColumns:
    """One number cell compares with another on the same line; a line blank on either side never passes."""
    column: Col
    op: str
    other: Col

    def __post_init__(self):
        if self.op not in COMPARISONS:
            raise ValueError(f"unknown comparison: {self.op}")


@dataclass(frozen=True, slots=True)
class IsBlank:
    """A cell is blank, or with filled=True is not. A marker such as "-" is filled."""
    column: Col
    filled: bool = False


@dataclass(frozen=True, slots=True)
class IsNumber:
    """A cell of a mixed column reads as a number."""
    column: Col


@dataclass(frozen=True, slots=True)
class InSection:
    """A band above the line says this, whole, or holding it when partial; bands nest, and any of them counts."""
    table: str
    text: str
    partial: bool = False


@dataclass(frozen=True, slots=True)
class OnLine:
    """A line's own label holds this, printed lines included: the condition that addresses printed lines. Empty text is a line the sheet leaves unlabelled; role narrows to the printed lines of one role, a subtotal or the total."""
    table: str
    text: str
    role: str | None = None


Condition: TypeAlias = Equals | NotEquals | Contains | InPeriod | Compare | CompareColumns | IsBlank | IsNumber | InSection | OnLine


def addresses_printed(where: tuple[Condition, ...]) -> bool:
    return any(isinstance(condition, OnLine) for condition in where)


def condition_coordinates(condition: Condition) -> tuple[str, ...]:
    if isinstance(condition, Equals):
        value = number_text(condition.value) if isinstance(condition.value, (int, float)) else fold(condition.value)
        return (coordinate(SLOT_FILTER, condition.column.name), coordinate(SLOT_VALUE, condition.column.name, value))
    if isinstance(condition, NotEquals):
        return (coordinate(SLOT_FILTER, condition.column.name), coordinate(SLOT_COMPARISON, NE), coordinate(SLOT_VALUE, condition.column.name, fold(condition.value)))
    if isinstance(condition, Contains):
        if condition.anchor != ANYWHERE:
            return (coordinate(SLOT_FILTER, condition.column.name), coordinate(SLOT_CONTAINS, condition.column.name, fold(condition.text), condition.anchor))
        return (coordinate(SLOT_FILTER, condition.column.name), coordinate(SLOT_CONTAINS, condition.column.name, fold(condition.text)))
    if isinstance(condition, InPeriod):
        return (coordinate(SLOT_FILTER, condition.column.name), coordinate(SLOT_PERIOD, condition.span.name))
    if isinstance(condition, Compare):
        return (coordinate(SLOT_FILTER, condition.column.name), coordinate(SLOT_COMPARISON, condition.op), coordinate(SLOT_NUMBER, number_text(condition.number)))
    if isinstance(condition, CompareColumns):
        return (coordinate(SLOT_FILTER, condition.column.name), coordinate(SLOT_FILTER, condition.other.name), coordinate(SLOT_COMPARISON, condition.op), coordinate(SLOT_COMPARISON, f"{condition.op}@{condition.column.name}"))
    if isinstance(condition, IsBlank):
        return (coordinate(SLOT_FILTER, condition.column.name), coordinate(SLOT_BLANK, "filled" if condition.filled else "blank"))
    if isinstance(condition, IsNumber):
        return (coordinate(SLOT_FILTER, condition.column.name), coordinate(SLOT_BLANK, "number"))
    if isinstance(condition, InSection):
        return (coordinate(SLOT_SECTION, condition.table, fold(condition.text)),)
    if condition.role is not None:
        return (coordinate(SLOT_LINE, condition.table, fold(condition.text), condition.role),)
    return (coordinate(SLOT_LINE, condition.table, fold(condition.text)),)


# ------------------------------------------------------------------------------------------------------------ forms
@dataclass(frozen=True, slots=True)
class Show:
    """The cell at a labelled line and named column (the first such line, printed lines included), or with listing=True the distinct values on the kept data lines."""
    table: str
    shown: tuple[Col, ...]
    where: tuple[Condition, ...] = ()
    listing: bool = False

    def __post_init__(self):
        if not self.shown:
            raise ValueError("a show names a column")
        if self.listing and len(self.shown) != 1:
            raise ValueError("a list shows one column")


@dataclass(frozen=True, slots=True)
class Agg:
    """f of a measure over the data lines the conditions keep. Blanks and text cells are skipped; count takes no measure, a distinct count a column. magnitude states the result by its size."""
    function: str
    measure: Measure | None
    table: str
    where: tuple[Condition, ...] = ()
    magnitude: bool = False

    def __post_init__(self):
        if self.function not in AGGREGATES:
            raise ValueError(f"unknown aggregate: {self.function}")
        if (self.function == COUNT) != (self.measure is None):
            raise ValueError("count alone takes no measure")
        if self.function == COUNT_DISTINCT and not isinstance(self.measure, Col):
            raise ValueError("a distinct count reads a column")


@dataclass(frozen=True, slots=True)
class Rank:
    """The top line, group or peer column; every tied one is kept.

    line   shown columns of the line(s) with the top measure.
    group  the key whose lines give the top f(measure), or the top count when function is count.
    peer   which of the peer columns (positions) holds the top sum across the kept lines, or with mark the most cells equal to it.
    by_size ranks groups and peers by size, the sign dropped; a line's size is a Size measure.
    """
    entity: str
    table: str
    direction: str = HIGHEST
    where: tuple[Condition, ...] = ()
    measure: Measure | None = None
    shown: tuple[Col, ...] = ()
    key: Col | None = None
    function: str | None = None
    peers: tuple[int, ...] = ()
    by_size: bool = False
    mark: str | None = None

    def __post_init__(self):
        if self.entity not in RANK_ENTITIES:
            raise ValueError(f"unknown rank entity: {self.entity}")
        if self.direction not in DIRECTIONS:
            raise ValueError(f"unknown direction: {self.direction}")
        if self.entity == ENTITY_LINE and (not self.shown or self.measure is None):
            raise ValueError("a line rank shows a column and ranks a measure")
        if self.entity == ENTITY_GROUP:
            if self.key is None or self.function not in (*NUMBER_AGGREGATES, COUNT) or (self.function == COUNT) != (self.measure is None):
                raise ValueError("a group rank needs a key, and a function over a measure or a count")
        if self.entity == ENTITY_PEER and not self.peers:
            raise ValueError("a peer rank needs its peers")


@dataclass(frozen=True, slots=True)
class Calc:
    """The difference, sum or ratio of two programs that each give one number. magnitude states the result by its size."""
    left: "Program"
    op: str
    right: "Program"
    magnitude: bool = False

    def __post_init__(self):
        if self.op not in CALC_OPS:
            raise ValueError(f"unknown calc operator: {self.op}")
        for side in (self.left, self.right):
            if not isinstance(side, (Show, Agg, Calc)) or (isinstance(side, Show) and (side.listing or len(side.shown) != 1)):
                raise ValueError("a calc reads two programs of one number each")


Program: TypeAlias = Show | Agg | Rank | Calc


def is_sound(program: Program) -> bool:
    """A program that says something: a column does not rank or show under a condition fixing that same column to one value, nor group or rank by itself. Conditions are free otherwise: a measure may compare with a number (61.2), a list may hold a word (18.2)."""
    if isinstance(program, Calc):
        return is_sound(program.left) and is_sound(program.right)
    fixed = {condition.column for condition in program.where if isinstance(condition, Equals)}
    if isinstance(program, Show):
        return not fixed.intersection(program.shown)
    measured = set(measure_columns(program.measure)) if program.measure is not None else set()
    if isinstance(program, Agg):
        return not fixed & measured
    if program.entity == ENTITY_LINE:
        return not fixed & (measured | set(program.shown)) and not measured & set(program.shown)
    if program.entity == ENTITY_GROUP:
        return program.key not in measured and program.key not in fixed and not fixed & measured
    return True


def form_of(program: Program) -> str:
    return SHOW if isinstance(program, Show) else AGG if isinstance(program, Agg) else RANK if isinstance(program, Rank) else CALC


def tables_of(program: Program) -> tuple[str, ...]:
    if isinstance(program, Calc):
        return tuple(dict.fromkeys(tables_of(program.left) + tables_of(program.right)))
    return (program.table,)


def conditions_of(program: Program) -> tuple[Condition, ...]:
    if isinstance(program, Calc):
        return conditions_of(program.left) + conditions_of(program.right)
    return program.where


def measure_coordinates(measure: Measure) -> list[str]:
    if isinstance(measure, Col):
        return [coordinate(SLOT_MEASURE, measure.name)]
    if isinstance(measure, DateOf):
        return [coordinate(SLOT_MEASURE, measure.column.name)]
    if isinstance(measure, Across):
        return [coordinate(SLOT_ROW_OP, PLUS), coordinate(SLOT_PEERS, measure.columns[0].table, peers_name(tuple(column.position for column in measure.columns)))]
    if isinstance(measure, Size):
        return [coordinate(SLOT_SIZE, EACH_BY_SIZE)] + measure_coordinates(measure.measure)
    left = measure_coordinates(measure.left)
    sided = [coordinate(SLOT_CALC_LEFT, name) for name in left if name.startswith(f"{SLOT_MEASURE}:")] if measure.op in (MINUS, OVER) else []
    return left + [coordinate(SLOT_ROW_OP, measure.op)] + measure_coordinates(measure.right) + sided


def shown_together(shown: tuple[Col, ...]) -> list[str]:
    """Several columns shown read as a coordinate of their own: each column's link adds, and most questions ask for one thing."""
    return [coordinate(SLOT_SHOWN, TOGETHER, len(shown))] if len(shown) > 1 else []


def peers_name(peers: tuple[int, ...]) -> str:
    return ",".join(str(position) for position in peers)


def _body_coordinates(program: Program) -> list[str]:
    """What a program fills, its form aside."""
    names: list[str] = []
    if isinstance(program, Calc):
        return [coordinate(SLOT_CALC_OP, program.op), *([coordinate(SLOT_SIZE, STATED_BY_SIZE)] if program.magnitude else []), *_body_coordinates(program.left), *_body_coordinates(program.right)]
    names.append(coordinate(SLOT_TABLE, program.table))
    if isinstance(program, Show):
        names.extend(coordinate(SLOT_SHOWN, column.name) for column in program.shown)
        names.extend(shown_together(program.shown))
        if program.listing:
            names.append(coordinate(SLOT_LISTING, "yes"))
    elif isinstance(program, Agg):
        names.append(coordinate(SLOT_AGGREGATE, program.function))
        if program.measure is not None:
            names.extend(measure_coordinates(program.measure))
        if program.magnitude:
            names.append(coordinate(SLOT_SIZE, STATED_BY_SIZE))
    else:
        names.append(coordinate(SLOT_ENTITY, program.entity))
        names.append(coordinate(SLOT_DIRECTION, program.direction))
        if program.function is not None:
            names.append(coordinate(SLOT_AGGREGATE, program.function))
        if program.measure is not None:
            names.extend(measure_coordinates(program.measure))
        names.extend(coordinate(SLOT_SHOWN, column.name) for column in program.shown)
        names.extend(shown_together(program.shown))
        if program.key is not None:
            names.append(coordinate(SLOT_KEY, program.key.name))
        if program.peers:
            names.append(coordinate(SLOT_PEERS, program.table, peers_name(program.peers)))
        if program.by_size:
            names.append(coordinate(SLOT_SIZE, BY_SIZE))
        if program.mark is not None:
            names.append(coordinate(SLOT_VALUE, program.table, fold(program.mark)))
    for condition in program.where:
        names.extend(condition_coordinates(condition))
    return names


def coordinates(program: Program, column_kind=None) -> tuple[str, ...]:
    """Every coordinate a program fills, each once, in a stable order: the form first, then its answer type when column_kind says what its columns hold. In a calc, the coordinates only the left side fills are also named as that side's, since a - b is not b - a."""
    names = [coordinate(SLOT_FORM, form_of(program))]
    if column_kind is not None:
        names.append(coordinate(SLOT_ANSWER_TYPE, answer_type_of(program, column_kind)))
    names.extend(_body_coordinates(program))
    names.append(coordinate(SLOT_CONDITIONS, tables_of(program)[0], len(set(conditions_of(program)))))
    if isinstance(program, Calc) and program.op != PLUS:
        left, right = list(dict.fromkeys(_body_coordinates(program.left))), set(_body_coordinates(program.right))
        only = [name for name in left if name not in right] or left      # a over a plus b: the left is all inside the right, and it is still a, not b
        names.extend(coordinate(SLOT_CALC_LEFT, name) for name in only)
    return tuple(dict.fromkeys(names))


def answer_type_of(program: Program, column_kind) -> str:
    """The coarse type a program answers with. column_kind(Col) says a column's value kind."""
    if isinstance(program, Calc):
        return A_PERCENT if program.op == OVER else A_NUMBER
    if isinstance(program, Agg):
        return A_COUNT if program.function in (COUNT, COUNT_DISTINCT) else A_NUMBER
    if isinstance(program, Show):
        if program.listing:
            return A_LIST
        return _type_of_shown(program.shown, column_kind)
    if program.entity == ENTITY_PEER:
        return A_NAME
    return _type_of_column(program.key, column_kind) if program.entity == ENTITY_GROUP else _type_of_shown(program.shown, column_kind)


def _type_of_shown(shown: tuple[Col, ...], column_kind) -> str:
    """Several columns shown answer as their kinds together, whichever stands first on the sheet: a figure if any is one, else a date, else a name."""
    kinds = {_type_of_column(column, column_kind) for column in shown}
    return A_NUMBER if A_NUMBER in kinds else A_DATE if A_DATE in kinds else A_NAME


def _type_of_column(column: Col, column_kind) -> str:
    if column.position == SECTION:
        return A_NAME
    kind = column_kind(column)
    return A_NUMBER if kind == "number" else A_DATE if kind in ("date", "period") else A_NAME


__all__ = ["Across", "Agg", "Calc", "Col", "Compare", "CompareColumns", "Condition", "Contains", "DateOf", "Equals", "InPeriod", "InSection", "IsBlank", "IsNumber", "Measure", "NotEquals", "OnLine", "PeriodSpan", "Program", "Rank", "RowArithmetic", "SECTION", "Show", "Size",
           "TOGETHER", "addresses_printed", "answer_type_of", "coordinates", "shown_together", "conditions_of", "form_of", "measure_columns", "tables_of", "peers_name"]
