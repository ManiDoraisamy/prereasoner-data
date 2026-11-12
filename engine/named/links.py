"""The link score, the one formula a word and a target meet through:

    link(span, target) = sum over named coordinates c of weight(c) x question(span, c) x target(c)  +  sum over named rules r of weight(r) x evidence(r)

every term returned by name. question(span, c) is the mean over the span's words. Both sides arrive rounded to thousandths, so a link computes the same from the same reading whatever the thread count. Moving a target's coordinate by d, where the question reads 1, moves the link by exactly d x weight(c): tests.test_named holds that.

Rule evidence is engine/program/words.py's reading of the question's words, read here coordinate by coordinate as the named rule that produced it, so one piece of code matches words. An open channel exists beside this (reader.py); nothing here reads it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from engine.program.index import words_of
from engine.program.names import SLOT_COMPARISON, SLOT_CONTAINS, SLOT_FILTER, SLOT_KEY, SLOT_LINE, SLOT_MEASURE, SLOT_NUMBER, SLOT_PERIOD, SLOT_SECTION, SLOT_SHOWN, SLOT_TABLE, SLOT_VALUE, slot_of

EXACT = "exact"
PARTIAL = "partial"
PREFIX = "prefix"
DATE = "date"
STATED = "stated"
TAB_WORD = "tab_word"
TITLE_WORD = "title_word"
ONLY_OF_TYPE = "only_of_type"
RULES = (EXACT, PARTIAL, PREFIX, DATE, STATED, TAB_WORD, TITLE_WORD, ONLY_OF_TYPE)
# What a coordinate reads when nothing supports it: a column no word links to reads below one a word does. A bundle carries these as learned, per kind of target.
SLOT_BIASES = ((SLOT_MEASURE, 0.5), (SLOT_KEY, 0.5), (SLOT_SHOWN, 0.5))
SLOTS_OF_LINK_KIND = {"column": (SLOT_MEASURE, SLOT_KEY, SLOT_SHOWN), "value": (SLOT_VALUE, SLOT_CONTAINS), "line": (SLOT_LINE,), "band": (SLOT_SECTION,), "table": (SLOT_TABLE,)}
SCALE = 1000
# Below this a link's named sum is noise, not support.
SUPPORT = 0.05
_PREFIX_SCORE = 0.4
_DIGIT = re.compile(r"\d")


@dataclass(frozen=True, slots=True)
class Contribution:
    """One named term of a link."""
    coordinate: str
    weight: float
    question: float
    target: float

    @property
    def value(self) -> float:
        return self.weight * self.question * self.target

    def record(self) -> dict:
        return {"coordinate": self.coordinate, "weight": round(self.weight, 6), "question": round(self.question, 3), "target": round(self.target, 3), "contribution": round(self.value, 6)}


@dataclass(frozen=True, slots=True)
class RuleHit:
    """One piece of named rule evidence."""
    rule: str
    weight: float
    evidence: float

    @property
    def value(self) -> float:
        return self.weight * self.evidence

    def record(self) -> dict:
        return {"rule": self.rule, "weight": round(self.weight, 6), "evidence": round(self.evidence, 3), "contribution": round(self.value, 6)}


@dataclass(frozen=True)
class LinkWeights:
    """Plain numbers: one per coordinate, one per rule, the word role's and the closed heads', and each slot's bias. open_weight applies only in the mode that lets similarity decide."""
    coordinates: np.ndarray
    rules: Mapping[str, float]
    role: float = 1.0
    closed: float = 1.0
    slot_biases: Mapping[str, float] = None
    open_weight: float = 1.0

    def __post_init__(self):
        if self.slot_biases is None:
            object.__setattr__(self, "slot_biases", dict(SLOT_BIASES))

    def bias(self, slot: str) -> float:
        return float(self.slot_biases.get(slot, 0.0))

    def record(self) -> dict:
        return {"coordinates": [round(float(value), 6) for value in self.coordinates], "rules": {rule: round(float(self.rules[rule]), 6) for rule in RULES}, "role": round(self.role, 6), "closed": round(self.closed, 6), "slot_biases": {slot: round(float(value), 6) for slot, value in self.slot_biases.items()}, "open_weight": self.open_weight}

    @classmethod
    def from_record(cls, record: dict) -> "LinkWeights":
        return cls(np.asarray(record["coordinates"], dtype=np.float64), dict(record["rules"]), float(record["role"]), float(record["closed"]), dict(record["slot_biases"]), float(record.get("open_weight", 1.0)))


def uniform_weights(coordinates: int) -> LinkWeights:
    return LinkWeights(np.ones(coordinates, dtype=np.float64), {rule: 1.0 for rule in RULES})


def to_thousandths(scores: np.ndarray) -> np.ndarray:
    """Scores in 0..1 as whole thousandths: what an index keeps and what a reading compares."""
    return np.rint(np.clip(scores, 0.0, 1.0) * SCALE).astype(np.int16)


def named_matrix(question: np.ndarray, ids: np.ndarray, values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Every word against every target: [words, targets] of the named sum. question: [words, coordinates] in thousandths; ids, values: [targets, keep] in thousandths."""
    if not len(ids) or not len(question):
        return np.zeros((len(question), len(ids)), dtype=np.float64)
    weighted = question.astype(np.float64) * weights / SCALE
    return (weighted[:, ids] * (values.astype(np.float64) / SCALE)).sum(axis=-1)


def contributions_of(span_question: np.ndarray, ids: np.ndarray, values: np.ndarray, weights: np.ndarray, names: Sequence[str]) -> tuple[Contribution, ...]:
    """A span's named terms against one target, largest first, ties by name. span_question: [coordinates] in thousandths, the mean over the span's words."""
    out = []
    for coordinate_id, value in zip(ids.tolist(), values.tolist()):
        if value <= 0 or span_question[coordinate_id] <= 0:
            continue
        out.append(Contribution(names[coordinate_id], float(weights[coordinate_id]), float(span_question[coordinate_id]) / SCALE, value / SCALE))
    return tuple(sorted(out, key=lambda term: (-term.value, term.coordinate)))


def link_total(contributions: Sequence[Contribution], rules: Sequence[RuleHit]) -> float:
    """The link: the sum of its named terms and its rule evidence, nothing else."""
    return sum(term.value for term in contributions) + sum(hit.value for hit in rules)


def rule_evidence(name: str, score: float, question_words: set[str], table=None) -> tuple[tuple[str, float], ...]:
    """A coordinate of the words reading as the named rule behind it: (rule, evidence in 0..1). words.py scores a whole value 1, a part by what the question covers, a word's prefix 0.4 inside a longer cell."""
    slot = slot_of(name)
    if score <= 0:
        return ()
    if slot in (SLOT_MEASURE, SLOT_KEY, SLOT_SHOWN, SLOT_FILTER, SLOT_LINE, SLOT_SECTION):
        return ((EXACT, 1.0),) if score >= 1.0 else ((PARTIAL, min(score, 1.0)),)
    if slot == SLOT_VALUE:
        return ((EXACT, 1.0),) if score >= 1.0 else ((PARTIAL, min(score / 0.8, 1.0)),)
    if slot == SLOT_CONTAINS:
        return ((PREFIX, 1.0),) if score == _PREFIX_SCORE else ((PARTIAL, min(score, 1.0)),)
    if slot == SLOT_PERIOD:
        return ((DATE, min(score, 1.0)),)
    if slot in (SLOT_NUMBER, SLOT_COMPARISON):
        return ((STATED, min(score, 1.0)),)
    if slot == SLOT_TABLE and table is not None:
        tab = set(words_of(table.tab)) & question_words
        return ((TAB_WORD, min(score, 1.0)),) if tab else ((TITLE_WORD, min(score, 1.0)),)
    return ()


def words_in_text(words: Sequence[str], text: str) -> tuple[int, ...]:
    """Which of the question's words (by position) a target's own text holds."""
    have = set(words_of(text))
    return tuple(at for at, word in enumerate(words) if word in have or any(len(word) >= 3 and held.startswith(word) for held in have))


def is_stated_word(word: str) -> bool:
    """A number: a word the question states rather than links. A month written in words is the learned readers' to read."""
    return bool(_DIGIT.search(word))


__all__ = ["Contribution", "DATE", "EXACT", "LinkWeights", "ONLY_OF_TYPE", "PARTIAL", "PREFIX", "RULES", "RuleHit", "SCALE", "SLOTS_OF_LINK_KIND", "SLOT_BIASES", "STATED", "SUPPORT", "TAB_WORD", "TITLE_WORD", "contributions_of", "is_stated_word", "link_total", "named_matrix", "rule_evidence", "to_thousandths", "uniform_weights", "words_in_text"]
