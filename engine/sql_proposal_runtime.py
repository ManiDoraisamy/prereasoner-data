"""Shared runtime adapter from schemas and encoder vectors to proposal signals."""
from __future__ import annotations

import os
from typing import Any, Sequence

import numpy as np

from engine.artifact_provenance import sha256_file, sha256_tree
from engine.sql_proposal import SQLProposalModel, semantic_signals_from_schema
from engine.sql_schema import SchemaGraph


def _file_sha256(path: str) -> str:
    return sha256_file(path)


def _tree_sha256(path: str) -> str:
    """Byte-identical to spider/probe/train_ast_proposer._tree_sha256 so the recorded provenance is comparable."""
    return sha256_tree(path)


def verify_proposer_adapter(model: "SQLProposalModel", encoder=None) -> None:
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
    if not want:
        raise RuntimeError("SQL proposer artifact has no encoder adapter provenance")
    have = getattr(encoder, "encoder_adapter_sha256", None)
    adapter = getattr(encoder, "encoder_data_dir", None)
    if have is None:
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
        verify_proposer_adapter(model, encoder)
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
