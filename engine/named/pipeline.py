"""The whole path, one question at a time: a compiled workbook's index, the reader, the decoder (and, with a bundle's readers, today's own-data readers proposing beside it: engine/named/proposals.py), the verifier, the chooser, an answer or a question back. float32, encoder only.

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
from engine.named.proposals import ReaderProposer, SheetView, program_key, sheet_view
from engine.named.reader import MODE_NAMED, NamedReading, build_reading, read_questions
from engine.program.catalog import Catalog
from engine.program.decoder import POLICY, Decoder, Policy
from engine.program.output import read_as
from engine.program.scoring import DEFAULT_WEIGHTS, Weights, score_program
from engine.program.store import RequestTables
from engine.program.verifier import Verified, verify

DECODE_K = 50


@dataclass
class Prepared:
    """One workbook, ready: its catalog, decoder, request tables and index."""
    catalog: Catalog
    decoder: Decoder
    tables: RequestTables
    index: WorkbookIndex | None
    index_seconds: float = 0.0
    view: SheetView | None = None


@dataclass(frozen=True, slots=True)
class Pool:
    """A question's verified candidates: the decoder's first, in its order, then the programs only the readers proposed, in theirs. reader_scores: the readers' probability by program_key; decoded: how many from the front the decoder listed."""
    verified: tuple[Verified, ...]
    reader_scores: dict
    decoded: int


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

    def __init__(self, backbone: TextBackbone, reader: NamedReader, names: NameSet, config: ReaderConfig, weights: LinkWeights, *, chooser: Chooser = PRIOR, signatures: ClassSignatures | None = None, slot_weights: Weights = DEFAULT_WEIGHTS, reader_id: str = "", policy: Policy = POLICY, cache: IndexCache | None = None, align: AlignWeights | None = None, proposer: ReaderProposer | None = None):
        self.backbone, self.reader, self.names, self.config, self.weights = backbone.eval(), reader.eval(), names, config, weights
        self.chooser, self.signatures, self.slot_weights, self.policy = chooser, signatures or ClassSignatures({}), slot_weights, policy
        self.encoder = SheetEncoder(self.backbone, self.reader, names, config, self.signatures, reader_id)
        self.cache = cache if cache is not None else IndexCache()
        self.aligner = Aligner(align, names, weights.coordinates) if align is not None else None
        self.proposer = proposer

    def prepare(self, catalog: Catalog, tab_hashes=None) -> Prepared:
        """A workbook read into its index, tab by tab; an unchanged tab comes from the cache."""
        started = time.perf_counter()
        index = index_workbook(self.encoder, catalog, self.cache, tab_hashes)
        decoder = Decoder(catalog, policy=self.policy)
        decoder.warm()
        tables = RequestTables(catalog)
        for table in catalog.tables:
            tables.ensure(table.id)
        view = sheet_view(catalog) if self.proposer is not None else None
        seconds = time.perf_counter() - started
        return Prepared(catalog, decoder, tables, index, seconds, view)

    def read(self, question: str, prepared: Prepared, mode: str = MODE_NAMED) -> NamedReading:
        read = read_questions(self.backbone, self.reader, self.config, [question])[0]
        return build_reading(read, prepared.index, prepared.catalog, prepared.decoder, self.names, self.weights, mode=mode, signatures=self.signatures)

    def propose(self, question: str, prepared: Prepared) -> tuple:
        """The readers' proposals for a question, none without readers."""
        if self.proposer is None or prepared.view is None:
            return ()
        return self.proposer.propose(question, prepared.view)

    def pool(self, question: str, prepared: Prepared, named: NamedReading, *, k: int = DECODE_K, decoded=None, proposals=None) -> Pool:
        """The decoder's top k verified, then each program only the readers proposed, scored on the same reading and verified on its own budget. One pool for serving, collection and evaluation."""
        decoded = decoded if decoded is not None else prepared.decoder.decode(named.reading, k=k, weights=self.slot_weights)
        verified = tuple(verify(decoded.programs, prepared.tables, named.expected, top=k))
        proposals = self.propose(question, prepared) if proposals is None else proposals
        if not proposals:
            return Pool(verified, {}, len(verified))
        listed = {program_key(item.scored.program) for item in verified}
        reader_scores: dict = {}
        extra = []
        for proposal in proposals:
            key = program_key(proposal.program)
            reader_scores[key] = max(reader_scores.get(key, 0.0), proposal.score)
            if key not in listed:
                listed.add(key)
                extra.append(score_program(proposal.program, named.reading, prepared.catalog, self.slot_weights))
        more = tuple(verify(extra, prepared.tables, named.expected, top=len(extra))) if extra else ()
        return Pool(verified + more, reader_scores, len(verified))

    def score(self, pool: Pool, named: NamedReading, prepared: Prepared, chooser: Chooser | None = None, *, terms: bool = False) -> list:
        """A pool's candidates scored by a chooser (the engine's by default), best first."""
        return score_candidates(pool.verified, named, prepared.catalog, chooser or self.chooser, self.aligner, terms=terms, reader_scores=pool.reader_scores, decoded=pool.decoded)

    def ask(self, question: str, prepared: Prepared, *, mode: str = MODE_NAMED, k: int = DECODE_K) -> Outcome:
        """One question, answered or asked back. Timed by stage."""
        clock = [time.perf_counter()]
        named = self.read(question, prepared, mode)
        clock.append(time.perf_counter())
        decoded = prepared.decoder.decode(named.reading, k=k, weights=self.slot_weights)
        clock.append(time.perf_counter())
        proposals = self.propose(question, prepared)
        clock.append(time.perf_counter())
        pool = self.pool(question, prepared, named, k=k, decoded=decoded, proposals=proposals)
        clock.append(time.perf_counter())
        candidates = self.score(pool, named, prepared)
        decision = answer_or_ask(candidates, self.chooser)
        clock.append(time.perf_counter())
        seconds = {stage: round(clock[at + 1] - clock[at], 4) for at, stage in enumerate(("read", "decode", "propose", "verify", "choose"))}
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


def load_bundle(folder: str | Path, *, backbone: TextBackbone | None = None, chooser: Chooser | None = None, cache: IndexCache | None = None, policy: Policy = POLICY, readers: str | Path | None = None) -> NamedEngine:
    """An engine from a bundle's directory. readers: a folder of today's readers (stage1/, stage2_*/) to propose beside the decoder; by default the folder the bundle names, if it names one."""
    bundle = read_bundle(folder, backbone=backbone)
    readers = readers if readers is not None else bundle.readers
    proposer = ReaderProposer(readers) if readers else None
    return NamedEngine(bundle.backbone, bundle.reader, bundle.names, bundle.config, bundle.weights, chooser=chooser or bundle.chooser, signatures=bundle.signatures, slot_weights=bundle.slot_weights, reader_id=bundle.reader_id, policy=policy, cache=cache, align=bundle.align, proposer=proposer)


__all__ = ["DECODE_K", "NamedEngine", "Outcome", "Pool", "Prepared", "load_bundle"]
