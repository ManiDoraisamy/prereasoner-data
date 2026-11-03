"""The whole path, one question at a time: a compiled workbook's index, the reader, the decoder, the verifier, the chooser, an answer or a question back. float32, encoder only.

No LLM reaches this path: tests.test_named checks what importing it loads.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from engine.named.align import Aligner, AlignWeights
from engine.named.backbone import TextBackbone
from engine.named.bundle import read_bundle
from engine.named.chooser import ASK, PRIOR, Chooser, Decision, answer_or_ask, score_candidates
from engine.named.index import ClassSignatures, IndexCache, SheetEncoder, WorkbookIndex, index_workbook
from engine.named.links import LinkWeights
from engine.named.model import NamedReader, ReaderConfig
from engine.named.names import NameSet
from engine.named.reader import MODE_NAMED, NamedReading, build_reading, read_questions
from engine.program.catalog import Catalog
from engine.program.decoder import POLICY, Decoder, Policy
from engine.program.output import read_as
from engine.program.scoring import DEFAULT_WEIGHTS, Weights
from engine.program.store import RequestTables
from engine.program.verifier import verify

DECODE_K = 50


@dataclass
class Prepared:
    """One workbook, ready: its catalog, decoder, request tables and index."""
    catalog: Catalog
    decoder: Decoder
    tables: RequestTables
    index: WorkbookIndex | None
    index_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the path says back, with what it took."""
    question: str
    kind: str
    rows: tuple[tuple, ...]
    read_as: str
    ask: str
    decision: Decision
    named: NamedReading
    seconds: dict = field(default_factory=dict)
    candidates: tuple = ()


class NamedEngine:
    """A bundle in memory. Load one at a time."""

    def __init__(self, backbone: TextBackbone, reader: NamedReader, names: NameSet, config: ReaderConfig, weights: LinkWeights, *, chooser: Chooser = PRIOR, signatures: ClassSignatures | None = None, slot_weights: Weights = DEFAULT_WEIGHTS, reader_id: str = "", policy: Policy = POLICY, cache: IndexCache | None = None, align: AlignWeights | None = None):
        self.backbone, self.reader, self.names, self.config, self.weights = backbone.eval(), reader.eval(), names, config, weights
        self.chooser, self.signatures, self.slot_weights, self.policy = chooser, signatures or ClassSignatures({}), slot_weights, policy
        self.encoder = SheetEncoder(self.backbone, self.reader, names, config, self.signatures, reader_id)
        self.cache = cache if cache is not None else IndexCache()
        self.aligner = Aligner(align, names, weights.coordinates) if align is not None else None

    def prepare(self, catalog: Catalog, tab_hashes=None) -> Prepared:
        """A workbook read into its index, tab by tab; an unchanged tab comes from the cache."""
        started = time.perf_counter()
        index = index_workbook(self.encoder, catalog, self.cache, tab_hashes)
        decoder = Decoder(catalog, policy=self.policy)
        decoder.warm()
        tables = RequestTables(catalog)
        for table in catalog.tables:
            tables.ensure(table.id)
        seconds = time.perf_counter() - started
        return Prepared(catalog, decoder, tables, index, seconds)

    def read(self, question: str, prepared: Prepared, mode: str = MODE_NAMED) -> NamedReading:
        read = read_questions(self.backbone, self.reader, self.config, [question])[0]
        return build_reading(read, prepared.index, prepared.catalog, prepared.decoder, self.names, self.weights, mode=mode, signatures=self.signatures)

    def ask(self, question: str, prepared: Prepared, *, mode: str = MODE_NAMED, k: int = DECODE_K) -> Outcome:
        """One question, answered or asked back. Timed by stage."""
        clock = [time.perf_counter()]
        named = self.read(question, prepared, mode)
        clock.append(time.perf_counter())
        decoded = prepared.decoder.decode(named.reading, k=k, weights=self.slot_weights)
        clock.append(time.perf_counter())
        verified = verify(decoded.programs, prepared.tables, named.expected, top=k)
        clock.append(time.perf_counter())
        candidates = score_candidates(verified, named, prepared.catalog, self.chooser, self.aligner)
        decision = answer_or_ask(candidates, self.chooser)
        clock.append(time.perf_counter())
        seconds = {stage: round(clock[at + 1] - clock[at], 4) for at, stage in enumerate(("read", "decode", "verify", "choose"))}
        seconds["total"] = round(clock[-1] - clock[0], 4)
        return outcome_of(question, decision, named, prepared.catalog, seconds, tuple(candidates))


def outcome_of(question: str, decision: Decision, named: NamedReading, catalog: Catalog, seconds: dict, candidates: tuple = ()) -> Outcome:
    best = decision.candidate
    if decision.kind == ASK:
        words = ""
        if best is not None and decision.other is not None:
            words = f"{read_as(best.verified.scored.program, catalog)} or {read_as(decision.other.verified.scored.program, catalog)}?"
        elif best is not None:
            words = f"Do you mean {read_as(best.verified.scored.program, catalog)}?"
        return Outcome(question, ASK, (), "", words or "Which figure or column do you mean?", decision, named, seconds, candidates)
    return Outcome(question, decision.kind, best.rows, read_as(best.verified.scored.program, catalog), "", decision, named, seconds, candidates)


def load_bundle(folder: str | Path, *, backbone: TextBackbone | None = None, chooser: Chooser | None = None, cache: IndexCache | None = None, policy: Policy = POLICY) -> NamedEngine:
    """An engine from a bundle's directory."""
    bundle = read_bundle(folder, backbone=backbone)
    return NamedEngine(bundle.backbone, bundle.reader, bundle.names, bundle.config, bundle.weights, chooser=chooser or bundle.chooser, signatures=bundle.signatures, slot_weights=bundle.slot_weights, reader_id=bundle.reader_id, policy=policy, cache=cache, align=bundle.align)


__all__ = ["DECODE_K", "NamedEngine", "Outcome", "Prepared", "load_bundle"]
