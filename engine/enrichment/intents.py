"""Conservative, deterministic extraction of requested enrichment attributes."""
from __future__ import annotations

from dataclasses import dataclass
import re

from engine.currency_intent import currency_conversion_target

from engine.enrichment.registry import (
    ATTR_ASSESSMENT,
    ATTR_COUNTRY_METADATA,
    ATTR_COUNTRY_NAME,
    ATTR_CURRENCY_METADATA,
    ATTR_EXCHANGE_RATE,
    ATTR_HOLIDAY,
    ATTR_MEDICAL_METADATA,
    ATTR_PHONE_METADATA,
    ATTR_PLACE_METADATA,
    ATTR_POSTAL_CONTEXT,
    ATTR_TIMEZONE,
    ATTR_UNIT_METADATA,
    ATTR_VAT_RULE,
)


@dataclass(frozen=True)
class IntentEvidence:
    attribute: str
    phrase: str
    rule: str


_RULES = (
    (ATTR_EXCHANGE_RATE, "exchange-rate", re.compile(
        r"\b(?:exchange|fx)\s+rates?\b",
        re.I,
    )),
    (ATTR_CURRENCY_METADATA, "currency-metadata", re.compile(
        r"\b(?:currency\s+(?:name|symbol|code|number)|iso\s*4217|minor\s+units?|decimal\s+places?)\b",
        re.I,
    )),
    (ATTR_COUNTRY_NAME, "country-name", re.compile(
        r"\bcountry\s+names?\b",
        re.I,
    )),
    (ATTR_COUNTRY_METADATA, "country-metadata", re.compile(
        r"\b(?:country\s+codes?|alpha[- ]?[23]\s+(?:country\s+)?codes?|numeric\s+country\s+codes?|iso\s*3166)\b",
        re.I,
    )),
    (ATTR_TIMEZONE, "timezone", re.compile(
        r"\b(?:what|which)\s+(?:is\s+the\s+)?(?:time\s*zone|timezone)\b|"
        r"\b(?:time\s*zone|timezone)\b.{0,25}\b(?:for|of)\b|"
        r"\b(?:find|look\s*up|determine)\b.{0,25}\b(?:time\s*zone|timezone)\b",
        re.I,
    )),
    (ATTR_PHONE_METADATA, "phone-metadata", re.compile(
        r"\b(?:valid(?:ate|ity)|format|possible|calling\s+code|international\s+prefix|national\s+prefix)\b.{0,30}\b(?:phone|telephone|mobile|number)\b|"
        r"\b(?:phone|telephone|mobile)\b.{0,30}\b(?:valid(?:ate|ity)|format|possible|calling\s+code|prefix)\b",
        re.I,
    )),
    (ATTR_POSTAL_CONTEXT, "postal-context", re.compile(
        r"\b(?:city|town|locality|place|state|province|region|latitude|longitude|coordinates?|where)\b.{0,35}\b(?:postal|zip)\s*codes?\b|"
        r"\b(?:postal|zip)\s*codes?\b.{0,35}\b(?:city|town|locality|place|state|province|region|latitude|longitude|coordinates?|located)\b",
        re.I,
    )),
    (ATTR_PLACE_METADATA, "place-metadata", re.compile(
        r"\bgeonames?\b|\bgeoname\s+ids?\b.{0,35}\b(?:name|place|latitude|longitude|coordinates?|population|timezone)\b",
        re.I,
    )),
    (ATTR_VAT_RULE, "vat-rule", re.compile(
        r"\b(?:vat|value[- ]added\s+tax)\b.{0,30}\b(?:rate|rule|category|applicable|effective)\b|"
        r"\b(?:rate|rule|category)\b.{0,20}\b(?:vat|value[- ]added\s+tax)\b",
        re.I,
    )),
    (ATTR_HOLIDAY, "holiday", re.compile(
        r"\b(?:public|bank|national)\s+holidays?\b|\bis\b.{0,30}\b(?:a\s+)?holidays?\b|\bholiday\s+(?:name|date|calendar)\b",
        re.I,
    )),
    (ATTR_MEDICAL_METADATA, "medical-code", re.compile(
        r"\bicd[- ]?10(?:-?cm)?\b|\b(?:medical|diagnosis|procedure)\s+codes?\b.{0,35}\b(?:description|name|parent|category|hierarchy)\b|"
        r"\bcodes?\b.{0,20}\b(?:description|parent|hierarchy)\b",
        re.I,
    )),
    (ATTR_ASSESSMENT, "assessment", re.compile(
        r"\bcommon\s+data\s+elements?\b|\bnih\s+cde\b|\bcde\s+(?:id|name|metadata|form)\b|"
        r"\bassessment\s+forms?\b.{0,30}\b(?:metadata|elements?|questions?|version)\b",
        re.I,
    )),
    (ATTR_UNIT_METADATA, "unit-metadata", re.compile(
        r"\b(?:unit\s+(?:conversion|factor|symbol|name|system)|convert(?:ed|ing)?\s+(?:units?|measurements?))\b",
        re.I,
    )),
)


def requested_attribute_evidence(question: str) -> tuple[IntentEvidence, ...]:
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    normalized = " ".join(question.split())
    evidence = []
    for attribute, rule, pattern in _RULES:
        match = pattern.search(normalized)
        if match is not None:
            evidence.append(IntentEvidence(attribute, match.group(0), rule))
    target = currency_conversion_target(normalized)
    if target is not None and not any(item.attribute == ATTR_EXCHANGE_RATE for item in evidence):
        evidence.append(IntentEvidence(ATTR_EXCHANGE_RATE, target, "currency-conversion"))
    return tuple(evidence)


def requested_attributes(question: str) -> frozenset[str]:
    return frozenset(item.attribute for item in requested_attribute_evidence(question))
