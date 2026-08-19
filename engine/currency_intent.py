"""Pure, deterministic currency-conversion intent parsing."""
from __future__ import annotations

import re
from typing import Sequence


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
