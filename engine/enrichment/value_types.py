"""Deterministic column value-typing (M0). Pure, offline, no learned model — this is the
separate `value_types` owner that must NOT be folded into the learned engine.router.Router
family contract. It returns evidence tags (currency_code, email, date, gtin, iso_country,
lei, ...) for a column, used by eligibility. A tag is necessary, never sufficient, for
enrichment (the registry predicate + companions/context decide — see select.py)."""
from __future__ import annotations

import re
from datetime import date, datetime

from engine.enrichment.registry import CURRENCY_CODE, EMAIL, DATE, GTIN, ISO_COUNTRY, LEI

# Generated from the active IANA tzdb 2026c and CLDR 48.2 source snapshots.
# CLDR includes historical, fund, precious-metal, and non-tender identifiers; exact source
# lookup and policy contracts decide whether a tagged value is eligible for enrichment.
_ISO4217_CODES = frozenset((
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD",
    "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BOV", "BRL", "BSD", "BTN", "BWP",
    "BYR", "BZD", "CAD", "CDF", "CHE", "CHF", "CHW", "CLF", "CLP", "CNH", "CNY", "COP",
    "COU", "CRC", "CUC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN",
    "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD",
    "HKD", "HNL", "HRK", "HTG", "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK", "JMD",
    "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD", "KZT", "LAK",
    "LBP", "LKR", "LRD", "LSL", "LTL", "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT",
    "MOP", "MRO", "MRU", "MUR", "MVR", "MWK", "MXN", "MXV", "MYR", "MZN", "NAD", "NGN",
    "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG",
    "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP",
    "SLL", "SOS", "SRD", "SSP", "STD", "STN", "SYP", "SZL", "THB", "TJS", "TMT", "TND",
    "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "USN", "UYI", "UYU", "UYW",
    "UZS", "VED", "VEF", "VES", "VND", "VUV", "WST", "XAF", "XAG", "XAU", "XBA", "XBB",
    "XBC", "XBD", "XCD", "XDR", "XOF", "XPD", "XPF", "XPT", "XSU", "XTS", "XUA", "XXX",
    "YER", "ZAR", "ZMW"
))
# Public immutable contract reused by deterministic currency parsing. The values remain generated
# from the pinned CLDR snapshot above; consumers must not maintain a second hand-written code list.
ISO4217_CODES = _ISO4217_CODES
_ISO3166_A2 = frozenset((
    "AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT",
    "AU", "AW", "AX", "AZ", "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI",
    "BJ", "BL", "BM", "BN", "BO", "BQ", "BR", "BS", "BT", "BV", "BW", "BY",
    "BZ", "CA", "CC", "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN",
    "CO", "CR", "CU", "CV", "CW", "CX", "CY", "CZ", "DE", "DJ", "DK", "DM",
    "DO", "DZ", "EC", "EE", "EG", "EH", "ER", "ES", "ET", "FI", "FJ", "FK",
    "FM", "FO", "FR", "GA", "GB", "GD", "GE", "GF", "GG", "GH", "GI", "GL",
    "GM", "GN", "GP", "GQ", "GR", "GS", "GT", "GU", "GW", "GY", "HK", "HM",
    "HN", "HR", "HT", "HU", "ID", "IE", "IL", "IM", "IN", "IO", "IQ", "IR",
    "IS", "IT", "JE", "JM", "JO", "JP", "KE", "KG", "KH", "KI", "KM", "KN",
    "KP", "KR", "KW", "KY", "KZ", "LA", "LB", "LC", "LI", "LK", "LR", "LS",
    "LT", "LU", "LV", "LY", "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK",
    "ML", "MM", "MN", "MO", "MP", "MQ", "MR", "MS", "MT", "MU", "MV", "MW",
    "MX", "MY", "MZ", "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP",
    "NR", "NU", "NZ", "OM", "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM",
    "PN", "PR", "PS", "PT", "PW", "PY", "QA", "RE", "RO", "RS", "RU", "RW",
    "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM",
    "SN", "SO", "SR", "SS", "ST", "SV", "SX", "SY", "SZ", "TC", "TD", "TF",
    "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TO", "TR", "TT", "TV", "TW",
    "TZ", "UA", "UG", "UM", "US", "UY", "UZ", "VA", "VC", "VE", "VG", "VI",
    "VN", "VU", "WF", "WS", "YE", "YT", "ZA", "ZM", "ZW"
))
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
