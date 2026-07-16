"""Candidate value type shared by SQL generation and ranking."""
from __future__ import annotations

from dataclasses import dataclass

from engine.sql_ast import Query


@dataclass(frozen=True)
class ScoredQuery:
    query: Query
    sql: str
    score: float
    evidence: tuple[str, ...]
    features: tuple[tuple[str, float], ...] = ()
