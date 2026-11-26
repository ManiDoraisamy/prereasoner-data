"""Small, readable SQL-semantic operators used by emitted Python analyses."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
import re
from typing import Generic, TypeVar

from engine.numeric import decimal_deviation, decimal_divide, decimal_median, wire_document
from engine.sql_ast import DAYS_PER_UNIT, day_number

T = TypeVar("T")
U = TypeVar("U")
A = TypeVar("A")


class PythonRowLimitExceeded(RuntimeError):
    """The materialized Python pipeline exceeded its bounded execution budget."""


@dataclass(frozen=True)
class SourceRowKey:
    """A physical ORM source identity, not an inferred original row number."""

    schema: str | None
    table: str
    columns: tuple[str, ...]
    values: tuple[object, ...]

    def record(self):
        return {"schema": self.schema, "table": self.table, "key_columns": list(self.columns),
                "key_values": [v.isoformat() if isinstance(v, (date, datetime)) else wire_document(v) for v in self.values]}


def _source_keys(values):
    from sqlalchemy import inspect

    keys = []
    for value in values:
        if value is None:
            continue  # The missing side of an outer join has no source row.
        state = inspect(value, raiseerr=False)
        if state is None or not hasattr(state, "mapper") or state.identity is None:
            return None
        table = state.mapper.local_table
        keys.append(SourceRowKey(table.schema, table.name, tuple(c.name for c in state.mapper.primary_key), tuple(state.identity)))
    return tuple(keys)


def _merge_sources(parts):
    """Preserve occurrences, including the same source row used more than once."""
    result = []
    for part in parts:
        if part is None:
            return None
        result.extend(part)
    return tuple(result)


@dataclass(frozen=True)
class View(Generic[T]):
    name: str
    rows: tuple[T, ...]
    row_limit: int | None = None
    source_rows: tuple[tuple[SourceRowKey, ...] | None, ...] = field(default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.source_rows and self.rows:
            object.__setattr__(self, "source_rows", (None,) * len(self.rows))
        if (not isinstance(self.source_rows, tuple) or len(self.source_rows) != len(self.rows)
                or any(part is not None and (not isinstance(part, tuple) or any(not isinstance(k, SourceRowKey) for k in part))
                       for part in self.source_rows)):
            raise ValueError("view source identities must align with every output row")
        if self.row_limit is not None and (
            type(self.row_limit) is not int or self.row_limit < 0
        ):
            raise ValueError("Python view row limit must be non-negative")
        if self.row_limit is not None and len(self.rows) > self.row_limit:
            raise PythonRowLimitExceeded(
                f"Python view {self.name!r} exceeded the {self.row_limit}-row limit"
            )

    @staticmethod
    def _append_bounded(
        materialized: list[U], value: U, name: str, row_limit: int | None
    ) -> None:
        if row_limit is not None and len(materialized) >= row_limit:
            raise PythonRowLimitExceeded(
                f"Python view {name!r} exceeded the {row_limit}-row limit"
            )
        materialized.append(value)

    @classmethod
    def from_orm(
        cls,
        name: str,
        rows: Iterable[object],
        construct: Callable[..., T],
        row_limit: int | None = None,
    ) -> View[T]:
        materialized, origins = [], []
        for row in rows:
            values = tuple(row) if not isinstance(row, tuple) else row
            cls._append_bounded(
                materialized, construct(*values), name, row_limit
            )
            origins.append(_source_keys(values))
        return cls(name, tuple(materialized), row_limit, tuple(origins))

    def for_each(
        self, name: str, emit: Callable[[T], U | Iterable[U] | None]
    ) -> View[U]:
        materialized, origins = [], []
        for row, origin in zip(self.rows, self.source_rows, strict=True):
            produced = emit(row)
            if produced is None:
                continue
            if isinstance(produced, (list, tuple)):
                for value in produced:
                    self._append_bounded(
                        materialized, value, name, self.row_limit
                    )
                    origins.append(origin)
            else:
                self._append_bounded(
                    materialized, produced, name, self.row_limit
                )
                origins.append(origin)
        return View(name, tuple(materialized), self.row_limit, tuple(origins))

    def filter(self, name: str, where: Callable[[T], bool | None]) -> View[T]:
        materialized, origins = [], []
        for row, origin in zip(self.rows, self.source_rows, strict=True):
            if where(row) is True:
                self._append_bounded(
                    materialized, row, name, self.row_limit
                )
                origins.append(origin)
        return View(name, tuple(materialized), self.row_limit, tuple(origins))

    def sort(self, name, keys, descending, limit=None, ties_on=0):
        """ORDER BY with explicit NULLS LAST; emitters supply deterministic tie keys. With ``ties_on``,
        a row equal to the last kept row on the first ``ties_on`` keys is kept too (FETCH FIRST n ROWS
        WITH TIES)."""

        def compare(left, right, terms=None):
            pairs = zip(keys(left), keys(right), descending, strict=True)
            for a, b, desc in list(pairs)[:terms]:
                if a is None or b is None:
                    result = (a is None) - (b is None)
                else:
                    result = (a > b) - (a < b)
                    if desc:
                        result = -result
                if result:
                    return result
            return 0

        indices = sorted(range(len(self.rows)), key=cmp_to_key(lambda i, j: compare(self.rows[i], self.rows[j])))
        ordered = [self.rows[i] for i in indices]
        end = limit
        if limit is not None and ties_on and limit > 0:
            while end < len(ordered) and compare(ordered[end], ordered[limit - 1], ties_on) == 0:
                end += 1
        return View(name, tuple(ordered[:end]), self.row_limit, tuple(self.source_rows[i] for i in indices[:end]))

    def cross(
        self,
        name: str,
        other: View[U],
        construct: Callable[[T, U], A],
    ) -> View[A]:
        """CROSS JOIN with a hard materialization bound."""
        materialized, origins = [], []
        for left, left_origin in zip(self.rows, self.source_rows, strict=True):
            for right, right_origin in zip(other.rows, other.source_rows, strict=True):
                self._append_bounded(
                    materialized, construct(left, right), name, self.row_limit
                )
                origins.append(_merge_sources((left_origin, right_origin)))
        return View(name, tuple(materialized), self.row_limit, tuple(origins))

    def anti_join(
        self,
        name: str,
        other: View[U],
        left_key: Callable[[T], tuple[object, ...]],
        right_key: Callable[[U], tuple[object, ...]],
    ) -> View[T]:
        """SQL ``NOT EXISTS`` equality semantics, including non-matching NULL keys."""
        blocked = {
            key
            for row in other.rows
            if (key := right_key(row)) and all(value is not None for value in key)
        }

        def keep(row: T) -> bool:
            key = left_key(row)
            return any(value is None for value in key) or key not in blocked

        return self.filter(name, keep)

    def reduce(
        self,
        name: str,
        initial: A,
        step: Callable[[A, T], A],
        finalize: Callable[[A], U] | None = None,
    ) -> View[A] | View[U]:
        result = initial
        for row in self.rows:
            result = step(result, row)
        # SQL scalar aggregates always emit one row, even for an empty input.
        output_limit = (
            None if self.row_limit is None else max(1, self.row_limit)
        )
        origins = (_merge_sources(self.source_rows),)
        if finalize is not None:
            return View(name, (finalize(result),), output_limit, origins)
        return View(name, (result,), output_limit, origins)

    def longest_run(
        self,
        name: str,
        order: Callable[[T], object],
        keep: Callable[[T], bool | None],
        emit: Callable[[int], U],
    ) -> View[U]:
        """The most rows in a row, as ``order`` lists them, that ``keep`` holds for: one row, 0 when it holds for none.
        A row it does not hold for, or reads as unknown (None), ends a run. The SQL program counts the same runs by
        gaps and islands (engine/deterministic/emitter/sql)."""
        ranked = self.sort(name, lambda row: (order(row),), (False,))
        best = current = 0
        for row in ranked.rows:
            current = current + 1 if keep(row) is True else 0
            best = max(best, current)
        output_limit = None if self.row_limit is None else max(1, self.row_limit)
        return View(name, (emit(best),), output_limit, (_merge_sources(self.source_rows),))

    def group_reduce(
        self,
        name: str,
        key: Callable[[T], object],
        initial: Callable[[T], A],
        step: Callable[[A, T], A],
        finalize: Callable[[A], U] | None = None,
    ) -> View[A] | View[U]:
        groups: dict[object, A] = {}
        origins = {}
        for row, origin in zip(self.rows, self.source_rows, strict=True):
            group_key = key(row)
            if group_key not in groups:
                current = initial(row)
            else:
                current = groups[group_key]
            groups[group_key] = step(current, row)
            origins.setdefault(group_key, []).append(origin)
        lineage = tuple(_merge_sources(origins[k]) for k in groups)
        if finalize is not None:
            return View(
                name,
                tuple(finalize(value) for value in groups.values()),
                self.row_limit,
                lineage,
            )
        return View(name, tuple(groups.values()), self.row_limit, lineage)


@dataclass(frozen=True)
class AverageState:
    total: Decimal = Decimal(0)
    count: int = 0

    @property
    def value(self) -> Decimal | None:
        return DIVIDE(self.total, self.count)


def FINALIZE_AVG(value: AverageState) -> Decimal | None:
    return value.value


def SUM(current, value):
    if value is None:
        return current
    return value if current is None else current + value


def MAX(current, value):
    if value is None:
        return current
    return value if current is None or value > current else current


def MIN(current, value):
    if value is None:
        return current
    return value if current is None or value < current else current


def COUNT(current: int, value) -> int:
    return current if value is None else current + 1


@dataclass
class DistinctCountState:
    values: set[object] = field(default_factory=set)


def COUNT_DISTINCT(current: DistinctCountState, value) -> DistinctCountState:
    if value is not None:
        current.values.add(value)
    return current


def FINALIZE_COUNT_DISTINCT(current: DistinctCountState) -> int:
    return len(current.values)


def COUNT_STAR(current: int) -> int:
    return current + 1


@dataclass
class SeriesState:
    """Every value an aggregate of the whole series reads (a median, a standard deviation), in the order met."""
    values: list[Decimal] = field(default_factory=list)


def MEDIAN(current: SeriesState, value) -> SeriesState:
    if value is not None:
        current.values.append(Decimal(str(value)))
    return current


STDEV = MEDIAN      # both gather the series; their FINALIZE tells them apart


def FINALIZE_MEDIAN(current: SeriesState) -> Decimal | None:
    return decimal_median(current.values)


def FINALIZE_STDEV(current: SeriesState) -> Decimal | None:
    return decimal_deviation(current.values)


def AVG(current: AverageState, value) -> AverageState:
    if value is None:
        return current
    return AverageState(current.total + Decimal(str(value)), current.count + 1)


def ADD(left, right):
    return None if left is None or right is None else left + right


def SUBTRACT(left, right):
    return None if left is None or right is None else left - right


def MULTIPLY(left, right):
    return None if left is None or right is None else left * right


def DIVIDE(left, right):
    if left is None or right in (None, 0):
        return None
    return decimal_divide(Decimal(str(left)), Decimal(str(right)))


def _comparable(left, right):
    def date_text(value):
        return str(value) if isinstance(value, datetime) else value.isoformat()

    if isinstance(left, str) and isinstance(right, date):
        right = date_text(right)
    elif isinstance(left, date) and isinstance(right, str):
        left = date_text(left)
    return left, right


def EQ(left, right):
    if left is None or right is None:
        return None
    left, right = _comparable(left, right)
    return left == right


def LOWER(value):
    return None if value is None else value.lower()


def TEXT(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def ABS(value):
    """A number's size, its sign dropped."""
    return None if value is None else abs(value)


def BLANK_ZERO(value):
    """A number whose blank counts 0: one blank side of a line in row arithmetic of + and -."""
    return Decimal(0) if value is None else value


def LIKE(value, pattern):
    """SQL's LIKE over text, as both SQL engines read it: '%' matches any run of characters, '_' one
    character, and every other character itself, over the whole text. The search compares lower-cased
    text with a lower-case pattern (sql_ast.Lower), so case never decides it."""
    if value is None or pattern is None:
        return None
    expression = "".join(".*" if part == "%" else "." if part == "_" else re.escape(part) for part in str(pattern))
    return re.fullmatch(expression, str(value), re.DOTALL) is not None


def MONTH(value):
    """The month of a date, read from its ISO text as the SQL program reads it: '2026-08-04' -> 8."""
    if value is None:
        return None
    text = value.isoformat() if isinstance(value, date) else str(value)
    return int(text[5:7])


def YEAR(value):
    """The year of a date, read from its ISO text as the SQL program reads it: '2026-08-04' -> 2026."""
    if value is None:
        return None
    text = value.isoformat() if isinstance(value, date) else str(value)
    return int(text[:4])


def YEAR_MONTH(value):
    """The year-month of a date, read from its ISO text as the SQL program reads it: '2026-08-04' ->
    '2026-08'."""
    if value is None:
        return None
    text = value.isoformat() if isinstance(value, date) else str(value)
    return text[:7]


def SPAN(start, end, until, unit):
    """How long a row lasted in ``unit``s, as the SQL program computes it (engine.sql_ast.date_span_sql): the
    days from ``start``'s date to ``end``'s, or to ``until`` when ``end`` is empty or missing, over the unit's
    length in days; None when the start is empty or missing."""
    first = _day(start)
    if first is None:
        return None
    last = _day(end)
    return ((day_number(until) if last is None else last) - first) / DAYS_PER_UNIT[unit]


def _day(value):
    """The Julian day number of a date or of its ISO text's first ten characters; None for an empty one."""
    if value is None:
        return None
    text = (value.isoformat() if isinstance(value, date) else str(value))[:10]
    return day_number(text) if text else None


def AND(*values):
    return False if False in values else None if None in values else True


def OR(*values):
    return True if True in values else None if None in values else False


def NE(left, right):
    if left is None or right is None:
        return None
    left, right = _comparable(left, right)
    return left != right


def GT(left, right):
    if left is None or right is None:
        return None
    left, right = _comparable(left, right)
    return left > right


def GE(left, right):
    if left is None or right is None:
        return None
    left, right = _comparable(left, right)
    return left >= right


def LT(left, right):
    if left is None or right is None:
        return None
    left, right = _comparable(left, right)
    return left < right


def LE(left, right):
    if left is None or right is None:
        return None
    left, right = _comparable(left, right)
    return left <= right


def _verdict(holds: Callable, left, right):
    """A verdict as SQL computes one: 1 or 0, None when a side is missing; two numbers compare by their decimal values,
    anything else (an equality of texts) by its text."""
    if left is None or right is None:
        return None
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
    except InvalidOperation:
        a, b = str(left), str(right)
    return 1 if holds(a, b) else 0


def VERDICT_GT(left, right):
    return _verdict(lambda a, b: a > b, left, right)


def VERDICT_GE(left, right):
    return _verdict(lambda a, b: a >= b, left, right)


def VERDICT_LT(left, right):
    return _verdict(lambda a, b: a < b, left, right)


def VERDICT_LE(left, right):
    return _verdict(lambda a, b: a <= b, left, right)


def VERDICT_EQ(left, right):
    return _verdict(lambda a, b: a == b, left, right)


def VERDICT_NE(left, right):
    return _verdict(lambda a, b: a != b, left, right)


def IS(left, right):
    return left is right if right is None else left == right


def IS_NOT(left, right):
    return not IS(left, right)
