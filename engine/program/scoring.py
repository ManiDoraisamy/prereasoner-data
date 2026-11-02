"""The one score of a program: bias + the sum, over the coordinates it fills, of weight(slot) x reading(coordinate). The additive formula of engine/schema_decode.py, its contributions surfaced.

A Reading is data: coordinate name -> score. A coordinate no one scored reads 0. Today question words fill it (words.py); the encoders plug in by filling the same names. Weights are data too, one per slot, 1 until the chooser fits them, so nothing
here changes when they do. Moving a coordinate's score by d moves the score of every program that fills it by exactly d x its slot's weight.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from engine.program.language import Program, coordinates
from engine.program.names import ROLE_OF_SLOT, SLOTS, slot_of


@dataclass(frozen=True, slots=True)
class Reading:
    """A question read as scores on named coordinates."""
    scores: Mapping[str, float]

    def score(self, name: str) -> float:
        return float(self.scores.get(name, 0.0))


@dataclass(frozen=True, slots=True)
class Weights:
    """One weight per slot, and a bias."""
    by_slot: Mapping[str, float] = field(default_factory=lambda: {slot: 1.0 for slot in SLOTS})
    bias: float = 0.0

    def of(self, slot: str) -> float:
        return float(self.by_slot.get(slot, 0.0))


DEFAULT_WEIGHTS = Weights()


@dataclass(frozen=True, slots=True)
class Contribution:
    """One named term of a program's score."""
    coordinate: str
    slot: str
    score: float
    weight: float

    @property
    def value(self) -> float:
        return self.weight * self.score

    def record(self) -> dict:
        record = {"coordinate": self.coordinate, "slot": self.slot, "score": round(self.score, 6), "weight": self.weight, "contribution": round(self.value, 8)}
        if self.slot in ROLE_OF_SLOT:
            record["word_role"] = ROLE_OF_SLOT[self.slot]
        return record


@dataclass(frozen=True, slots=True)
class ScoredProgram:
    """A program with the score and the named contributions that make it."""
    program: Program
    score: float
    contributions: tuple[Contribution, ...]
    bias: float = 0.0


def score_program(program: Program, reading: Reading, catalog, weights: Weights = DEFAULT_WEIGHTS) -> ScoredProgram:
    """A program's score and its named contributions. catalog says a column's kind, which the program's answer type reads."""
    terms = tuple(Contribution(name, slot_of(name), reading.score(name), weights.of(slot_of(name))) for name in coordinates(program, catalog.column_kind))
    return ScoredProgram(program, weights.bias + sum(term.value for term in terms), terms, weights.bias)
