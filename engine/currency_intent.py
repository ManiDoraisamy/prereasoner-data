"""Pure, deterministic currency-conversion intent parsing."""
from __future__ import annotations

import re
from typing import Iterable, Sequence

_CURRENCY_ALIASES = {
    "USD": (r"usd", r"u\.?\s*s\.?\s+dollars?", r"united\s+states\s+dollars?"),
    "EUR": (r"eur", r"euros?"),
    "GBP": (r"gbp", r"pounds?\s+sterling", r"british\s+pounds?"),
}
# The phrase used when the engine writes a currency back into a question it proposes to the user.
# Rendering a bare ISO code there reads like an error message, not like a question a person would ask.
_CURRENCY_PHRASES = {"USD": "US dollars", "EUR": "euros", "GBP": "British pounds"}
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


def currency_rate_target(column_name: str) -> str | None:
    """Inverse of :func:`currency_rate_attribute`: the code a direct-rate column converts into.

    The ``rate_to_<code>`` convention is written in one place and read in several, so the parse lives
    next to the constructor rather than being re-expressed as a regex at each reader. Deliberately
    NOT restricted to the codes this module can parse out of a question: a ``rate_to_jpy`` column
    still names a conversion, and a candidate that performs one nobody asked for must stay
    penalizable. Callers that need a code the engine can also *talk* about use
    :func:`supported_currency_targets`.
    """
    match = re.fullmatch(r"rate_to_([a-z]{3})", str(column_name).strip().lower())
    return match.group(1).upper() if match is not None else None


def supported_currency_targets(column_names: Iterable[str]) -> frozenset[str]:
    """Codes these columns can convert into AND the engine can name in a proposed question."""
    return frozenset(
        code for code in (currency_rate_target(name) for name in column_names)
        if code in _CURRENCY_PHRASES
    )


def substitute_currency_target(question: str, code: str) -> str | None:
    """Rewrite the question's conversion target to ``code``, keeping the rest of it intact.

    Used to propose a question the attached data can actually answer. Rewriting the target preserves
    the user's intent to CONVERT; dropping the phrase instead would propose the unconverted total,
    which is the very answer that made the original question wrong.
    """
    phrase = _CURRENCY_PHRASES.get(str(code).strip().upper())
    if phrase is None:
        return None
    text = " ".join(str(question).split())
    matches = tuple(_CURRENCY_TARGET.finditer(text))
    if not matches:
        return None
    start, end = matches[-1].span("target")
    return text[:start] + phrase + text[end:]


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
