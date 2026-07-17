"""Shared runtime adapter from schemas and encoder vectors to proposal signals."""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from engine.sql_proposal import SQLProposalModel, semantic_signals_from_schema
from engine.sql_schema import SchemaGraph


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
