"""Candidate value type shared by SQL generation and ranking."""
from __future__ import annotations

from dataclasses import dataclass

from engine.sql_ast import Query


@dataclass(frozen=True)
class Requirement:
    """One HARD thing the question demands of the answer, and whether this candidate delivers it.

    A requirement is not a preference. A preference nudges the ranking and the best available
    candidate still answers the question asked; an UNMET requirement means the candidate answers a
    DIFFERENT question, so presenting its number is a wrong answer rather than a weak one. Ranking
    alone cannot express that: a penalty applied to every candidate cancels out, and serving takes
    ``candidates[0]`` unconditionally.

    ``proposal`` is written by the producer, which is the only party that knows what the requirement
    is about. Serving reads it verbatim. That split is what keeps the decline policy scenario-
    agnostic: a future producer of a non-currency requirement inherits the decline with no change to
    the serving gate.
    """

    name: str                        # stable family id, e.g. "currency_conversion"
    detail: str                      # the concrete thing needed, e.g. "rate_to_eur"
    satisfied: bool
    requested: str = ""              # what the question asked for, e.g. "EUR"
    available: tuple[str, ...] = ()  # what the attached data could satisfy instead, e.g. ("USD",)
    proposal: str = ""               # a rephrasing the data CAN answer; "" when there is none


@dataclass(frozen=True)
class ScoredQuery:
    query: Query
    sql: str
    score: float
    evidence: tuple[str, ...]
    features: tuple[tuple[str, float], ...] = ()
    requirements: tuple[Requirement, ...] = ()
