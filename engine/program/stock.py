"""The stock reading: a question read onto the decoder's named coordinates by nothing trained for it.

Two parts, added on each coordinate:
  - the engine's word reader (engine/program/words.py read_question): digits, periods and the sheet's own words;
  - stock bge-m3 (engine/named/backbone.py, the base model's own weights): each workbook target (engine/named/units.py
    targets_of: the table, its columns, the values of its text columns, its printed lines' labels, its bands) is scored by
    its best cosine to the question's one- to three-word phrases and to the whole question. Of a table's values, lines
    and bands only the VALUE_TOP most similar are kept.
Every closed head offers every class at its uniform share, and each head's classes stand on the reading at 0.0, so the
closed questions (engine/program/closed.py) can read them; the word roles say nothing.

This reading is a shortlist: it decides which options the reading record offers the Decisions API, and keeps its
scores on the slots the API is not asked. The vectors of texts read before are kept (CACHE_MOST, float16), so a
workbook's targets are encoded on its first question only.
"""
from __future__ import annotations

import threading

import numpy as np
import torch

from engine.named.backbone import TextBackbone, load_backbone, question_words
from engine.named.names import CALC_SIDES, CLOSED_HEADS, NONE, WORD_ROLE_CLASSES
from engine.named.reader import MODE_NAMED_PLUS_OPEN, VALUE_TOP, NamedReading, QuestionRead
from engine.named.units import T_BAND, T_LINE, T_TABLE, T_VALUE, Target, UnitsConfig, targets_of
from engine.program.catalog import Catalog
from engine.program.decoder import Decoder, table_reach
from engine.program.names import SIGNED, SLOT_ANSWER_TYPE, SLOT_CONDITIONS, SLOT_SIZE, coarse_type, coordinate
from engine.program.scoring import Reading
from engine.program.words import read_question

STOCK_BACKBONE = "bge-m3"
PHRASE_WORDS = 3           # a question's phrases: every run of one to this many of its words
CACHE_MOST = 400_000       # vectors kept by text; past this the cache is cleared
TEXT_TOKENS = 32           # tokens of a text or phrase the backbone reads
ENCODE_BATCH = 64
TABLE_COLUMNS_NAMED = 8    # a table with no tab name is read by its first columns' names
SCORE_DIGITS = 4

_READER = None
_READER_LOCK = threading.Lock()


def workbook_targets(catalog: Catalog) -> dict[str, tuple[list[Target], list[tuple[str, ...]]]]:
    """Each table's targets and the coordinates each fills, read once per workbook."""
    out = {}
    for table in catalog.tables:
        targets = targets_of(table, UnitsConfig())
        out[table.id] = (targets, [target.coordinates() for target in targets])
    return out


class StockReader:
    """Stock bge-m3 similarity beside the word reader. One per process (stock_reader); questions served at once share
    it, so its cache is read and written under one lock."""

    def __init__(self, backbone: TextBackbone):
        self.backbone = backbone
        self.cache: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()

    def encode(self, texts: list[str]) -> np.ndarray:
        """Unit vectors of texts, [len(texts), dim] float32, read through the cache (stored as float16)."""
        with self._lock:
            missing = [text for text in dict.fromkeys(texts) if text not in self.cache]
            if missing:
                if len(self.cache) + len(missing) > CACHE_MOST:
                    self.cache.clear()
                    missing = list(dict.fromkeys(texts))
                with torch.inference_mode():
                    distinct, vectors = self.backbone.encode_texts(missing, max_tokens=TEXT_TOKENS, batch=ENCODE_BATCH)
                vectors = torch.nn.functional.normalize(vectors, dim=-1).numpy().astype(np.float16)
                self.cache.update(zip(distinct, vectors))
            if not texts:
                return np.zeros((0, self.backbone.dim), np.float32)
            return np.stack([self.cache[text] for text in texts]).astype(np.float32)

    def read(self, question: str, catalog: Catalog, decoder: Decoder, targets: dict[str, tuple[list[Target], list[tuple[str, ...]]]]) -> NamedReading:
        """The question's stock reading: the word reader's scores, each target's similarity added on the coordinates its
        link fills, and the closed heads at their uniform shares."""
        floor = read_question(question, catalog, decoder).scores
        scores = dict(floor)
        words = question_words(question)
        phrases = sorted({" ".join(words[at:at + size]) for size in range(1, PHRASE_WORDS + 1) for at in range(len(words) - size + 1)} | {question})
        asked = self.encode(phrases)
        for table in catalog.tables:
            held = targets.get(table.id)
            if held is None or not table.lines or not held[0]:
                continue
            table_targets, filled = held
            texts = [target.text if target.kind != T_TABLE else (table.tab or " ".join(c.name for c in table.columns[:TABLE_COLUMNS_NAMED])) for target in table_targets]
            similarity = (asked @ self.encode(texts).T).max(axis=0)
            value_rows = [at for at, target in enumerate(table_targets) if target.kind in (T_VALUE, T_LINE, T_BAND)]
            kept = set(sorted(value_rows, key=lambda at: (-round(float(similarity[at]), SCORE_DIGITS), texts[at], at))[:VALUE_TOP])
            for at, (target, names) in enumerate(zip(table_targets, filled)):
                if target.kind in (T_VALUE, T_LINE, T_BAND) and at not in kept:
                    continue
                for name in names:
                    scores[name] = round(float(similarity[at]) + floor.get(name, 0.0), SCORE_DIGITS)
        closed = {}
        for head, classes in CLOSED_HEADS.items():
            closed[head] = {c: round(1.0 / len(classes), SCORE_DIGITS) for c in classes}
            if head == SLOT_CONDITIONS:
                continue
            silent = NONE if NONE in classes else SIGNED if head == SLOT_SIZE else None
            for c in classes:
                if c != silent:
                    scores.setdefault(coordinate(head, coarse_type(c) if head == SLOT_ANSWER_TYPE else c), 0.0)
        roles = np.zeros((len(words), len(WORD_ROLE_CLASSES)))
        roles[:, WORD_ROLE_CLASSES.index(NONE)] = 1.0
        read = QuestionRead(question, words, np.zeros((len(words), 0), np.int16), roles, np.zeros((len(words), len(CALC_SIDES))), closed, np.zeros((len(words), 0), np.int8))
        reading = Reading(scores)
        return NamedReading(read, reading, {}, MODE_NAMED_PLUS_OPEN, {}, None, None, table_reach(reading))


def stock_reader() -> StockReader:
    """The process's one stock reader, loaded on first use (a model build touches process-wide torch state: one at a time)."""
    global _READER
    with _READER_LOCK:
        if _READER is None:
            _READER = StockReader(load_backbone(STOCK_BACKBONE))
        return _READER


__all__ = ["STOCK_BACKBONE", "StockReader", "stock_reader", "workbook_targets"]
