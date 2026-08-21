"""Pure, deterministic currency-conversion intent parsing."""
from __future__ import annotations

import re
from typing import Iterable, Sequence

_CURRENCY_ALIASES = {
    "USD": (r"usd", r"u\.?\s*s\.?\s+dollars?", r"united\s+states\s+dollars?"),
    "EUR": (r"eur", r"euros?"),
    "GBP": (r"gbp", r"pounds?\s+sterling", r"british\s+pounds?"),
}
_CURRENCY_TARGET = re.compile(
    r"\b(?:in|into|to)\s+(?:the\s+)?(?P<target>"
    + "|".join(alias for aliases in _CURRENCY_ALIASES.values() for alias in aliases)
    + r")\b",
    re.I,
)


def currency_conversion_target(question: str | Sequence[str]) -> str | None:
    """Return the explicitly requested output currency, or ``None``.

    A directional phrase is required. This avoids treating a source currency in
    ``convert USD to EUR`` as the target and avoids silently interpreting ambiguous
    ``dollars`` as USD.
    """
    if isinstance(question, str):
        text = " ".join(question.split())
    else:
        text = " ".join(str(token) for token in question)
    matches = tuple(_CURRENCY_TARGET.finditer(text))
    if not matches:
        return None
    target = matches[-1].group("target")
    for code, aliases in _CURRENCY_ALIASES.items():
        if any(re.fullmatch(alias, target, re.I) for alias in aliases):
            return code
    return None


def currency_rate_attribute(target: str) -> str:
    """Canonical direct-rate column required to express a conversion target."""
    code = str(target).strip().upper()
    if code not in _CURRENCY_ALIASES:
        raise ValueError(f"unsupported currency target: {target!r}")
    return f"rate_to_{code.lower()}"


def is_currency_source_column(name: str) -> bool:
    """Whether a fact-table column can carry a source currency code."""
    normalized = str(name).strip().lower()
    return "currency" in normalized or normalized in {"ccy", "ccy_code"}


def is_currency_reference_key(name: str) -> bool:
    """Whether a reference-table key has the supported currency-code shape."""
    return str(name).strip().lower() in {"currency", "currency_code"}


def currency_rate_binding(
    question: str | Sequence[str],
    source_table: str,
    edges: Iterable[tuple[str, str, str, str]],
    numeric_columns: Iterable[tuple[str, str]],
) -> tuple[str, str] | None:
    """Resolve one unambiguous direct-rate table for a source measure table.

    Edges are ``(from_table, from_column, to_table, to_column)`` tuples. The
    function is deliberately independent of either planner's schema classes so
    both serving paths enforce the same deterministic eligibility policy.
    """
    target = currency_conversion_target(question)
    if target is None:
        return None
    rate_column = currency_rate_attribute(target)
    # Column headers keep the case the user uploaded — engine.tables normalizes TABLE names, not columns —
    # while `rate_column` is synthesized lowercase. Comparing them exactly meant an FX sheet headed
    # `Rate_To_USD` bound nothing, and "total amount in US dollars" answered with the UNCONVERTED sum: a
    # wrong number, silently, with no clarify. Match case-insensitively but return the column as the schema
    # actually spells it, so the rendered SQL references a real identifier.
    numeric = {(table, str(column).lower()): (table, column) for table, column in numeric_columns}
    matches = {
        numeric[(to_table, rate_column)]
        for from_table, from_column, to_table, to_column in edges
        if from_table == source_table
        and is_currency_source_column(from_column)
        and is_currency_reference_key(to_column)
        and (to_table, rate_column) in numeric
    }
    return next(iter(matches)) if len(matches) == 1 else None


def currency_conversion_words(target: str) -> frozenset[str]:
    """Question words realized by a conversion to ``target`` in generated SQL."""
    words = {
        "convert", "converted", "converting", "conversion",
        "usd", "us", "united", "states", "dollar", "dollars",
    } if target == "USD" else {
        "eur", "euro", "euros",
    } if target == "EUR" else {
        "gbp", "british", "pound", "pounds", "sterling",
    } if target == "GBP" else set()
    return frozenset(words)
