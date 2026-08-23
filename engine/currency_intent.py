"""Pure, deterministic currency intent and FX-binding rules."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterable, Sequence

_CURRENCY_ALIASES = {
    "USD": (r"usd", r"u\.?\s*s\.?\s+dollars?", r"united\s+states\s+dollars?"),
    "EUR": (r"eur", r"euros?"),
    "GBP": (r"gbp", r"pounds?\s+sterling", r"british\s+pounds?"),
}
_CURRENCY_PHRASES = {"USD": "US dollars", "EUR": "euros", "GBP": "British pounds"}
_ALIAS_PATTERN = "|".join(alias for aliases in _CURRENCY_ALIASES.values() for alias in aliases)
_CURRENCY_TARGET = re.compile(
    r"\b(?P<direction>in|into|to)\s+(?:the\s+)?(?P<target>" + _ALIAS_PATTERN
    + r"|[A-Za-z]{3})\b",
    re.I,
)
_EXPLICIT_CONVERSION = re.compile(
    r"\b(?:convert|converted|converting|conversion|express(?:ed)?|denominat(?:e|ed)|report(?:ed)?)\b",
    re.I,
)
_COUNT = re.compile(r"\b(?:count|number\s+of|how\s+many)\b", re.I)
_AGGREGATE_VALUE = re.compile(
    r"\b(?:total|sum|average|avg|mean|minimum|min|maximum|max|revenue|sales|turnover|"
    r"spend(?:ing)?|amount|cost|price|value)\b",
    re.I,
)
_MONETARY_MEASURE_WORDS = frozenset({
    "amount", "amt", "budget", "charge", "cost", "expense", "fee", "income",
    "paid", "payment", "price", "profit", "revenue", "sale", "sales", "spend",
    "spending", "subtotal", "total", "turnover", "value",
})
# ISO 4217 deliberately uses three-letter identifiers, several of which are also ordinary words.
# Lowercase planner tokens lose the user's original casing, so these require an uppercase spelling.
_AMBIGUOUS_ISO_WORDS = frozenset({
    "ALL", "BAM", "BOB", "COP", "CUP", "GEL", "KID", "MAD", "MOP", "PEN",
    "RON", "SOS", "TOP", "TRY",
})


class CurrencyIntentKind(str, Enum):
    OUTPUT = "output_currency"
    FILTER = "row_filter"


@dataclass(frozen=True)
class CurrencyIntent:
    target: str
    kind: CurrencyIntentKind
    phrase: str
    rule: str
    explicit: bool
    negated: bool = False


def _code(value: str) -> str | None:
    text = " ".join(str(value).split())
    for code, aliases in _CURRENCY_ALIASES.items():
        if any(re.fullmatch(alias, text, re.I) for alias in aliases):
            return code
    from engine.enrichment.value_types import ISO4217_CODES
    # Many ISO codes are ordinary English words (ALL, COP, GEL, MAD, PEN, TOP, TRY).
    # Generic codes therefore require their conventional uppercase spelling; named aliases above
    # remain case-insensitive.
    target = text.upper()
    if target in ISO4217_CODES and (text == target or target not in _AMBIGUOUS_ISO_WORDS):
        return target
    return None


def currency_intent(question: str | Sequence[str]) -> CurrencyIntent | None:
    """Classify a directional currency phrase as output units or a row filter.

    This is intentionally syntax-only. Whether an output request needs conversion, is already in
    the target currency, or merely annotates an otherwise unitless measure is decided from the
    selected AST and source values by the registered currency calculation specification.
    """
    text = " ".join(question.split()) if isinstance(question, str) else " ".join(
        str(token) for token in question
    )
    matches = tuple(_CURRENCY_TARGET.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    target = _code(match.group("target"))
    if target is None:
        return None
    explicit = bool(_EXPLICIT_CONVERSION.search(text)) or match.group("direction").lower() in {"to", "into"}
    negated = bool(re.search(r"\b(?:not|never)\s*$", text[:match.start()], re.I))
    if explicit:
        return CurrencyIntent(target, CurrencyIntentKind.OUTPUT, match.group(0),
                              "explicit-conversion", True)
    if negated:
        return CurrencyIntent(target, CurrencyIntentKind.FILTER, match.group(0),
                              "negated-row-selection", False, True)
    if _COUNT.search(text):
        return CurrencyIntent(target, CurrencyIntentKind.FILTER, match.group(0),
                              "non-scalable-count", False)
    if _AGGREGATE_VALUE.search(text):
        return CurrencyIntent(target, CurrencyIntentKind.OUTPUT, match.group(0),
                              "aggregate-output-unit", False)
    return CurrencyIntent(target, CurrencyIntentKind.FILTER, match.group(0),
                          "row-selection", False)


def currency_conversion_target(question: str | Sequence[str]) -> str | None:
    """Return the explicitly requested output currency, or ``None``.

    A directional phrase is required. This avoids treating a source currency in
    ``convert USD to EUR`` as the target and avoids silently interpreting ambiguous
    ``dollars`` as USD.
    """
    intent = currency_intent(question)
    return intent.target if intent is not None and intent.kind == CurrencyIntentKind.OUTPUT else None


def currency_rate_attribute(target: str) -> str:
    """Canonical direct-rate column required to express a conversion target."""
    code = str(target).strip().upper()
    from engine.enrichment.value_types import ISO4217_CODES
    if code not in ISO4217_CODES:
        raise ValueError(f"unsupported currency target: {target!r}")
    return f"rate_to_{code.lower()}"


def currency_rate_target(column_name: str) -> str | None:
    """Return the target named by a canonical ``rate_to_<iso-code>`` column."""
    match = re.fullmatch(r"rate_to_([a-z]{3})", str(column_name).strip().lower())
    if match is None:
        return None
    target = match.group(1).upper()
    from engine.enrichment.value_types import ISO4217_CODES
    return target if target in ISO4217_CODES else None


def currency_rate_bindings(
    source_table: str,
    edges: Iterable[tuple[str, str, str, str]],
    numeric_columns: Iterable[tuple[str, str]],
) -> dict[str, tuple[str, str]]:
    """Return targets genuinely joinable from ``source_table`` under the planner's rule."""
    numeric = {(table, str(column).lower()): (table, column) for table, column in numeric_columns}
    reference_tables = {
        to_table
        for from_table, from_column, to_table, to_column in edges
        if from_table == source_table
        and is_currency_source_column(from_column)
        and is_currency_reference_key(to_column)
    }
    bindings = {}
    candidate_codes = {
        target
        for table, column in numeric
        if table in reference_tables
        and (target := currency_rate_target(column)) is not None
    }
    for code in sorted(candidate_codes):
        rate_column = currency_rate_attribute(code)
        matches = {numeric[(table, rate_column)] for table in reference_tables
                   if (table, rate_column) in numeric}
        if len(matches) == 1:
            bindings[code] = next(iter(matches))
    return bindings


def substitute_currency_target(question: str, code: str) -> str | None:
    """Replace only the final directional target with a supported human-readable target."""
    normalized_code = str(code).strip().upper()
    phrase = _CURRENCY_PHRASES.get(normalized_code, normalized_code)
    text = " ".join(str(question).split())
    matches = tuple(_CURRENCY_TARGET.finditer(text))
    if phrase is None or not matches:
        return None
    start, end = matches[-1].span("target")
    return text[:start] + phrase + text[end:]


def substitute_currency_filter(question: str, code: str) -> str | None:
    """Rewrite an ambiguous output-unit phrase as an explicit row-filter question."""
    normalized_code = str(code).strip().upper()
    text = " ".join(str(question).split())
    matches = tuple(_CURRENCY_TARGET.finditer(text))
    if _code(normalized_code) is None or not matches:
        return None
    start, end = matches[-1].span(0)
    return text[:start] + f"where currency is {normalized_code}" + text[end:]


def is_currency_source_column(name: str) -> bool:
    """Whether a fact-table column can carry a source currency code."""
    normalized = str(name).strip().lower()
    return "currency" in normalized or normalized in {"ccy", "ccy_code"}


def is_currency_measure_column(name: str) -> bool:
    """Whether a numeric column name carries monetary rather than physical quantity semantics."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    words = {word.lower() for word in re.findall(r"[A-Za-z0-9]+", spaced)}
    return bool(words & _MONETARY_MEASURE_WORDS)


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
    return currency_rate_bindings(source_table, edges, numeric_columns).get(target)


def currency_conversion_words(target: str) -> frozenset[str]:
    """Question words realized by a conversion to ``target`` in generated SQL."""
    words = {
        "convert", "converted", "converting", "conversion",
        "usd", "us", "united", "states", "dollar", "dollars",
    } if target == "USD" else {
        "eur", "euro", "euros",
    } if target == "EUR" else {
        "gbp", "british", "pound", "pounds", "sterling",
    } if target == "GBP" else {str(target).lower()}
    return frozenset(words)
