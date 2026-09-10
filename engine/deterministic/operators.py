"""Small, readable SQL-semantic operators used by emitted Python analyses."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Generic, TypeVar
from engine.numeric import DECIMAL_PRECISION, DIVISION_SCALE

T = TypeVar("T")
U = TypeVar("U")
A = TypeVar("A")


@dataclass(frozen=True)
class View(Generic[T]):
    name: str
    rows: tuple[T, ...]

    @classmethod
    def from_orm(
        cls, name: str, rows: Iterable[object], construct: Callable[..., T]
    ) -> View[T]:
        materialized = []
        for row in rows:
            values = tuple(row) if not isinstance(row, tuple) else row
            materialized.append(construct(*values))
        return cls(name, tuple(materialized))

    def for_each(
        self, name: str, emit: Callable[[T], U | Iterable[U] | None]
    ) -> View[U]:
        materialized = []
        for row in self.rows:
            produced = emit(row)
            if produced is None:
                continue
            if isinstance(produced, (list, tuple)):
                materialized.extend(produced)
            else:
                materialized.append(produced)
        return View(name, tuple(materialized))

    def filter(self, name: str, where: Callable[[T], bool | None]) -> View[T]:
        materialized = []
        for row in self.rows:
            if where(row) is True:
                materialized.append(row)
        return View(name, tuple(materialized))

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
        if finalize is not None:
            return View(name, (finalize(result),))
        return View(name, (result,))

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
            current = groups.get(group_key)
            if current is None:
                current = initial(row)
            groups[group_key] = step(current, row)
        if finalize is not None:
            return View(name, tuple(finalize(value) for value in groups.values()))
        return View(name, tuple(groups.values()))


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
