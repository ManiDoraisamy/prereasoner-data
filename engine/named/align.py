"""The named alignment: every question word read against every part of a candidate program, each word given to one part or to nothing.

A part is one coordinate a program fills: its form, aggregate, order (direction, entity), an operator (size, row or calc operator, peers, listing, blank), a measure, key or shown column, a condition's column or value, a printed label or band, a period, a comparison or stated number, the sheet (table). Each (word, part) pair has named evidence, the same for every candidate that holds the part:

    align(word, part) = sum over DENSE features f of weight(group, f) x f(word, part)
                      + sum over the part's kept coordinates c of weight(group, c) x question(word, c) x target(c)

DENSE: the reader's own link sum ("named"), the word's rules against the part's own words (exact, the only column holding the word, prefix, a misspelling's sheet word, how much of the part the question covers), the closed lexicon of words.py for an operator, the word's reading of the part's structure name, its role, a stated number or date. A pair with none of that evidence does not align, whatever its weights.

A candidate's alignment: each word that has evidence somewhere in the question goes to its best part when that beats its nothing score, else to nothing; a part no word went to costs its group's unasked weight. Every weight is a name ("measure.exact", "shown.name=biz:amount_money", "nothing.content", "unasked.key"), every score is rounded to 1e-4 before it compares, ties go to the part first by name. Float64 numpy, no random draw: the same reading gives the same alignment on any thread count.

The weights are learned listwise over the decoder's own candidates (training/named/align.py); a bundle carries them as plain numbers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from engine.named.links import SCALE, SUPPORT, is_stated_word
from engine.named.names import NONE, WORD_ROLE_CLASSES, NameSet
from engine.program.catalog import Catalog
from engine.program.index import index_of, stem, words_of
from engine.program.language import TOGETHER, Program, coordinates, period_from_name
from engine.program.names import (ROLE_OF_SLOT, SLOT_AGGREGATE, SLOT_ANSWER_TYPE, SLOT_BLANK, SLOT_CALC_LEFT, SLOT_CALC_OP, SLOT_COMPARISON, SLOT_CONDITIONS, SLOT_CONTAINS, SLOT_DIRECTION, SLOT_ENTITY, SLOT_FILTER, SLOT_FORM, SLOT_KEY, SLOT_LINE, SLOT_LISTING,
                                  SLOT_MEASURE, SLOT_NUMBER, SLOT_PEERS, SLOT_PERIOD, SLOT_ROW_OP, SLOT_SECTION, SLOT_SHOWN, SLOT_SIZE, SLOT_TABLE, SLOT_VALUE, slot_of)
from engine.program.semantics import fold, number_text
from engine.program.words import _ACROSS, _AVG, _BLANK, _CALC_MINUS, _CALC_OVER, _CALC_PLUS, _COMPARE, _COUNT, _DISTINCT, _HIGH, _LOW, _MONTHS, _ROW_MINUS, _ROW_PLUS, _ROW_TIMES, _SIGNED_ZERO, _STOP, _SUM

GROUP_OF_SLOT = {SLOT_FORM: "form", SLOT_AGGREGATE: "aggregate", SLOT_DIRECTION: "order", SLOT_ENTITY: "order", SLOT_SIZE: "operator", SLOT_ROW_OP: "operator", SLOT_CALC_OP: "operator", SLOT_PEERS: "operator", SLOT_LISTING: "operator", SLOT_BLANK: "operator",
                 SLOT_MEASURE: "measure", SLOT_KEY: "key", SLOT_SHOWN: "shown", SLOT_FILTER: "condition_column", SLOT_VALUE: "condition_value", SLOT_CONTAINS: "condition_value", SLOT_LINE: "label", SLOT_SECTION: "label",
                 SLOT_PERIOD: "period", SLOT_NUMBER: "comparison", SLOT_COMPARISON: "comparison", SLOT_TABLE: "sheet"}
GROUPS = ("form", "aggregate", "order", "operator", "measure", "key", "shown", "condition_column", "condition_value", "label", "period", "comparison", "sheet")
# The groups whose parts stand on a target of the index, and so read the target's named coordinates.
LINKED = ("measure", "key", "shown", "condition_column", "condition_value", "label", "sheet")
DENSE = ("named", "exact", "only_column", "prefix", "spelled", "share", "lexicon", "structure", "role", "stated")
# What makes a pair evidence: role alone is how a word reads, not what it says of a part.
EVIDENT = ("exact", "only_column", "prefix", "spelled", "lexicon", "stated")
NOTHING = ("bias", "content", "stated", *(f"role_{name}" for name in WORD_ROLE_CLASSES))
STRUCTURE_MIN = 0.5
ROUND = 4
# The coordinates no word is read against: what the decoder states for bookkeeping, not a part a question asks for.
_SKIPPED = (SLOT_ANSWER_TYPE, SLOT_CONDITIONS, SLOT_CALC_LEFT)
_LEXICON = {"aggregate:sum": (_SUM,), "aggregate:avg": (_AVG,), "aggregate:count": (_COUNT,), "aggregate:countd": (_DISTINCT,), "aggregate:max": (_HIGH,), "aggregate:min": (_LOW,),
            "direction:highest": (_HIGH,), "direction:lowest": (_LOW,), "form:rank": (_HIGH, _LOW), "form:agg": (_SUM, _AVG, _COUNT), "form:calc": (_CALC_MINUS, _CALC_OVER, _CALC_PLUS),
            "calc_op:-": (_CALC_MINUS,), "calc_op:/": (_CALC_OVER,), "calc_op:+": (_CALC_PLUS,), "row_op:*": (_ROW_TIMES,), "row_op:-": (_ROW_MINUS,), "row_op:+": (_ROW_PLUS,), "row_op:/": (_CALC_OVER,),
            "blank:blank": (_BLANK,), "blank:filled": (_BLANK,), **{f"comparison:{op}": (pattern,) for op, pattern in (*_COMPARE, *_SIGNED_ZERO)}}
_STRUCTURE_OF_SLOT = {SLOT_MEASURE: "word_role:measure", SLOT_KEY: "word_role:key", SLOT_SHOWN: "word_role:key", SLOT_FILTER: "word_role:filter", SLOT_VALUE: "condition:equals", SLOT_CONTAINS: "condition:contains", SLOT_LINE: "condition:line",
                      SLOT_SECTION: "condition:section", SLOT_PERIOD: "condition:period", SLOT_NUMBER: "condition:comparison", SLOT_BLANK: "condition:blank", SLOT_TABLE: "word_role:context", SLOT_PEERS: "measure_kind:across", SLOT_LISTING: "answer_type:list"}


def group_of(name: str) -> str | None:
    """The group a part's coordinate reads in; None for what no word is read against."""
    slot = slot_of(name)
    if slot in _SKIPPED or TOGETHER in name:
        return None
    return GROUP_OF_SLOT.get(slot)


def parts_of(program: Program, catalog: Catalog) -> tuple[str, ...]:
    """A program's parts, each once, sorted by name: the order ties fall in."""
    return tuple(sorted({name for name in coordinates(program, catalog.column_kind) if group_of(name) is not None}))


def structure_name(name: str) -> str:
    """The structure-tier name a word reads when it says this part: an operator's own name, a condition's kind, a column's role."""
    slot = slot_of(name)
    if slot in (SLOT_FORM, SLOT_AGGREGATE, SLOT_DIRECTION, SLOT_ENTITY, SLOT_SIZE, SLOT_ROW_OP, SLOT_CALC_OP, SLOT_COMPARISON):
        return name
    return _STRUCTURE_OF_SLOT.get(slot, "")


@dataclass(frozen=True)
class AlignWeights:
    """Every weight by name. A name absent reads 0."""
    values: Mapping[str, float]

    def get(self, name: str) -> float:
        return float(self.values.get(name, 0.0))

    def record(self) -> dict:
        return {name: round(float(value), 6) for name, value in sorted(self.values.items()) if round(float(value), 6) != 0.0}

    @classmethod
    def from_record(cls, record: Mapping[str, float]) -> "AlignWeights":
        return cls({name: float(value) for name, value in record.items()})

    def write(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.record(), indent=0, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def prior_weights(link_rules: Mapping[str, float]) -> AlignWeights:
    """Before any fit: a word aligns on the reader's own link and the same rule weights the reader reads, a content word or a part left alone costs a little. Not a result: training replaces it."""
    values: dict[str, float] = {}
    for group in GROUPS:
        values.update({f"{group}.named": 1.0, f"{group}.exact": float(link_rules.get("exact", 1.0)), f"{group}.only_column": 0.5, f"{group}.prefix": float(link_rules.get("prefix", 1.0)) * 0.5, f"{group}.spelled": 0.5, f"{group}.share": 0.5,
                       f"{group}.lexicon": 1.0, f"{group}.structure": 0.5, f"{group}.role": 0.5, f"{group}.stated": float(link_rules.get("stated", 1.0))})
        values[f"unasked.{group}"] = -0.5 if group in LINKED else -0.1
    values.update({"nothing.content": -0.5, "nothing.stated": -0.5})
    return AlignWeights(values)


@dataclass(frozen=True)
class Compiled:
    """The weights as arrays over one name set: [groups, DENSE], [groups, names], nothing [NOTHING], unasked [groups]."""
    dense: np.ndarray
    names: np.ndarray
    nothing: np.ndarray
    unasked: np.ndarray


def compile_weights(weights: AlignWeights, names: NameSet) -> Compiled:
    dense = np.asarray([[weights.get(f"{group}.{feature}") for feature in DENSE] for group in GROUPS], dtype=np.float64)
    by_name = np.zeros((len(GROUPS), len(names)), dtype=np.float64)
    for key, value in weights.values.items():
        group, _, rest = key.partition(".")
        if rest.startswith("name=") and group in GROUPS:
            at = names.at(rest[5:])
            if at is not None:
                by_name[GROUPS.index(group), at] = value
    return Compiled(dense, by_name, np.asarray([weights.get(f"nothing.{feature}") for feature in NOTHING], dtype=np.float64), np.asarray([weights.get(f"unasked.{group}") for group in GROUPS], dtype=np.float64))


@dataclass(frozen=True)
class PartEvidence:
    """What each word says of one part. dense: [words, DENSE]; ids: the part's target coordinates (name set positions); products: [words, ids] question(word, c) x target(c); evident: [words]."""
    part: str
    group: str
    dense: np.ndarray
    ids: np.ndarray
    products: np.ndarray
    evident: np.ndarray


@dataclass(frozen=True)
class QuestionEvidence:
    """A question's words, what each says of nothing, and which words take part at all (a content word, or one with evidence for some part)."""
    words: tuple[str, ...]
    nothing: np.ndarray
    content: np.ndarray


def _lexicon_words(low: str, words: Sequence[str], patterns) -> np.ndarray:
    """Which words sit inside a match of any of the patterns, by character spans."""
    out = np.zeros(len(words), dtype=bool)
    if not patterns:
        return out
    spans = []
    at = 0
    for word in words:
        start = low.find(word, at)
        if start < 0:
            spans.append((-1, -1))
            continue
        spans.append((start, start + len(word)))
        at = start + len(word)
    for pattern in patterns:
        for match in pattern.finditer(low):
            for position, (start, end) in enumerate(spans):
                if start >= 0 and start < match.end() and end > match.start():
                    out[position] = True
    return out


def _date_tokens(body: str) -> set[str]:
    """The words a period's name can be said by: its years, its months as words and numbers, its days."""
    out: set[str] = set()
    try:
        span = period_from_name(body)
    except (ValueError, IndexError):
        return out
    if span.month is not None:
        out.update({_MONTHS[span.month - 1], str(span.month)})
    for day in (span.start, span.end):
        if not day:
            continue
        year, month, date = (day.split("-") + ["", "", ""])[:3]
        out.add(year)
        if month.isdigit():
            out.update({_MONTHS[int(month) - 1], str(int(month))})
        if date.isdigit() and span.kind == "day":
            out.add(str(int(date)))
    if "-q" in body:
        out.update({f"q{body[-1]}", body[-1]})
    return out


class EvidenceBuilder:
    """The (word, part) evidence of one question over one workbook, each part read once however many candidates hold it."""

    def __init__(self, named, catalog: Catalog, link_weights: np.ndarray, names: NameSet):
        self.named, self.catalog, self.link_weights, self.names = named, catalog, link_weights, names
        read = named.read
        self.words = tuple(read.words)
        self.stems = tuple(stem(word) for word in self.words)
        self.low = fold(read.question).replace("’", "'")
        self.question = read.coordinates.astype(np.float64) / SCALE if len(self.words) else np.zeros((0, len(names)))
        self.roles = read.roles if len(self.words) else np.zeros((0, len(WORD_ROLE_CLASSES)))
        self.stated = np.asarray([is_stated_word(word) for word in self.words], dtype=bool)
        self.content = np.asarray([stem not in _STOP for stem in self.stems], dtype=bool) | self.stated
        self.parts: dict[str, PartEvidence] = {}
        self._targets: dict[str, tuple[str, int]] | None = None
        self._lexicon: dict[str, np.ndarray] = {}
        self._question: QuestionEvidence | None = None

    def targets(self) -> dict[str, tuple[str, int]]:
        if self._targets is None:
            out: dict[str, tuple[str, int]] = {}
            index = self.named.index
            for table_id in sorted(index.views) if index is not None else ():
                for row, target in enumerate(index.views[table_id].targets):
                    for name in target.coordinates():
                        out.setdefault(name, (table_id, row))
            self._targets = out
        return self._targets

    def question_evidence(self) -> QuestionEvidence:
        if self._question is None:
            rows = [[1.0, float(content), float(stated), *[float(value) for value in role]] for content, stated, role in zip(self.content, self.stated, self.roles)]
            self._question = QuestionEvidence(self.words, np.round(np.asarray(rows, dtype=np.float64).reshape(len(self.words), len(NOTHING)), ROUND), self.content.copy())
        return self._question

    def part(self, name: str) -> PartEvidence:
        held = self.parts.get(name)
        if held is None:
            held = self.parts[name] = self._build(name)
        return held

    def _lexicon_for(self, name: str) -> np.ndarray:
        held = self._lexicon.get(name)
        if held is None:
            held = self._lexicon[name] = _lexicon_words(self.low, self.words, _LEXICON.get(name, ()) + ((_ACROSS,) if slot_of(name) == SLOT_PEERS else ()))
        return held

    def _build(self, name: str) -> PartEvidence:
        group = group_of(name)
        slot = slot_of(name)
        count = len(self.words)
        dense = np.zeros((count, len(DENSE)), dtype=np.float64)
        ids = np.zeros(0, dtype=np.int64)
        products = np.zeros((count, 0), dtype=np.float64)
        at = {feature: position for position, feature in enumerate(DENSE)}
        # the part's own words, by kind
        body = name.split(":", 1)[1]
        own: frozenset[str] = frozenset()
        unique: frozenset[str] = frozenset()
        table = None
        if slot in (SLOT_MEASURE, SLOT_KEY, SLOT_SHOWN, SLOT_FILTER):
            table_id, _, position = body.rpartition(".")
            table = self._table(table_id)
            if table is not None and position.lstrip("-").isdigit():
                index = index_of(table)
                own = index.name_words.get(int(position), frozenset()) if int(position) >= 0 else frozenset({"section"})
                unique = frozenset(word for word in own if index.name_counts.get(word, 0) == 1)
        elif slot in (SLOT_VALUE, SLOT_CONTAINS):
            column, _, rest = body.partition("=")
            table = self._table(column.rpartition(".")[0])
            text = rest.rsplit("=", 1)[0] if slot == SLOT_CONTAINS and rest.count("=") >= 1 and rest.rsplit("=", 1)[1] in ("starts", "ends", "anywhere") else rest
            own = frozenset(words_of(text))
        elif slot in (SLOT_LINE, SLOT_SECTION):
            table_id, _, rest = body.partition("=")
            table = self._table(table_id)
            text = rest.rsplit("=", 1)[0] if slot == SLOT_LINE and "=" in rest else rest
            own = frozenset(words_of(text))
        elif slot == SLOT_TABLE:
            table = self._table(body)
            own = index_of(table).title_words if table is not None else frozenset()
        if own:
            spelled_index = index_of(table) if table is not None else None
            covered = sum(word in set(self.stems) for word in own) / len(own)
            for position, word in enumerate(self.stems):
                if word in _STOP and not self.stated[position]:
                    continue
                if word in own:
                    dense[position, at["exact"]] = 1.0
                    dense[position, at["share"]] = covered
                    if word in unique:
                        dense[position, at["only_column"]] = 1.0
                elif len(word) >= 4 and any(held.startswith(word) or (len(held) >= 4 and word.startswith(held)) for held in own):
                    dense[position, at["prefix"]] = 1.0
                elif spelled_index is not None and not word.isdigit():
                    spelled = spelled_index.spelled_as(word)
                    if spelled is not None and spelled in own:
                        dense[position, at["spelled"]] = 1.0
        if slot == SLOT_PERIOD:
            tokens = _date_tokens(body)
            for position, word in enumerate(self.words):
                if self.stated[position] and (word in tokens or word[:3] in tokens):
                    dense[position, at["stated"]] = 1.0
        if slot == SLOT_NUMBER:
            for position, word in enumerate(self.words):
                if word.isdigit() and number_text(float(word)) == body:
                    dense[position, at["stated"]] = 1.0
                elif body == "0" and self._lexicon_for("comparison:>")[position] | self._lexicon_for("comparison:<")[position] | self._lexicon_for("comparison:!=")[position]:
                    dense[position, at["stated"]] = 1.0
        lexicon = self._lexicon_for(name)
        dense[:, at["lexicon"]] = lexicon.astype(np.float64)
        structure = self.names.at(structure_name(name)) if structure_name(name) else None
        if structure is not None and count:
            dense[:, at["structure"]] = self.question[:, structure]
        role = ROLE_OF_SLOT.get(slot)
        if role is not None and count:
            dense[:, at["role"]] = self.roles[:, WORD_ROLE_CLASSES.index(role)]
        target = self.targets().get(name) if group in LINKED else None
        if target is not None and count:
            view = self.named.index.views[target[0]]
            ids = view.ids[target[1]].astype(np.int64)
            values = view.values[target[1]].astype(np.float64) / SCALE
            keep = values > 0
            ids, values = ids[keep], values[keep]
            products = self.question[:, ids] * values[None, :]
            products = np.where(self.content[:, None], products, 0.0)
            dense[:, at["named"]] = products @ self.link_weights[ids]
        dense = np.round(dense, ROUND)
        products = np.round(products, ROUND)
        evident = dense[:, [at[feature] for feature in EVIDENT]].any(axis=1) | (dense[:, at["named"]] >= SUPPORT) | (dense[:, at["structure"]] >= STRUCTURE_MIN)
        return PartEvidence(name, group, dense, ids, products, evident)

    def _table(self, table_id: str):
        try:
            return self.catalog.table(table_id)
        except KeyError:
            return None


@dataclass(frozen=True, slots=True)
class WordAlignment:
    """One word's place in a candidate: its part (or None for nothing), its score, and the named terms behind it."""
    word: str
    at: int
    part: str | None
    score: float
    terms: tuple[tuple[str, float, float], ...]

    def record(self) -> dict:
        return {"word": self.word, "at": self.at, "part": self.part, "score": self.score, "terms": [{"feature": name, "weight": round(weight, 6), "evidence": round(value, 4), "contribution": round(weight * value, 6)} for name, weight, value in self.terms]}


@dataclass(frozen=True)
class Alignment:
    """A candidate's alignment and its three sums: what the aligned words add, what the words left to nothing cost, what the parts nobody asked for cost."""
    words: tuple[WordAlignment, ...]
    unasked: tuple[tuple[str, float], ...]
    aligned: float
    nothing: float
    unasked_total: float

    @property
    def total(self) -> float:
        return round(self.aligned + self.nothing + self.unasked_total, ROUND)

    def features(self) -> dict[str, float]:
        return {"align_words": self.aligned, "align_nothing": self.nothing, "align_unasked": self.unasked_total}

    def record(self) -> dict:
        return {"total": self.total, "aligned": self.aligned, "nothing": self.nothing, "unasked": [{"part": part, "contribution": value} for part, value in self.unasked], "words": [word.record() for word in self.words]}


def pair_scores(evidence: PartEvidence, compiled: Compiled) -> np.ndarray:
    """align(word, part) for every word: rounded, and -inf where the pair has no evidence."""
    g = GROUPS.index(evidence.group)
    score = evidence.dense @ compiled.dense[g]
    if len(evidence.ids):
        score = score + evidence.products @ compiled.names[g, evidence.ids]
    return np.where(evidence.evident, np.round(score, ROUND), -np.inf)


def align_parts(question: QuestionEvidence, parts: Sequence[PartEvidence], compiled: Compiled, *, terms: bool = False, names: NameSet | None = None, weights: AlignWeights | None = None) -> Alignment:
    """The alignment of one candidate, from its parts' evidence. parts must come sorted by name (parts_of): a tie goes to the first. terms: write each word's named terms (needs names and weights)."""
    count = len(question.words)
    nothing = np.round(question.nothing @ compiled.nothing, ROUND) if count else np.zeros(0)
    if parts:
        table = np.stack([pair_scores(part, compiled) for part in parts], axis=1)
    else:
        table = np.full((count, 0), -np.inf)
    taking = question.content | (np.isfinite(table).any(axis=1) if table.size else np.zeros(count, dtype=bool))
    words = []
    used = set()
    aligned = lost = 0.0
    for at in range(count):
        if not taking[at]:
            continue
        best = int(np.argmax(table[at])) if table.shape[1] else -1
        score = float(table[at, best]) if best >= 0 else -np.inf
        if best >= 0 and np.isfinite(score) and score > nothing[at]:
            used.add(best)
            aligned += score
            words.append(WordAlignment(question.words[at], at, parts[best].part, round(score, ROUND), _terms_of(parts[best], at, names, weights) if terms else ()))
        else:
            lost += float(nothing[at])
            words.append(WordAlignment(question.words[at], at, None, round(float(nothing[at]), ROUND), tuple((f"nothing.{feature}", weights.get(f"nothing.{feature}"), float(question.nothing[at, position])) for position, feature in enumerate(NOTHING) if question.nothing[at, position] and weights.get(f"nothing.{feature}")) if terms and weights is not None else ()))
    unasked = tuple((part.part, round(float(compiled.unasked[GROUPS.index(part.group)]), ROUND)) for position, part in enumerate(parts) if position not in used)
    return Alignment(tuple(words), unasked, round(aligned, ROUND), round(lost, ROUND), round(sum(value for _, value in unasked), ROUND))


def _terms_of(part: PartEvidence, at: int, names: NameSet | None, weights: AlignWeights | None) -> tuple[tuple[str, float, float], ...]:
    if names is None or weights is None:
        return ()
    out = [(f"{part.group}.{feature}", weights.get(f"{part.group}.{feature}"), float(part.dense[at, position])) for position, feature in enumerate(DENSE) if part.dense[at, position]]
    out += [(f"{part.group}.name={names.names[coordinate]}", weights.get(f"{part.group}.name={names.names[coordinate]}"), float(part.products[at, position])) for position, coordinate in enumerate(part.ids.tolist()) if part.products[at, position] and weights.get(f"{part.group}.name={names.names[coordinate]}")]
    return tuple(sorted((term for term in out if term[1] * term[2]), key=lambda term: (-abs(term[1] * term[2]), term[0])))


@dataclass
class Aligner:
    """A bundle's alignment weights over its name set, and the reader's link weights the "named" feature sums with."""
    weights: AlignWeights
    names: NameSet
    link_weights: np.ndarray
    compiled: Compiled = field(init=False)

    def __post_init__(self):
        self.compiled = compile_weights(self.weights, self.names)

    def builder(self, named, catalog: Catalog) -> EvidenceBuilder:
        return EvidenceBuilder(named, catalog, self.link_weights, self.names)

    def align(self, program: Program, builder: EvidenceBuilder, catalog: Catalog, *, terms: bool = False) -> Alignment:
        return align_parts(builder.question_evidence(), [builder.part(name) for name in parts_of(program, catalog)], self.compiled, terms=terms, names=self.names, weights=self.weights)


ALIGN_FEATURES = ("align_words", "align_nothing", "align_unasked")

__all__ = ["ALIGN_FEATURES", "Aligner", "AlignWeights", "Alignment", "Compiled", "DENSE", "EVIDENT", "EvidenceBuilder", "GROUPS", "LINKED", "NOTHING", "PartEvidence", "QuestionEvidence", "WordAlignment", "align_parts", "compile_weights", "group_of", "pair_scores", "parts_of", "prior_weights", "structure_name"]
