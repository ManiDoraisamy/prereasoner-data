"""Embedded-fixture selection and tab materialization.

Given the request's source tables, decide which registry datasets are eligible (per-dataset
required/optional/disqualifying evidence over value-typing + a key-overlap confidence check)
and produce an explicit key edge + a planner tab per selected dataset. The tab is built with
engine.tables.table_from_rows — the SAME builder engine.master.relevant_tables uses — so it
enters the canonical engine.relations.discover_fks -> typed-AST path. This is a generalization
of the reference-table mechanism, not a second registration subsystem.

Database-backed definitions are loaded only by SnapshotStore and remain disabled here.
No network, no routing, no model. `select_datasets` must remain independent of
engine.routing.route (compose ownership) — a detected pattern never triggers compose.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from engine.tables import table_from_rows
from engine.enrichment.registry import (
    Activation, EmbeddedStorage, LookupCardinality, REGISTRY, DatasetDefinition, SnapshotPin,
)
from engine.enrichment.value_types import detect_column


@dataclass(frozen=True)
class ExplicitKeyEdge:
    from_table: str
    from_columns: tuple[str, ...]
    to_table: str
    to_columns: tuple[str, ...]
    cardinality: LookupCardinality = LookupCardinality.ONE

    def __post_init__(self) -> None:
        if not self.from_columns or len(self.from_columns) != len(self.to_columns):
            raise ValueError("explicit key edges require equally sized column tuples")

    @property
    def from_col(self) -> str:
        if len(self.from_columns) != 1:
            raise ValueError("composite edge has no single from_col")
        return self.from_columns[0]

    @property
    def to_col(self) -> str:
        if len(self.to_columns) != 1:
            raise ValueError("composite edge has no single to_col")
        return self.to_columns[0]

    def as_foreign_key(self, confidence: float) -> dict:
        edge = {
            "from_table": self.from_table, "from_cols": self.from_columns,
            "to_table": self.to_table, "to_cols": self.to_columns,
            "conf": confidence, "inclusion": confidence,
            "cardinality": self.cardinality.value,
        }
        if len(self.from_columns) == 1:
            edge.update(from_col=self.from_columns[0], to_col=self.to_columns[0])
        return edge


@dataclass(frozen=True)
class SelectedDataset:
    dataset: DatasetDefinition
    source_table: str
    source_column: str
    explicit_edge: ExplicitKeyEdge
    key_confidence: float        # lower of exact distinct-value overlap and exact row coverage
    distinct_key_confidence: float
    row_coverage: float
    snapshot: SnapshotPin

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.release_id


# Selection policy (conservative; abstaining is always safe). These two constants ARE the
# policy: raise them to abstain more, lower them to enrich more.
MIN_EVIDENCE_CELLS = 2
# Keep selection consistent with relations.discover_fks and master.relevant_tables. A lower
# threshold would claim a reference is joinable even though the canonical FK path rejects it.
DEFAULT_MIN_KEY_CONFIDENCE = 0.9


def _column_cells(table: dict, ci: int) -> list[str]:
    out = []
    for r in table.get("rows", []) or []:
        if ci < len(r) and r[ci] is not None and str(r[ci]).strip() != "":
            out.append(str(r[ci]).strip())
    return out


def select_datasets(source_tables, registry=None, request_evidence=(),
                    min_key_confidence: float = DEFAULT_MIN_KEY_CONFIDENCE) -> list[SelectedDataset]:
    """Eligible (dataset, source_column) selections. One selection per (dataset, source_table):
    among the columns whose evidence satisfies the dataset predicate AND whose values actually
    overlap the dataset key (key_confidence) with enough evidence, pick the HIGHEST-confidence
    one (ties broken by leftmost column) — otherwise the dataset ABSTAINS on that table."""
    if (isinstance(min_key_confidence, bool) or not isinstance(min_key_confidence, (int, float))
            or not isfinite(float(min_key_confidence)) or not 0.0 <= min_key_confidence <= 1.0):
        raise ValueError("min_key_confidence must be a finite real number in [0,1]")
    reg = registry if registry is not None else REGISTRY
    if isinstance(request_evidence, str):
        raise ValueError("request_evidence must be a collection of tags, not a string")
    request_evidence = frozenset(request_evidence)
    if any(not isinstance(tag, str) or not tag.strip() for tag in request_evidence):
        raise ValueError("request_evidence must contain non-empty strings")
    selected: list[SelectedDataset] = []
    for table in source_tables or []:
        cols = table.get("columns") or []
        for dataset_name in sorted(reg):
            ds = reg[dataset_name]
            if ds.activation == Activation.DISABLED or not isinstance(ds.storage, EmbeddedStorage):
                continue
            if ds.commercial_use != "approved" or ds.privacy_class != "public_reference":
                continue
            if ds.temporal.kind.value != "snapshot":       # temporal semantics are deliberately outside M0
                continue
            if ds.cardinality != LookupCardinality.ONE or len(ds.lookup_key) != 1:
                continue
            key = ds.lookup_key[0]
            # SQL text equality is case-sensitive in both serving engines. Do not normalize here:
            # a lower-cased source value must abstain until the AST has an explicit normalization node.
            key_index = ds.columns.index(key)
            ds_key_vals = {
                str(row[key_index]).strip() for row in ds.storage.rows
                if row[key_index] is not None
            }
            candidates: list[tuple[int, str, float, float, float]] = []
            for ci, col in enumerate(cols):
                cells = _column_cells(table, ci)
                if len(cells) < MIN_EVIDENCE_CELLS:             # too little evidence to conclude a type -> abstain
                    continue
                if not ds.eligibility.eligible(detect_column(cells) | request_evidence):
                    continue
                child = set(cells)
                distinct_conf = len(child & ds_key_vals) / len(child)
                row_coverage = sum(cell in ds_key_vals for cell in cells) / len(cells)
                conf = min(distinct_conf, row_coverage)
                if conf < min_key_confidence:                   # typed as the right thing but not joinable -> abstain
                    continue
                candidates.append((ci, col, conf, distinct_conf, row_coverage))
            if candidates:
                ci, col, conf, distinct_conf, row_coverage = max(
                    candidates, key=lambda candidate: (candidate[2], -candidate[0])
                )
                selected.append(SelectedDataset(
                    dataset=ds, source_table=table.get("name"), source_column=col,
                    explicit_edge=ExplicitKeyEdge(
                        table.get("name"), (col,), ds.name, (key,), ds.cardinality
                    ),
                    key_confidence=conf, distinct_key_confidence=distinct_conf,
                    row_coverage=row_coverage,
                    snapshot=SnapshotPin("embedded", ds.embedded_snapshot_id, 1,
                                         ds.definition_id)))
    return selected


def to_tabs(selected: list[SelectedDataset], existing_names=()) -> list[dict]:
    """Materialize each distinct selected dataset as a planner tab (table_from_rows), ready to
    extend the request's table set exactly like engine.master.relevant_tables output."""
    reserved = set(existing_names)
    seen: set[str] = set()
    tabs: list[dict] = []
    for s in selected:
        if s.dataset.name in seen:
            continue
        if s.dataset.name in reserved:
            raise ValueError(f'enrichment table name collides with request table: {s.dataset.name}')
        seen.add(s.dataset.name)
        if not isinstance(s.dataset.storage, EmbeddedStorage):
            raise ValueError(f"{s.dataset.name}: database rows must be loaded by SnapshotStore")
        rows = [list(r) for r in s.dataset.storage.rows]
        tabs.append(table_from_rows(s.dataset.name, list(s.dataset.columns), rows))
    return tabs
