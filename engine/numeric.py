"""Exact numeric parsing, SQLite execution helpers, and wire normalization.

PostgreSQL has an exact ``NUMERIC`` type. SQLite does not: values with a decimal
point are normally coerced to binary ``REAL`` during arithmetic. The typed AST
therefore has a SQLite decimal dialect whose functions are registered here.
Fractional results that cannot be represented exactly as a JSON float cross the
wire as canonical decimal strings; integral results remain JSON integers.
"""
from __future__ import annotations

from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
import math
import re
import sqlite3
import json
from typing import Any, Iterable


MAX_INTEGER_DIGITS = 38
MAX_DECIMAL_SCALE = 20
DECIMAL_PRECISION = 128
DIVISION_SCALE = 20
MIN_STORAGE_INTEGER = -(2**63)
MAX_STORAGE_INTEGER = 2**63 - 1
MAX_JSON_SAFE_INTEGER = 2**53 - 1
# A comma inside a number groups its digits: in threes ("1,234,567"), or in pairs above the last three as
# Indian lakhs and crores are written ("12,34,567"); a group never starts with 0. Any other comma is not
# grouping: "1,50" and "1.234,56" use it as the decimal point, and dropping every comma read them as 150
# and 1.23456 (release review, 2026-10-07). Such a value is not a number until its format is known.
GROUPED_DIGITS = r"[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d?(?:,\d{2})+,\d{3}"
# A plain decimal as a question or a cell writes it: a minus sign, digits (grouped or not), a fraction.
NUMBER_TEXT = re.compile(rf"^-?(?:\d+|{GROUPED_DIGITS})(?:\.\d+)?$")
_GROUPED_NUMBER = re.compile(rf"[+-]?(?:{GROUPED_DIGITS})(?:\.\d*)?")


def wire_document(value: Any) -> Any:
    """Keep integer digits exact in browser HTTP, live traces and saved snapshots."""
    if type(value) is int and abs(value) > MAX_JSON_SAFE_INTEGER:
        return str(value)
    if isinstance(value, Decimal):
        return wire_document(wire_decimal(value))
    if isinstance(value, dict):
        return {key: wire_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire_document(item) for item in value]
    return value


def json_dumps(value: Any, **kwargs) -> str:
    return json.dumps(wire_document(value), **kwargs)


def observed_numeric_affinity(values: Iterable[Any]) -> str:
    """Choose storage from every observed value, with exact decimal promotion at integer bounds.

    A large numeric identifier must not crash unrelated queries. PostgreSQL BIGINT and SQLite
    INTEGER share these bounds; larger finite values use the existing exact NUMERIC/text dialect.
    Invalid/out-of-contract cells remain text, preserving them instead of casting them to NULL.
    """
    fractional = False
    for value in values:
        try:
            number = parse_decimal(value)
        except (TypeError, ValueError):
            return "TEXT"
        if ("." in str(value) or number != number.to_integral_value()
                or not MIN_STORAGE_INTEGER <= number <= MAX_STORAGE_INTEGER):
            fractional = True
    return "REAL" if fractional else "INTEGER"


def parse_decimal(value: Any, *, enforce_input_bounds: bool = True) -> Decimal:
    """Parse a finite numeric value without introducing binary-float error.

    Uploaded operands are bounded to the PostgreSQL column contract. Exact
    calculation results may legitimately grow wider, so result-normalization
    callers explicitly disable that input check.
    """
    if isinstance(value, bool):
        raise ValueError("booleans are not numeric values")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("numeric values must be finite")
        result = Decimal(str(value))
    else:
        text = str(value).strip()
        if text.startswith("$"):
            text = text[1:].strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        if "," in text:
            if not _GROUPED_NUMBER.fullmatch(text):
                raise ValueError(f"invalid numeric value: {value!r} (its comma does not group digits)")
            text = text.replace(",", "")
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"invalid numeric value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("numeric values must be finite")
    if enforce_input_bounds:
        _validate_input_bounds(result)
    return result


def _validate_input_bounds(value: Decimal) -> None:
    _sign, digits, exponent = value.as_tuple()
    scale = max(-exponent, 0)
    integer_digits = max(len(digits) + exponent, 0)
    if scale > MAX_DECIMAL_SCALE or integer_digits > MAX_INTEGER_DIGITS:
        raise ValueError(
            f"numeric values support at most {MAX_INTEGER_DIGITS} integer digits and "
            f"{MAX_DECIMAL_SCALE} fractional digits"
        )


def canonical_decimal(value: Decimal) -> str:
    """Return a stable non-exponent decimal representation."""
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def coerce_numeric(value: Any, affinity: str) -> int | Decimal | None:
    if value is None:
        return None
    try:
        numeric = parse_decimal(value)
    except ValueError:
        return None
    if affinity == "INTEGER":
        if numeric != numeric.to_integral_value():
            return None
        return int(numeric)
    return numeric


def sqlite_numeric(value: Any, affinity: str) -> int | str | None:
    """Use integer storage or canonical text so SQLite never rounds on insert."""
    value = coerce_numeric(value, affinity)
    if value is None or isinstance(value, int):
        return value
    return canonical_decimal(value)


def wire_decimal(value: Decimal) -> int | float | str:
    """Return an exact JSON-safe scalar, preferring ordinary JSON numbers."""
    if value == value.to_integral_value():
        return int(value)
    as_float = float(value)
    if math.isfinite(as_float) and Decimal.from_float(as_float) == value:
        return as_float
    return canonical_decimal(value)


def wire_value(value: Any) -> Any:
    return wire_decimal(value) if isinstance(value, Decimal) else value


def wire_rows(rows: Iterable[Iterable[Any]]) -> list[list[Any]]:
    return [["" if value is None else wire_value(value) for value in row] for row in rows]


def _decimal_arg(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return parse_decimal(value, enforce_input_bounds=False)
    except ValueError:
        return None


def _binary(operator: str, left: Any, right: Any) -> str | None:
    a, b = _decimal_arg(left), _decimal_arg(right)
    if a is None or b is None:
        return None
    try:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            if operator == "/":
                result = (a / b).quantize(Decimal(1).scaleb(-DIVISION_SCALE))
            else:
                result = {
                    "+": lambda: a + b,
                    "-": lambda: a - b,
                    "*": lambda: a * b,
                }[operator]()
    except (DivisionByZero, InvalidOperation, ZeroDivisionError):
        return None
    return canonical_decimal(result)


def _compare(left: Any, right: Any) -> int | None:
    a, b = _decimal_arg(left), _decimal_arg(right)
    if a is None or b is None:
        return None
    return (a > b) - (a < b)


class _DecimalAggregate:
    mode = "sum"

    def __init__(self) -> None:
        self.values: list[Decimal] = []

    def step(self, value: Any) -> None:
        parsed = _decimal_arg(value)
        if parsed is not None:
            self.values.append(parsed)

    def finalize(self) -> str | None:
        if not self.values:
            return None
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            if self.mode == "sum":
                result = sum(self.values, Decimal(0))
            elif self.mode == "avg":
                result = sum(self.values, Decimal(0)) / len(self.values)
            elif self.mode == "min":
                result = min(self.values)
            else:
                result = max(self.values)
        return canonical_decimal(result)


class _DecimalAverage(_DecimalAggregate):
    mode = "avg"


class _DecimalMinimum(_DecimalAggregate):
    mode = "min"


class _DecimalMaximum(_DecimalAggregate):
    mode = "max"


def register_sqlite_decimal(connection: sqlite3.Connection) -> None:
    connection.create_function("decimal_add", 2, lambda a, b: _binary("+", a, b), deterministic=True)
    connection.create_function("decimal_sub", 2, lambda a, b: _binary("-", a, b), deterministic=True)
    connection.create_function("decimal_mul", 2, lambda a, b: _binary("*", a, b), deterministic=True)
    connection.create_function("decimal_div", 2, lambda a, b: _binary("/", a, b), deterministic=True)
    connection.create_function("decimal_cmp", 2, _compare, deterministic=True)
    connection.create_aggregate("decimal_sum", 1, _DecimalAggregate)
    connection.create_aggregate("decimal_avg", 1, _DecimalAverage)
    connection.create_aggregate("decimal_min", 1, _DecimalMinimum)
    connection.create_aggregate("decimal_max", 1, _DecimalMaximum)

    def collate(left: str, right: str) -> int:
        result = _compare(left, right)
        return result if result is not None else (left > right) - (left < right)

    connection.create_collation("decimal", collate)
