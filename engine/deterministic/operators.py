"""Small, readable SQL-semantic operators used by emitted Python analyses."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from functools import cmp_to_key
from typing import Generic, TypeVar

from engine.numeric import DECIMAL_PRECISION, DIVISION_SCALE

T = TypeVar("T")
U = TypeVar("U")
A = TypeVar("A")


class PythonRowLimitExceeded(RuntimeError):
    """The materialized Python pipeline exceeded its bounded execution budget."""


@dataclass(frozen=True)
class View(Generic[T]):
    name: str
    rows: tuple[T, ...]
    row_limit: int | None = None

    def __post_init__(self) -> None:
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
        materialized = []
        for row in rows:
            values = tuple(row) if not isinstance(row, tuple) else row
            cls._append_bounded(
                materialized, construct(*values), name, row_limit
            )
        return cls(name, tuple(materialized), row_limit)

    def for_each(
        self, name: str, emit: Callable[[T], U | Iterable[U] | None]
    ) -> View[U]:
        materialized = []
        for row in self.rows:
            produced = emit(row)
            if produced is None:
                continue
            if isinstance(produced, (list, tuple)):
                for value in produced:
                    self._append_bounded(
                        materialized, value, name, self.row_limit
                    )
            else:
                self._append_bounded(
                    materialized, produced, name, self.row_limit
                )
        return View(name, tuple(materialized), self.row_limit)

    def filter(self, name: str, where: Callable[[T], bool | None]) -> View[T]:
        materialized = []
        for row in self.rows:
            if where(row) is True:
                self._append_bounded(
                    materialized, row, name, self.row_limit
                )
        return View(name, tuple(materialized), self.row_limit)

    def sort(self, name, keys, descending, limit=None):
        """ORDER BY with explicit NULLS LAST; emitters supply deterministic tie keys."""

        def compare(left, right):
            for a, b, desc in zip(keys(left), keys(right), descending, strict=True):
                if a is None or b is None:
                    result = (a is None) - (b is None)
                else:
                    result = (a > b) - (a < b)
                    if desc:
                        result = -result
                if result:
                    return result
            return 0

        return View(
            name,
            tuple(sorted(self.rows, key=cmp_to_key(compare))[:limit]),
            self.row_limit,
        )

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
        if finalize is not None:
            return View(name, (finalize(result),), output_limit)
        return View(name, (result,), output_limit)

    def group_reduce(
        self,
        name: str,
        key: Callable[[T], object],
        initial: Callable[[T], A],
        step: Callable[[A, T], A],
        finalize: Callable[[A], U] | None = None,
    ) -> View[A] | View[U]:
        groups: dict[object, A] = {}
        for row in self.rows:
            group_key = key(row)
            if group_key not in groups:
                current = initial(row)
            else:
                current = groups[group_key]
            groups[group_key] = step(current, row)
        if finalize is not None:
            return View(
                name,
                tuple(finalize(value) for value in groups.values()),
                self.row_limit,
            )
        return View(name, tuple(groups.values()), self.row_limit)


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


def COUNT_STAR(current: int) -> int:
    return current + 1


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
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return (Decimal(str(left)) / Decimal(str(right))).quantize(
            Decimal(1).scaleb(-DIVISION_SCALE),
            rounding=ROUND_HALF_UP,
        )


def EQ(left, right):
    return None if left is None or right is None else left == right


def LOWER(value):
    return None if value is None else value.lower()


def TEXT(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def AND(*values):
    return False if False in values else None if None in values else True


def OR(*values):
    return True if True in values else None if None in values else False


def NE(left, right):
    return None if left is None or right is None else left != right


def GT(left, right):
    return None if left is None or right is None else left > right


def GE(left, right):
    return None if left is None or right is None else left >= right


def LT(left, right):
    return None if left is None or right is None else left < right


def LE(left, right):
    return None if left is None or right is None else left <= right


def IS(left, right):
    return left is right if right is None else left == right


def IS_NOT(left, right):
    return not IS(left, right)
