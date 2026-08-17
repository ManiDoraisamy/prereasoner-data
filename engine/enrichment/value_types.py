"""Deterministic column value-typing (M0). Pure, offline, no learned model — this is the
separate `value_types` owner that must NOT be folded into the learned engine.router.Router
family contract. It returns evidence tags (currency_code, email, date, gtin, iso_country,
lei, ...) for a column, used by eligibility. A tag is necessary, never sufficient, for
enrichment (the registry predicate + companions/context decide — see select.py)."""
from __future__ import annotations

import re
from datetime import date, datetime

from engine.enrichment.registry import CURRENCY_CODE, EMAIL, DATE, GTIN, ISO_COUNTRY, LEI

_ISO4217_CODES = frozenset({
    "USD", "EUR", "GBP", "JPY", "INR", "CAD", "AUD", "CHF", "CNY", "SGD", "HKD", "SEK", "NOK",
    "NZD", "ZAR", "BRL", "AED", "THB", "MXN", "KRW", "RUB", "TRY", "PLN", "DKK", "IDR", "MYR",
    "PHP", "SAR", "QAR", "KWD", "BHD", "OMR", "EGP", "NGN", "KES", "GHS", "PKR", "BDT", "LKR",
    "VND", "ILS", "CZK", "HUF", "RON", "CLP", "COP", "ARS", "PEN", "TWD", "UAH",
})
_ISO3166_A2 = frozenset({
    "US", "GB", "IN", "DE", "FR", "JP", "CA", "AU", "CN", "SG", "HK", "SE", "NO", "NZ", "ZA",
    "BR", "AE", "TH", "MX", "KR", "RU", "TR", "PL", "DK", "ID", "MY", "PH", "SA", "IT", "ES",
    "NL", "BE", "CH", "AT", "IE", "PT", "GR", "FI", "IL", "EG", "NG", "KE", "PK", "BD", "LK", "VN",
})
# re.ASCII on the digit classes: Python's default \d is Unicode-aware, so full-width digit
# strings ('１２３４５６７８') would falsely type as GTIN/DATE. Value-typing must be conservative.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", re.ASCII)
_LEI = re.compile(r"^[A-Z0-9]{20}$", re.ASCII)
_DIGITS = re.compile(r"^\d+$", re.ASCII)


def _nonempty(values) -> list[str]:
    out = []
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s != "":
            out.append(s)
    return out


def _frac(pred, vals) -> float:
    if not vals:
        return 0.0
    return sum(1 for v in vals if pred(v)) / len(vals)


def _valid_iso_date(value: str) -> bool:
    try:
        if "T" in value or " " in value:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _valid_gtin(value: str) -> bool:
    if not _DIGITS.fullmatch(value) or len(value) not in (8, 12, 13, 14):
        return False
    body = value[:-1]
    weighted = sum(int(digit) * (3 if index % 2 == 0 else 1)
                   for index, digit in enumerate(reversed(body)))
    return (10 - weighted % 10) % 10 == int(value[-1])


def _valid_lei(value: str) -> bool:
    value = value.upper()
    if not _LEI.fullmatch(value) or not value[-2:].isdigit():
        return False
    expanded = "".join(str(ord(char) - 55) if "A" <= char <= "Z" else char for char in value)
    return int(expanded) % 97 == 1


def detect_column(values, threshold: float = 0.9) -> frozenset[str]:
    """Evidence tags for a column, requiring `threshold` of the non-empty cells to match.
    Deterministic and conservative — ambiguous columns yield no tag (abstain-friendly)."""
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be a real number in [0,1]")
    vals = _nonempty(values)
    if len(vals) < 1:
        return frozenset()
    tags: set[str] = set()
    if _frac(lambda v: v.upper() in _ISO4217_CODES, vals) >= threshold:
        tags.add(CURRENCY_CODE)
    if _frac(lambda v: bool(_EMAIL.match(v)), vals) >= threshold:
        tags.add(EMAIL)
    if _frac(_valid_iso_date, vals) >= threshold:
        tags.add(DATE)
    if _frac(_valid_lei, vals) >= threshold:
        tags.add(LEI)
    if _frac(_valid_gtin, vals) >= threshold:
        tags.add(GTIN)
    # ISO country A2 collides with many 2-letter tokens; only tag if NOT already a currency col
    if CURRENCY_CODE not in tags and _frac(lambda v: v.upper() in _ISO3166_A2, vals) >= threshold:
        tags.add(ISO_COUNTRY)
    return frozenset(tags)
