"""Shared runtime adapter from schemas and encoder vectors to proposal signals."""
from __future__ import annotations

import hashlib
import os
from typing import Any, Sequence

import numpy as np

from engine.sql_proposal import SQLProposalModel, semantic_signals_from_schema
from engine.sql_schema import SchemaGraph


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_sha256(path: str) -> str:
    """Byte-identical to spider/probe/train_ast_proposer._tree_sha256 so the recorded provenance is comparable."""
    if not os.path.exists(path):
        return "missing"
    if os.path.isfile(path):
        return _file_sha256(path)
    digest = hashlib.sha256()
    for directory, directories, names in os.walk(path):
        directories.sort()
        for name in sorted(names):
            item = os.path.join(directory, name)
            relative = os.path.relpath(item, path).replace("\\", "/")
            digest.update(relative.encode("utf-8"))
            digest.update(_file_sha256(item).encode("ascii"))
    return digest.hexdigest()


def verify_proposer_adapter(model: "SQLProposalModel") -> None:
    """Fail-fast if the proposer was trained on a DIFFERENT encoder adapter than the one now loaded.

    The proposal heads score current encoder embeddings against weights fit in the metric space of the
    adapter they were trained on. When the adapter changes (e.g. a new fine-tune), the vector DIMENSIONS
    still match, so mismatched weights degrade results SILENTLY — the exact failure the property retrain
    introduced (proposer trained on adapter 41ccf973, current is different -> ~5pt Spider loss). Serving
    doesn't load the proposer, but the eval/training harness does; this makes the mismatch loud instead.
    Set PREREASONER_SKIP_ADAPTER_CHECK=1 to bypass (not recommended)."""
    if os.environ.get("PREREASONER_SKIP_ADAPTER_CHECK") == "1":
        return
    want = (getattr(model, "metadata", None) or {}).get("adapter_sha256")
    if not want:                                             # proposer predates provenance — nothing to check against
        return
    from engine.config import DATA_DIR
    adapter = os.path.join(str(DATA_DIR), "qwen_lora")
    have = _tree_sha256(adapter)
    if have != want:
        raise RuntimeError(
            f"SQL proposer/encoder-adapter MISMATCH: this proposer was trained against adapter {want[:12]}… "
            f"but the loaded adapter ({adapter}) is {have[:12]}…. The embedding dimensions match, so scoring "
            f"would degrade SILENTLY. Retrain with spider/probe/train_ast_proposer.py against this adapter, "
            f"or run pure-AST (no proposer). Bypass with PREREASONER_SKIP_ADAPTER_CHECK=1."
        )


def schema_descriptors(schema: SchemaGraph) -> tuple[dict[str, Any], ...]:
    """Return the canonical schema records consumed by the proposal model."""
    return tuple({
        "table": column.ref.table,
        "name": column.ref.name,
        "affinity": (
            "INTEGER" if column.ref.type.value == "integer"
            else "REAL" if column.ref.type.value == "real"
            else "DATE" if column.ref.type.value == "date"
            else "TEXT"
        ),
        "is_date": column.ref.type.value == "date",
    } for column in schema.columns)


class ProposalSignalProvider:
    """Build semantic signals while caching schema descriptor embeddings."""

    DESCRIPTOR_CACHE_LIMIT = 4096

    def __init__(self, model: SQLProposalModel, encoder):
        verify_proposer_adapter(model)                       # refuse a proposer trained on a different encoder adapter
        self.model = model
        self.encoder = encoder
        self._descriptor_vectors: dict[str, np.ndarray] = {}

    def signals(
        self,
        question: str,
        schema: SchemaGraph,
        question_vector: Sequence[float] | None = None,
    ):
        return self.signals_from_descriptors(
            question, schema_descriptors(schema), question_vector
        )

    def signals_from_descriptors(
        self,
        question: str,
        descriptors: Sequence[dict[str, Any]],
        question_vector: Sequence[float] | None = None,
    ):
        """Build signals from serving-style schema descriptor dictionaries."""

        def encode(texts):
            _, *names = texts
            missing = [
                name for name in dict.fromkeys(names)
                if name not in self._descriptor_vectors
            ]
            resolved = {
                name: self._descriptor_vectors[name]
                for name in dict.fromkeys(names)
                if name in self._descriptor_vectors
            }
            prefix = [] if question_vector is not None else [question]
            if prefix or missing:
                vectors = self.encoder._encode(prefix + missing)
                offset = len(prefix)
                resolved.update(zip(missing, vectors[offset:]))
                for name in missing:
                    self._descriptor_vectors[name] = resolved[name]
                    while len(self._descriptor_vectors) > self.DESCRIPTOR_CACHE_LIMIT:
                        self._descriptor_vectors.pop(next(iter(self._descriptor_vectors)))
                current_question = (
                    np.asarray(question_vector, dtype=np.float32)
                    if question_vector is not None else vectors[0]
                )
            else:
                current_question = np.asarray(question_vector, dtype=np.float32)
            return np.vstack([
                current_question,
                *(resolved[name] for name in names),
            ])

        return semantic_signals_from_schema(
            self.model, question, descriptors, encode
        )
