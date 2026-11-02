"""A question read onto the program decoder's coordinates.

Each coordinate of a reading is a sum of named parts: the link of the words that reach it (links.py), the word's role, the slot's bias, a closed head's log odds. The book keeps every part by coordinate, so a score reads back as its terms.

The open channel: an unnamed projection and a dot product. It may put a target before the decoder when nothing named reaches it, at one fixed small score, so its similarity orders nothing: a program resting on such a link is marked needs_asking and is not answered. In MODE_NAMED_PLUS_OPEN alone, kept for the comparison the owner was promised, the similarity joins the sum and may decide. Named-only is the engine; the other mode is a measurement.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import torch

from engine.named.backbone import TextBackbone
from engine.named.index import ClassSignatures, TableView, WorkbookIndex
from engine.named.links import ONLY_OF_TYPE, SCALE, SUPPORT, Contribution, LinkWeights, RuleHit, contributions_of, named_matrix, rule_evidence, to_thousandths, words_in_text
from engine.named.model import NamedReader, ReaderConfig
from engine.named.names import CALC_SIDES, CLOSED_HEADS, FINE_OF_NAME, LEFT, NONE, WORD_ROLE_CLASSES, NameSet
from engine.named.units import T_BAND, T_COLUMN, T_LINE, T_TABLE, T_VALUE, collate, question_graph
from engine.program.catalog import Catalog
from engine.program.decoder import Decoder, table_reach
from engine.program.index import index_of, stem
from engine.program.language import Col, Program, SECTION, coordinates
from engine.program.names import (CALC, MINUS, OVER, ROLE_OF_SLOT, SIGNED, SLOT_ANSWER_TYPE, SLOT_BLANK, SLOT_CALC_LEFT, SLOT_COMPARISON, SLOT_CONDITIONS, SLOT_CONTAINS, SLOT_FILTER, SLOT_FORM, SLOT_KEY, SLOT_LINE, SLOT_MEASURE, SLOT_NUMBER, SLOT_PERIOD, SLOT_ROW_OP, SLOT_SECTION, SLOT_SHOWN, SLOT_SIZE, SLOT_TABLE, SLOT_VALUE, coarse_type, coordinate, slot_of)
from engine.program.scoring import Reading
from engine.program.words import _STOP, FILTER_NAME_SHARE, read_question

MODE_NAMED = "named"
MODE_NAMED_PLUS_OPEN = "named_plus_open"
MODES = (MODE_NAMED, MODE_NAMED_PLUS_OPEN)

# Where a program's links stand. A filter column is carried by its value's link; a period or a number is stated, not linked.
LINK_SLOTS = (SLOT_MEASURE, SLOT_KEY, SLOT_SHOWN, SLOT_VALUE, SLOT_CONTAINS, SLOT_LINE, SLOT_SECTION)
PROPOSAL_SCORE = 0.01
OPEN_MIN = 0.3
OPEN_TOP = 3
VALUE_TOP = 64
LOG_FLOOR = -6.0
# Frames an unlikely head would open cost search time for nothing: below this a head's class writes no coordinate.
HEAD_MIN = 0.02
LEFT_MIN = 0.5
_STATED_SLOTS = (SLOT_PERIOD, SLOT_NUMBER, SLOT_COMPARISON, SLOT_CONTAINS, SLOT_VALUE)


@dataclass(slots=True)
class QuestionRead:
    """What the encoder said of a question, rounded. coordinates: [words, names] in thousandths; roles, sides: [words, classes]; closed: head -> class -> probability; open: [words, open_dim] int8."""
    question: str
    words: list[str]
    coordinates: np.ndarray
    roles: np.ndarray
    sides: np.ndarray
    closed: dict[str, dict[str, float]]
    open: np.ndarray


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything behind one coordinate's score."""
    coordinate: str
    span: tuple[int, ...]
    contributions: tuple[Contribution, ...]
    rules: tuple[RuleHit, ...]
    role: float
    bias: float
    open: float
    proposal: bool
    score: float

    @property
    def supported(self) -> bool:
        """A named term or a rule reached this coordinate. Similarity alone never makes this true."""
        return bool(self.rules) or sum(term.value for term in self.contributions) >= SUPPORT

    def record(self) -> dict:
        return {"coordinate": self.coordinate, "span": list(self.span), "score": self.score, "contributions": [term.record() for term in self.contributions], "rules": [hit.record() for hit in self.rules], "role": round(self.role, 6), "bias": self.bias, "open": round(self.open, 3), "proposal": self.proposal, "supported": self.supported}


@dataclass(slots=True)
class NamedReading:
    """The decoder's Reading, the book behind it, and the closed heads' probabilities."""
    read: QuestionRead
    reading: Reading
    book: dict[str, Evidence]
    mode: str
    class_agreement: dict[str, float]
    index: WorkbookIndex | None = None
    names: NameSet | None = None
    reach: dict[str, float] = field(default_factory=dict)

    @property
    def expected(self) -> str:
        """The answer type a program can state, which the head's classes add up to."""
        return _coarse_best(self.read.closed[SLOT_ANSWER_TYPE])

    @property
    def expected_fine(self) -> str:
        """What the question asks for as finely as the head reads it: its best class within the coarse type."""
        head = self.read.closed[SLOT_ANSWER_TYPE]
        coarse = _coarse_best(head)
        return max(sorted(name for name in head if coarse_type(name) == coarse), key=lambda name: head[name])

    def fine_support(self, table_id: str, position: int) -> float:
        """How far a column's named coordinates say it holds what the question asks for, in 0..1. Zero where the fine type has no names, or the column none of them."""
        if self.index is None or self.names is None:
            return 0.0
        view = self.index.views.get(table_id)
        if view is None:
            return 0.0
        fine = self.expected_fine
        for target, ids, values in zip(view.targets, view.ids, view.values):
            if target.kind == T_COLUMN and target.position == position:
                return max((value / SCALE for coordinate_id, value in zip(ids.tolist(), values.tolist()) if FINE_OF_NAME.get(self.names.names[coordinate_id]) == fine), default=0.0)
        return 0.0

    def form_prior(self, form: str) -> float:
        return float(np.log(max(self.read.closed[SLOT_FORM].get(form, 0.0), 1e-6)))


def _coarse_best(head: dict[str, float]) -> str:
    totals: dict[str, float] = {}
    for name, p in head.items():
        totals[coarse_type(name)] = totals.get(coarse_type(name), 0.0) + p
    return max(sorted(totals), key=lambda name: totals[name])


def read_questions(backbone: TextBackbone, reader: NamedReader, config: ReaderConfig, questions: list[str]) -> list[QuestionRead]:
    """The same backbone and the same head over each question alone."""
    out = []
    with torch.inference_mode():
        for question, (words, vectors) in zip(questions, backbone.encode_questions(questions, max_tokens=config.question_tokens)):
            graph = question_graph(words)
            inputs, edges, pad = collate([graph], [vectors if len(words) else torch.zeros((1, backbone.dim))])
            hidden = reader.hidden(inputs, edges, pad)
            closed = reader.closed_logits(hidden, pad)
            probabilities = {head: {name: round(float(p), 4) for name, p in zip(CLOSED_HEADS[head], torch.softmax(logits[0], -1).tolist())} for head, logits in closed.items()}
            word_hidden = hidden[0, :len(words)]
            scores = torch.sigmoid(reader.coordinate_logits(word_hidden)).numpy()
            opens = reader.open_word(word_hidden).numpy()
            norm = np.maximum(np.linalg.norm(opens, axis=1, keepdims=True), 1e-9)
            out.append(QuestionRead(question, words, to_thousandths(scores), np.round(torch.softmax(reader.word_role(word_hidden), -1).numpy().astype(np.float64), 3), np.round(torch.softmax(reader.calc_side(word_hidden), -1).numpy().astype(np.float64), 3), probabilities, np.rint(opens / norm * 127.0).astype(np.int8)))
    return out


def closed_reading(read: QuestionRead, weights: LinkWeights) -> dict[str, float]:
    """A head's class as a coordinate: its log probability, or where the head can stay silent its log odds against silence, so the sum over a program's filled heads is the log likelihood of all heads up to one constant per question. Conditions, per table, are added by the caller."""
    out = {}
    totals: dict[str, float] = {}
    for name, p in read.closed[SLOT_ANSWER_TYPE].items():
        totals[coarse_type(name)] = totals.get(coarse_type(name), 0.0) + p
    for name, p in totals.items():
        out[coordinate(SLOT_ANSWER_TYPE, name)] = weights.closed * max(float(np.log(max(p, 1e-6))), LOG_FLOOR)
    for head, classes in read.closed.items():
        if head in (SLOT_CONDITIONS, SLOT_ANSWER_TYPE):
            continue
        silent = NONE if NONE in classes else SIGNED if head == SLOT_SIZE else None
        floor = float(np.log(max(classes[silent], 1e-6))) if silent else 0.0
        for name, p in classes.items():
            if name == silent:
                continue
            if head in (SLOT_ROW_OP, SLOT_BLANK, SLOT_COMPARISON) or (head == SLOT_FORM and name == CALC):
                if p < HEAD_MIN:
                    continue
            out[coordinate(head, name)] = weights.closed * (max(float(np.log(max(p, 1e-6))), LOG_FLOOR) - floor)
    return out


def _span_of(row: np.ndarray, content: np.ndarray) -> tuple[int, ...]:
    """The words a link stands on: the best word, and neighbours reading at least half of it."""
    masked = np.where(content, row, 0.0)
    best = int(np.argmax(masked))
    if masked[best] <= 0:
        return ()
    span = [best]
    for step in (-1, 1):
        at = best + step
        while 0 <= at < len(row) and content[at] and masked[at] >= 0.5 * masked[best]:
            span.append(at)
            at += step
    return tuple(sorted(span))


class _Builder:
    def __init__(self, read: QuestionRead, windex: WorkbookIndex, catalog: Catalog, decoder: Decoder, names: NameSet, weights: LinkWeights, mode: str):
        self.read, self.windex, self.catalog, self.decoder, self.names, self.weights, self.mode = read, windex, catalog, decoder, names, weights, mode
        self.stems = {stem(word) for word in read.words}
        self.content = np.asarray([stem(word) not in _STOP for word in read.words], dtype=bool)
        self.scores: dict[str, float] = {}
        self.book: dict[str, Evidence] = {}
        self.floor = read_question(read.question, catalog, decoder).scores
        self.rule_values = {name: score for name, score in self.floor.items() if slot_of(name) in (SLOT_MEASURE, SLOT_KEY, SLOT_SHOWN, SLOT_FILTER, SLOT_VALUE, SLOT_CONTAINS, SLOT_LINE, SLOT_SECTION, SLOT_PERIOD, SLOT_NUMBER, SLOT_COMPARISON, SLOT_TABLE)}

    def rules_for(self, name: str, table=None) -> tuple[RuleHit, ...]:
        score = self.rule_values.get(name)
        if score is None:
            return ()
        return tuple(RuleHit(rule, float(self.weights.rules[rule]), evidence) for rule, evidence in rule_evidence(name, score, self.stems, table))

    def role_of(self, span: tuple[int, ...], slot: str) -> float:
        role = ROLE_OF_SLOT.get(slot)
        if not span or role is None:
            return 0.0
        return float(self.read.roles[list(span), WORD_ROLE_CLASSES.index(role)].mean())

    def write(self, name: str, span: tuple[int, ...], terms: tuple[Contribution, ...], rules: tuple[RuleHit, ...], open_score: float, proposal: bool) -> None:
        slot = slot_of(name)
        role = self.weights.role * self.role_of(span, slot) if (terms or rules) else 0.0
        bias = self.weights.bias(slot)
        named = sum(term.value for term in terms)
        if slot == SLOT_FILTER:                       # a column's name says little of a condition until a value is read there too: the role stays with the value
            named *= FILTER_NAME_SHARE
            role = 0.0
        total = named + sum(hit.value for hit in rules) + role - bias
        if proposal:
            total = PROPOSAL_SCORE                    # one fixed score: a proposal comes before the decoder and its similarity orders nothing
        if self.mode == MODE_NAMED_PLUS_OPEN:
            total += self.weights.open_weight * max(open_score, 0.0)
        total = round(total, 4)
        self.scores[name] = total
        self.book[name] = Evidence(name, span, terms, rules, role, bias, open_score, proposal and self.mode == MODE_NAMED, total)

    def table(self, table_id: str, view: TableView, n: np.ndarray, dots: np.ndarray) -> None:
        catalog_table = self.catalog.table(table_id)
        index = index_of(catalog_table)
        question = self.read.coordinates
        keep = view.ids
        best = n.max(axis=0) if n.size else np.zeros(len(view.targets))
        proposals = self._proposals(view, dots, best, catalog_table)
        numbers = list(index.number_columns)
        texts = list(index.text_columns)
        value_rows = [at for at, target in enumerate(view.targets) if target.kind in (T_VALUE, T_LINE, T_BAND)]
        if len(value_rows) > VALUE_TOP:
            value_rows = sorted(sorted(value_rows, key=lambda at: (-round(float(best[at]), 4), view.targets[at].text, catalog_table.column_rank.get(view.targets[at].position, -1)))[:VALUE_TOP])
        wanted = set(value_rows) | {at for at, target in enumerate(view.targets) if target.kind in (T_COLUMN, T_TABLE)} | set(proposals)
        for at, target in enumerate(view.targets):
            names = target.coordinates()
            if at not in wanted and not any(name in self.rule_values for name in names):
                continue
            span = _span_of(n[:, at], self.content) if n.size else ()
            terms = ()
            if span and best[at] > 0:
                terms = contributions_of(question[list(span)].mean(axis=0), keep[at], view.values[at], self.weights.coordinates, self.names.names)
            open_score = float(dots[:, at].max()) if dots.size else 0.0
            for name in names:
                rules = self.rules_for(name, catalog_table)
                if target.kind == T_COLUMN and slot_of(name) == SLOT_MEASURE and numbers == [target.position]:
                    rules += (RuleHit(ONLY_OF_TYPE, float(self.weights.rules[ONLY_OF_TYPE]), 1.0),)
                if target.kind == T_COLUMN and slot_of(name) in (SLOT_KEY, SLOT_SHOWN) and texts == [target.position]:
                    rules += (RuleHit(ONLY_OF_TYPE, float(self.weights.rules[ONLY_OF_TYPE]), 1.0),)
                if rules and not span:
                    span = words_in_text(self.read.words, target.text) or (words_in_text(self.read.words, catalog_table.tab) if target.kind == T_TABLE else ())
                has_named = bool(rules) or sum(term.value for term in terms) >= SUPPORT
                proposal = at in proposals and not has_named
                if not has_named and not proposal and target.kind in (T_VALUE, T_LINE, T_BAND):
                    continue                          # a value no word reaches reads 0, as one the index never looked at does
                self.write(name, span if (has_named or proposal) else (), terms, rules, open_score, proposal)

    def _proposals(self, view: TableView, dots: np.ndarray, best: np.ndarray, table) -> dict[int, float]:
        """What the open channel puts forward: per word, its top targets reaching OPEN_MIN and with no named support. In the order of (rounded similarity, target), and only as a set after that."""
        out: dict[int, float] = {}
        if not dots.size:
            return out
        open_targets = np.asarray([target.kind != T_TABLE for target in view.targets], dtype=bool)
        for row, is_content in zip(dots, self.content):
            if not is_content:
                continue
            candidates = np.flatnonzero((row >= OPEN_MIN) & (best < SUPPORT) & open_targets)
            if not len(candidates):
                continue
            rounded = np.round(row[candidates], 3)
            line = np.sort(rounded)[-min(OPEN_TOP, len(rounded))]
            near = candidates[rounded >= line]
            for at in sorted(near.tolist(), key=lambda at: (-round(float(row[at]), 3), view.targets[at].kind, view.targets[at].text, table.column_rank.get(view.targets[at].position, -1)))[:OPEN_TOP]:
                out[at] = max(out.get(at, 0.0), float(row[at]))
        return out

    def stated(self) -> None:
        """Periods, numbers, comparisons, contained phrases, and a number a column holds, come from the words alone: read as the rules they are."""
        for name, score in sorted(self.rule_values.items()):
            slot = slot_of(name)
            if slot not in _STATED_SLOTS or name in self.book:
                continue
            rules = self.rules_for(name)
            body = name.split(":", 1)[1]
            text = body.split("=", 1)[1] if slot == SLOT_CONTAINS and "=" in body else ""
            self.write(name, words_in_text(self.read.words, text) if text else (), (), rules, 0.0, False)

    def section(self, table) -> None:
        """A table's section, as a key or a shown column, reads as an unlinked column does, not as a coordinate nobody scored: a band's words link to the band."""
        if any(line.section for line in table.lines):
            name = Col(table.id, SECTION).name
            for slot in (SLOT_KEY, SLOT_SHOWN):
                self.scores.setdefault(coordinate(slot, name), round(-self.weights.bias(slot), 4))

    def conditions(self, table_id: str) -> None:
        """How many conditions: the head's log probability and the words' count of what they can read."""
        head = self.read.closed[SLOT_CONDITIONS]
        for count, p in head.items():
            name = coordinate(SLOT_CONDITIONS, table_id, count)
            self.scores[name] = round(self.floor.get(name, 0.0) + self.weights.closed * max(float(np.log(max(p, 1e-6))), LOG_FLOOR), 4)

    def calc_left(self) -> None:
        row_sided = max(self.read.closed[SLOT_ROW_OP].get(op, 0.0) for op in (MINUS, OVER))
        if max(self.read.closed[SLOT_FORM].get(CALC, 0.0), row_sided) < HEAD_MIN or not len(self.read.words):
            return
        left = self.read.sides[:, CALC_SIDES.index(LEFT)]
        for name, evidence in sorted(self.book.items()):
            if evidence.span and slot_of(name) in LINK_SLOTS:
                side = float(left[list(evidence.span)].mean())
                if side >= LEFT_MIN:
                    self.scores[coordinate(SLOT_CALC_LEFT, name)] = round(self.weights.closed * side, 4)


def build_reading(read: QuestionRead, windex: WorkbookIndex, catalog: Catalog, decoder: Decoder, names: NameSet, weights: LinkWeights, *, mode: str = MODE_NAMED, signatures: ClassSignatures | None = None) -> NamedReading:
    """A question's whole reading over one workbook."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    builder = _Builder(read, windex, catalog, decoder, names, weights, mode)
    builder.scores.update({name: round(value, 4) for name, value in closed_reading(read, weights).items()})
    question_open = read.open.astype(np.float64) / 127.0
    for table in catalog.tables:
        view = windex.views.get(table.id)
        if view is None or not table.lines:
            continue
        n = named_matrix(read.coordinates, view.ids, view.values, weights.coordinates)
        if len(read.words):
            n = np.where(builder.content[:, None], n, 0.0)
        dots = question_open @ (view.open.astype(np.float64) / 127.0).T if len(read.words) and len(view.targets) else np.zeros((0, len(view.targets)))
        builder.table(table.id, view, n, dots)
        builder.section(table)
        builder.conditions(table.id)
    builder.stated()
    builder.calc_left()
    agreement = _class_agreement(read, windex, names, signatures)
    reading = Reading(builder.scores)
    return NamedReading(read, reading, builder.book, mode, agreement, windex, names, table_reach(reading))


def _class_agreement(read: QuestionRead, windex: WorkbookIndex, names: NameSet, signatures: ClassSignatures | None) -> dict[str, float]:
    """The superposition applied to the question's words, against each table's: how far they read as the same class of thing."""
    if signatures is None or not signatures.classes or not len(read.words):
        return {}
    highest = read.coordinates.max(axis=0)
    profile = {name: highest[names.at(name)] / SCALE for row in signatures.classes.values() for name in (item["property"] for item in row["signature"]) if names.at(name) is not None}
    asked = signatures.scores(profile)
    return {table_id: round(sum(asked[name] * view.classes.get(name, 0.0) for name in asked), 4) for table_id, view in windex.views.items()}


def unsupported_links(program: Program, named: NamedReading, catalog: Catalog) -> tuple[str, ...]:
    """The program's links with no named support: a proposal, or nothing at all. Any of them and the program is needs_asking."""
    out = []
    for name in coordinates(program, catalog.column_kind):
        if slot_of(name) not in LINK_SLOTS:
            continue
        evidence = named.book.get(name)
        if evidence is None or not evidence.supported:
            out.append(name)
    return tuple(out)


def needs_asking(program: Program, named: NamedReading, catalog: Catalog) -> bool:
    return named.mode == MODE_NAMED and bool(unsupported_links(program, named, catalog))


__all__ = ["Evidence", "LINK_SLOTS", "MODES", "MODE_NAMED", "MODE_NAMED_PLUS_OPEN", "NamedReading", "PROPOSAL_SCORE", "QuestionRead", "build_reading", "closed_reading", "needs_asking", "read_questions", "unsupported_links"]
