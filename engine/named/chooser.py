"""The chooser: a multinomial logistic model over named evidence among a question's candidates, its contributions shown, calibrated per form on held-out families. It replaces a cross-encoder ranker: no text is read here, only the evidence below by name.

logit = bias + sum over features of weight x evidence. A probability is the chosen candidate's logit through its form's calibration. answer_or_ask then answers, or asks: a program with a link no name supports is always asked about (named or asked); so is a probability under the form's threshold, and two readings with different answers within the margin in logits, as "X or Y?".
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from engine.named.links import EXACT, PARTIAL, PREFIX, is_stated_word
from engine.named.names import NONE, WORD_ROLE_CLASSES
from engine.named.reader import LINK_SLOTS, MODE_NAMED, NamedReading, needs_asking, unsupported_links
from engine.program.catalog import Catalog
from engine.program.index import stem
from engine.program.language import Agg, Col, Program, Rank, Show, coordinates, form_of, tables_of
from engine.program.names import (ENTITY_GROUP, ENTITY_LINE, EV_ADDS_TWICE, EV_BARE_ZERO, EV_EMPTY, EV_FAILED, EV_FILTER_DID_NOTHING, EV_FRACTION_FOR_COUNT, EV_MANY_ROWS, EV_PRINTED_TIE, EV_REPEATED_LINES, EV_ROW_COUNT_FOR_AMOUNT, EV_ROW_DUMP, EV_SHAPE_AGREES, EV_TOO_LARGE_FOR_SHARE, EV_TYPE_AGREES, EV_WRONG_KIND, FORMS, SLOT_CONDITIONS, SLOT_CONTAINS, SLOT_FILTER, SLOT_FORM, SLOT_KEY, SLOT_LINE, SLOT_MEASURE, SLOT_SECTION, SLOT_SHOWN, SLOT_VALUE, slot_of)
from engine.program.words import _STOP

if TYPE_CHECKING:
    from engine.program.verifier import Verified

_KINDS = {SLOT_MEASURE: "measure", SLOT_KEY: "key", SLOT_SHOWN: "shown", SLOT_VALUE: "value", SLOT_CONTAINS: "contains", SLOT_LINE: "line", SLOT_SECTION: "section"}
_FLAGS = {EV_TYPE_AGREES: "type_agrees", EV_SHAPE_AGREES: "shape_agrees", EV_PRINTED_TIE: "printed_tie", EV_BARE_ZERO: "bare_zero", EV_EMPTY: "empty", EV_FAILED: "failed", EV_ROW_DUMP: "row_dump", EV_MANY_ROWS: "many_rows", EV_WRONG_KIND: "wrong_kind", EV_ROW_COUNT_FOR_AMOUNT: "row_count_for_amount", EV_FRACTION_FOR_COUNT: "fraction_for_count", EV_TOO_LARGE_FOR_SHARE: "too_large_for_share", EV_FILTER_DID_NOTHING: "filter_did_nothing", EV_REPEATED_LINES: "repeated_lines", EV_ADDS_TWICE: "adds_twice"}
FEATURES = ("fine_type", "shown_columns", "words_explained", "words_unexplained", "parts_asked", "parts_unasked", "link_mean", "link_min", *(f"link_{kind}" for kind in _KINDS.values()), "rule_exact", "rule_partial", "rule_prefix", *_FLAGS.values(), "form_prior", "decoder_score", "rank_inverse", "score_margin", "conditions", "class_agreement", "needs_asking", "open_similarity")
ROLE_MIN = 0.5
SUPPORT_NOTE = "no_named_support"
ASK_LOW = "low_confidence"
ASK_CLOSE = "close_readings"
ASK_NOTHING = "nothing_ran"
ASK_ADDS_TWICE = "adds_twice"
ANSWER = "answer"
ASK = "ask"


@dataclass(frozen=True, slots=True)
class FeatureTerm:
    """One named term of a candidate's logit."""
    feature: str
    weight: float
    evidence: float

    @property
    def value(self) -> float:
        return self.weight * self.evidence

    def record(self) -> dict:
        return {"feature": self.feature, "weight": round(self.weight, 6), "evidence": round(self.evidence, 4), "contribution": round(self.value, 6)}


@dataclass(frozen=True)
class Chooser:
    """Weights by feature, a bias, and for each form a calibration (a, b: p = sigmoid(a x logit + b)) and an answer threshold."""
    weights: Mapping[str, float]
    bias: float = 0.0
    calibration: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    thresholds: Mapping[str, float] = field(default_factory=dict)
    margin: float = 0.05

    def logit(self, features: Mapping[str, float]) -> tuple[float, tuple[FeatureTerm, ...]]:
        terms = tuple(FeatureTerm(name, float(self.weights.get(name, 0.0)), float(features.get(name, 0.0))) for name in FEATURES)
        return self.bias + sum(term.value for term in terms), tuple(term for term in terms if term.value)

    def probability(self, logit: float, form: str) -> float:
        a, b = self.calibration.get(form, (1.0, 0.0))
        linear = max(min(a * logit + b, 700.0), -700.0)
        return round(1.0 / (1.0 + math.exp(-linear)), 4)

    def threshold(self, form: str) -> float:
        return float(self.thresholds.get(form, 0.5))

    def record(self) -> dict:
        return {"features": list(FEATURES), "weights": {name: round(float(self.weights.get(name, 0.0)), 6) for name in FEATURES}, "bias": round(self.bias, 6), "calibration": {form: list(self.calibration[form]) for form in sorted(self.calibration)}, "thresholds": dict(sorted(self.thresholds.items())), "margin": self.margin}

    @classmethod
    def from_record(cls, record: dict) -> "Chooser":
        return cls(dict(record["weights"]), float(record["bias"]), {form: tuple(pair) for form, pair in record.get("calibration", {}).items()}, dict(record.get("thresholds", {})), float(record.get("margin", 0.05)))

    def write(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.record(), indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


# Before any fit: the signs the evidence should have, small. Not a result: a fitted chooser replaces it.
PRIOR = Chooser({"words_explained": 2.0, "words_unexplained": -1.0, "parts_asked": 1.0, "parts_unasked": -1.0, "link_mean": 1.0, "link_min": 0.5, "rule_exact": 0.5, "type_agrees": 1.0, "shape_agrees": 1.0, "printed_tie": 1.0, "bare_zero": -1.0, "empty": -3.0, "failed": -5.0, "adds_twice": -1.0, "wrong_kind": -2.0, "row_dump": -1.0, "many_rows": -1.0, "form_prior": 0.5, "decoder_score": 0.1, "rank_inverse": 0.5, "needs_asking": -1.0}, -1.0)


def explained_words(program: Program, named: NamedReading, catalog: Catalog) -> set[int]:
    """The question's words this program's links stand on."""
    out: set[int] = set()
    for name in coordinates(program, catalog.column_kind):
        evidence = named.book.get(name)
        if evidence is not None:
            out.update(evidence.span)
    return out


def answer_column(program: Program) -> Col | None:
    """The one column a program answers from, where it has one."""
    if isinstance(program, Show):
        return program.shown[0]
    if isinstance(program, Agg):
        return program.measure if isinstance(program.measure, Col) else None
    if isinstance(program, Rank):
        return program.shown[0] if program.entity == ENTITY_LINE else program.key if program.entity == ENTITY_GROUP else None
    return None


def features_of(verified: Verified, named: NamedReading, catalog: Catalog, *, rank: int, best_score: float) -> dict[str, float]:
    """The named evidence of one candidate, rounded so a comparison never turns on a float's last bits."""
    program = verified.scored.program
    words = named.read.words
    content = [at for at, word in enumerate(words) if stem(word) not in _STOP and not is_stated_word(word)]
    roles = named.read.roles
    none_at = WORD_ROLE_CLASSES.index(NONE)
    roleful = [at for at in content if roles[at, none_at] < ROLE_MIN]
    explained = explained_words(program, named, catalog)
    names = coordinates(program, catalog.column_kind)
    links = [name for name in names if slot_of(name) in LINK_SLOTS]
    strengths = []
    by_kind: dict[str, list[float]] = {}
    exact = partial = prefix = 0.0
    open_top = 0.0
    for name in links:
        evidence = named.book.get(name)
        if evidence is None:
            strengths.append(0.0)
            continue
        strength = sum(term.value for term in evidence.contributions) + sum(hit.value for hit in evidence.rules)
        strengths.append(strength)
        by_kind.setdefault(_KINDS[slot_of(name)], []).append(strength)
        open_top = max(open_top, evidence.open)
        for hit in evidence.rules:
            exact += hit.rule == EXACT
            partial += hit.rule == PARTIAL
            prefix += hit.rule == PREFIX
    parts = [name for name in names if not name.startswith(f"{SLOT_FORM}:") and slot_of(name) != SLOT_CONDITIONS]
    asked = [name for name in parts if named.reading.score(name) > 0]
    column = answer_column(program)
    features = {
        "fine_type": named.fine_support(column.table, column.position) if column is not None else 0.0,
        "shown_columns": float(max(len(getattr(program, "shown", ())) - 1, 0)),
        "words_explained": len(explained & set(content)) / max(1, len(content)),
        "words_unexplained": len(set(roleful) - explained) / max(1, len(content)),
        "parts_asked": len(asked) / max(1, len(parts)),
        "parts_unasked": (len(parts) - len(asked)) / max(1, len(parts)),
        "link_mean": sum(strengths) / len(strengths) if strengths else 0.0,
        "link_min": min(strengths) if strengths else 0.0,
        "rule_exact": exact, "rule_partial": partial, "rule_prefix": prefix,
        "form_prior": named.form_prior(form_of(program)),
        "decoder_score": verified.scored.score,
        "rank_inverse": 1.0 / (1.0 + rank),
        "score_margin": verified.scored.score - best_score,
        "conditions": float(len([name for name in names if slot_of(name) in (SLOT_FILTER, SLOT_VALUE, SLOT_CONTAINS, SLOT_LINE, SLOT_SECTION)])),
        "class_agreement": max((named.class_agreement.get(table, 0.0) for table in tables_of(program)), default=0.0),
        "needs_asking": 1.0 if needs_asking(program, named, catalog) else 0.0,
        "open_similarity": open_top if named.mode != MODE_NAMED else 0.0,
    }
    for kind, values in by_kind.items():
        features[f"link_{kind}"] = sum(values) / len(values)
    for item in verified.evidence:
        name = _FLAGS.get(item.name)
        if name is not None and item.holds:
            features[name] = 1.0
    return {name: round(float(value), 4) for name, value in features.items()}


@dataclass(frozen=True, slots=True)
class Candidate:
    """A verified program with its features, logit and probability."""
    verified: Verified
    features: Mapping[str, float]
    logit: float
    probability: float
    terms: tuple[FeatureTerm, ...]
    needs_asking: bool
    rank: int

    @property
    def rows(self) -> tuple[tuple, ...]:
        return self.verified.result.rows if self.verified.result is not None else ()

    def record(self) -> dict:
        return {"probability": self.probability, "logit": round(self.logit, 4), "needs_asking": self.needs_asking, "terms": [term.record() for term in self.terms], "rank": self.rank}


@dataclass(frozen=True, slots=True)
class Decision:
    """Answer or ask. candidate: what was chosen or best; other: the reading a question sets it against; reasons says why it asks."""
    kind: str
    candidate: Candidate | None
    other: Candidate | None
    reasons: tuple[str, ...]


def score_candidates(verified: Sequence[Verified], named: NamedReading, catalog: Catalog, chooser: Chooser) -> list[Candidate]:
    """Every candidate scored, best first: rounded logit, then the decoder's order. Stable: no tie depends on anything but those two."""
    best = verified[0].scored.score if verified else 0.0
    out = []
    for rank, item in enumerate(verified):
        features = features_of(item, named, catalog, rank=rank, best_score=best)
        logit, terms = chooser.logit(features)
        logit = round(logit, 4)
        out.append(Candidate(item, features, logit, chooser.probability(logit, form_of(item.scored.program)), terms, bool(features["needs_asking"]), rank))
    out.sort(key=lambda candidate: (-candidate.logit, candidate.rank))
    return out


def _same_answer(a: Candidate, b: Candidate) -> bool:
    return _answer_key(a.rows) == _answer_key(b.rows)


def _answer_key(rows) -> tuple:
    def cell(value):
        return round(float(value), 6) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
    return tuple(tuple(cell(value) for value in row) for row in rows)


def reasons_to_ask(probability: float, form: str, gap: float | None, needs_support: bool, adds_twice: bool, chooser: Chooser) -> tuple[str, ...]:
    """The rule itself. gap: how far, in logits, the best reading stands above the best with a different answer."""
    reasons = []
    if needs_support:
        reasons.append(SUPPORT_NOTE)
    if adds_twice:
        reasons.append(ASK_ADDS_TWICE)
    if probability < chooser.threshold(form):
        reasons.append(ASK_LOW)
    if gap is not None and gap < chooser.margin:
        reasons.append(ASK_CLOSE)
    return tuple(reasons)


def answer_or_ask(candidates: Sequence[Candidate], chooser: Chooser) -> Decision:
    """The one decision after the reader. Asked when nothing ran; when the best reading has a link no name supports; when it would add a family's repeated lines twice; when its calibrated probability is under its form's threshold; when another reading of a different answer sits within the margin."""
    ran = [candidate for candidate in candidates if candidate.rows]
    if not ran:
        return Decision(ASK, None, None, (ASK_NOTHING,))
    best = ran[0]
    other = next((candidate for candidate in ran[1:] if not _same_answer(best, candidate)), None)
    reasons = reasons_to_ask(best.probability, form_of(best.verified.scored.program), best.logit - other.logit if other is not None else None, best.needs_asking, best.verified.held(EV_ADDS_TWICE), chooser)
    return Decision(ASK if reasons else ANSWER, best, other, reasons)


__all__ = ["ANSWER", "ASK", "ASK_ADDS_TWICE", "Candidate", "Chooser", "Decision", "FEATURES", "FORMS", "FeatureTerm", "PRIOR", "answer_or_ask", "features_of", "reasons_to_ask", "score_candidates", "unsupported_links"]
