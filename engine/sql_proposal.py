"""Deterministic multi-task proposal heads for typed SQL AST search.

The frozen proposer consumes embeddings supplied by the shared encoder.  It does
not generate SQL tokens: one linear head predicts counted sketch features, and
pairwise heads score tables and role-specific columns.  Inference depends only
on NumPy and stable lexical features.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import re
from typing import Any, Callable, Mapping, Sequence
import zlib

import numpy as np


PROPOSAL_MODEL_VERSION = 1
PAIR_EXTRA_FEATURES = (
    "cosine",
    "name_overlap",
    "name_mentioned",
    "type_numeric",
    "type_text",
    "type_time",
    "is_identifier",
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _normalized(vector: Sequence[float]) -> np.ndarray:
    value = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(value))
    return value / max(norm, 1e-12)


def _tokens(value: str) -> tuple[str, ...]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value))
    return tuple(token.lower() for token in _TOKEN_RE.findall(spaced))


def pair_features(
    question: str,
    question_vector: Sequence[float],
    candidate_name: str,
    candidate_vector: Sequence[float],
    candidate_type: str = "",
) -> np.ndarray:
    """Build the shared interaction and lexical vector used by pairwise heads."""
    question_embedding = _normalized(question_vector)
    candidate_embedding = _normalized(candidate_vector)
    if question_embedding.shape != candidate_embedding.shape:
        raise ValueError("question and candidate embeddings must have the same shape")

    extras = pair_extra_features(
        question,
        question_embedding,
        candidate_name,
        candidate_embedding,
        candidate_type,
    )
    return np.concatenate((question_embedding * candidate_embedding, extras))


def pair_extra_features(
    question: str,
    question_vector: Sequence[float],
    candidate_name: str,
    candidate_vector: Sequence[float],
    candidate_type: str = "",
) -> np.ndarray:
    """Return the stable lexical and cosine tail of a pairwise feature vector."""
    question_embedding = _normalized(question_vector)
    candidate_embedding = _normalized(candidate_vector)
    if question_embedding.shape != candidate_embedding.shape:
        raise ValueError("question and candidate embeddings must have the same shape")

    question_tokens = set(_tokens(question))
    name_tokens = set(_tokens(candidate_name))
    overlap = len(question_tokens & name_tokens) / max(len(name_tokens), 1)
    mentioned = float(bool(name_tokens) and name_tokens <= question_tokens)
    lowered_type = candidate_type.lower()
    lowered_name = candidate_name.lower()
    return np.asarray((
        float(question_embedding @ candidate_embedding),
        overlap,
        mentioned,
        float(lowered_type in {"number", "integer", "real", "numeric"}),
        float(lowered_type in {"text", "string"}),
        float(lowered_type in {"date", "time", "datetime", "year"}),
        float(bool(re.search(r"(^|[_\s])(id|key|identifier)($|[_\s])", lowered_name))),
    ), dtype=np.float32)


def _sigmoid(value: np.ndarray) -> np.ndarray:
    clipped = np.clip(value, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _array(value: Any, dimensions: int, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float32)
    if array.ndim != dimensions or not np.isfinite(array).all():
        raise ValueError(f"invalid proposer array {name!r}")
    return array


def _encode_array(value: Any) -> dict[str, Any]:
    array = np.ascontiguousarray(np.asarray(value, dtype="<f4"))
    compressed = zlib.compress(array.tobytes(), level=9)
    return {
        "encoding": "zlib+base64",
        "dtype": "float32-le",
        "shape": list(array.shape),
        "data": base64.b64encode(compressed).decode("ascii"),
    }


def _decode_array(value: Any, dimensions: int, name: str) -> np.ndarray:
    if not isinstance(value, Mapping) or value.get("encoding") != "zlib+base64":
        raise ValueError(f"invalid encoded proposer array {name!r}")
    if value.get("dtype") != "float32-le":
        raise ValueError(f"unsupported proposer array dtype for {name!r}")
    shape = tuple(int(item) for item in value.get("shape", ()))
    if len(shape) != dimensions or any(item < 0 for item in shape):
        raise ValueError(f"invalid proposer array shape for {name!r}")
    try:
        raw = zlib.decompress(base64.b64decode(str(value.get("data", "")), validate=True))
        array = np.frombuffer(raw, dtype="<f4").reshape(shape).copy()
    except (ValueError, TypeError, zlib.error) as exc:
        raise ValueError(f"invalid proposer array payload for {name!r}") from exc
    return _array(array, dimensions, name)


@dataclass(frozen=True)
class SQLProposalModel:
    """Frozen JSON-serializable SQL sketch and schema-link proposal model."""

    sketch_names: tuple[str, ...]
    role_names: tuple[str, ...]
    sketch_presence_weight: np.ndarray
    sketch_presence_bias: np.ndarray
    sketch_count_weight: np.ndarray
    sketch_count_bias: np.ndarray
    sketch_profiles: tuple[Mapping[str, int], ...]
    sketch_profile_weight: np.ndarray
    sketch_profile_bias: np.ndarray
    table_weight: np.ndarray
    table_bias: float
    role_weight: np.ndarray
    role_bias: np.ndarray
    sketch_thresholds: np.ndarray
    metadata: Mapping[str, Any]
    version: int = PROPOSAL_MODEL_VERSION
    source_sha256: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.version != PROPOSAL_MODEL_VERSION:
            raise ValueError(
                f"unsupported SQL proposer version {self.version}; "
                f"expected {PROPOSAL_MODEL_VERSION}"
            )
        if not self.sketch_names or len(set(self.sketch_names)) != len(self.sketch_names):
            raise ValueError("SQL proposer sketch names must be nonempty and unique")
        if not self.role_names or len(set(self.role_names)) != len(self.role_names):
            raise ValueError("SQL proposer role names must be nonempty and unique")

        feature_count = len(self.sketch_names)
        hidden_size = self.hidden_size
        count_classes = self.count_classes
        profile_count = len(self.sketch_profiles)
        if not count_classes:
            raise ValueError("SQL proposer must define at least one positive count class")
        if not profile_count:
            raise ValueError("SQL proposer must define at least one exact sketch profile")
        known_features = set(self.sketch_names)
        for profile in self.sketch_profiles:
            if not profile or not set(profile) <= known_features:
                raise ValueError("SQL proposer profile contains unknown or no features")
            if any(not 1 <= int(value) <= count_classes for value in profile.values()):
                raise ValueError("SQL proposer profile count is outside the learned range")
        pair_size = hidden_size + len(PAIR_EXTRA_FEATURES)
        expected = {
            "sketch_presence_weight": (feature_count, hidden_size),
            "sketch_presence_bias": (feature_count,),
            "sketch_count_weight": (feature_count, count_classes, hidden_size),
            "sketch_count_bias": (feature_count, count_classes),
            "sketch_profile_weight": (profile_count, hidden_size),
            "sketch_profile_bias": (profile_count,),
            "table_weight": (pair_size,),
            "role_weight": (len(self.role_names), pair_size),
            "role_bias": (len(self.role_names),),
            "sketch_thresholds": (feature_count,),
        }
        for name, shape in expected.items():
            value = np.asarray(getattr(self, name))
            if value.shape != shape or not np.isfinite(value).all():
                raise ValueError(
                    f"SQL proposer {name} has shape {value.shape}, expected {shape}"
                )
        if not math.isfinite(float(self.table_bias)):
            raise ValueError("SQL proposer table bias must be finite")
        if np.any(self.sketch_thresholds <= 0) or np.any(self.sketch_thresholds >= 1):
            raise ValueError("SQL proposer thresholds must lie strictly between zero and one")

    @property
    def hidden_size(self) -> int:
        weight = np.asarray(self.sketch_presence_weight)
        return int(weight.shape[1]) if weight.ndim == 2 else 0

    @property
    def count_classes(self) -> int:
        weight = np.asarray(self.sketch_count_weight)
        return int(weight.shape[1]) if weight.ndim == 3 else 0

    def predict_sketch(self, question_vector: Sequence[float]) -> dict[str, int]:
        vector = _normalized(question_vector)
        if len(vector) != self.hidden_size:
            raise ValueError(
                f"proposer expected embedding size {self.hidden_size}, got {len(vector)}"
            )
        presence = _sigmoid(self.sketch_presence_weight @ vector + self.sketch_presence_bias)
        count_logits = np.einsum("fch,h->fc", self.sketch_count_weight, vector)
        count_logits = count_logits + self.sketch_count_bias
        result = {}
        for index, name in enumerate(self.sketch_names):
            if presence[index] < self.sketch_thresholds[index]:
                continue
            result[name] = int(np.argmax(count_logits[index])) + 1
        return result

    def propose_sketches(
        self,
        question_vector: Sequence[float],
        limit: int = 16,
    ) -> tuple[dict[str, int], ...]:
        """Return ranked exact profiles, using the compositional sketch if space remains."""
        if limit < 1:
            raise ValueError("sketch proposal limit must be positive")
        vector = _normalized(question_vector)
        if len(vector) != self.hidden_size:
            raise ValueError(
                f"proposer expected embedding size {self.hidden_size}, got {len(vector)}"
            )
        scores = self.sketch_profile_weight @ vector + self.sketch_profile_bias
        order = np.lexsort((np.arange(len(scores)), -scores))
        proposals: list[dict[str, int]] = []
        seen = set()
        for index in order:
            profile = {str(name): int(value) for name, value in self.sketch_profiles[index].items()}
            key = tuple(sorted(profile.items()))
            if key not in seen:
                proposals.append(profile)
                seen.add(key)
            if len(proposals) == limit:
                return tuple(proposals)
        fallback = self.predict_sketch(vector)
        key = tuple(sorted(fallback.items()))
        if fallback and key not in seen and len(proposals) < limit:
            proposals.append(fallback)
        return tuple(proposals)

    def sketch_probabilities(self, question_vector: Sequence[float]) -> dict[str, float]:
        vector = _normalized(question_vector)
        values = _sigmoid(self.sketch_presence_weight @ vector + self.sketch_presence_bias)
        return {name: float(values[index]) for index, name in enumerate(self.sketch_names)}

    def score_table(
        self,
        question: str,
        question_vector: Sequence[float],
        table_name: str,
        table_vector: Sequence[float],
    ) -> float:
        features = pair_features(question, question_vector, table_name, table_vector)
        return float(_sigmoid(np.asarray([self.table_weight @ features + self.table_bias]))[0])

    def score_column_roles(
        self,
        question: str,
        question_vector: Sequence[float],
        column_name: str,
        column_type: str,
        column_vector: Sequence[float],
    ) -> dict[str, float]:
        features = pair_features(
            question,
            question_vector,
            column_name,
            column_vector,
            column_type,
        )
        values = _sigmoid(self.role_weight @ features + self.role_bias)
        return {name: float(values[index]) for index, name in enumerate(self.role_names)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "type": "deterministic_ast_proposer",
            "sketch_names": list(self.sketch_names),
            "sketch_profiles": [dict(sorted(profile.items())) for profile in self.sketch_profiles],
            "role_names": list(self.role_names),
            "pair_extra_features": list(PAIR_EXTRA_FEATURES),
            "weights": {
                "sketch_presence": _encode_array(self.sketch_presence_weight),
                "sketch_presence_bias": _encode_array(self.sketch_presence_bias),
                "sketch_count": _encode_array(self.sketch_count_weight),
                "sketch_count_bias": _encode_array(self.sketch_count_bias),
                "sketch_profile": _encode_array(self.sketch_profile_weight),
                "sketch_profile_bias": _encode_array(self.sketch_profile_bias),
                "table": _encode_array(self.table_weight),
                "table_bias": float(self.table_bias),
                "roles": _encode_array(self.role_weight),
                "role_bias": _encode_array(self.role_bias),
            },
            "sketch_thresholds": {
                name: float(self.sketch_thresholds[index])
                for index, name in enumerate(self.sketch_names)
            },
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SQLProposalModel":
        if data.get("type") != "deterministic_ast_proposer":
            raise ValueError("not a deterministic SQL proposer artifact")
        if tuple(data.get("pair_extra_features", ())) != PAIR_EXTRA_FEATURES:
            raise ValueError("SQL proposer pair-feature contract does not match this runtime")
        sketch_names = tuple(str(name) for name in data.get("sketch_names", ()))
        role_names = tuple(str(name) for name in data.get("role_names", ()))
        sketch_profiles = tuple(
            {str(name): int(value) for name, value in profile.items()}
            for profile in data.get("sketch_profiles", ())
        )
        weights = data.get("weights", {})
        thresholds = data.get("sketch_thresholds", {})
        return cls(
            sketch_names=sketch_names,
            role_names=role_names,
            sketch_presence_weight=_decode_array(
                weights.get("sketch_presence"), 2, "sketch_presence"
            ),
            sketch_presence_bias=_decode_array(
                weights.get("sketch_presence_bias"), 1, "sketch_presence_bias"
            ),
            sketch_count_weight=_decode_array(
                weights.get("sketch_count"), 3, "sketch_count"
            ),
            sketch_count_bias=_decode_array(
                weights.get("sketch_count_bias"), 2, "sketch_count_bias"
            ),
            sketch_profiles=sketch_profiles,
            sketch_profile_weight=_decode_array(
                weights.get("sketch_profile"), 2, "sketch_profile"
            ),
            sketch_profile_bias=_decode_array(
                weights.get("sketch_profile_bias"), 1, "sketch_profile_bias"
            ),
            table_weight=_decode_array(weights.get("table"), 1, "table"),
            table_bias=float(weights.get("table_bias", 0.0)),
            role_weight=_decode_array(weights.get("roles"), 2, "roles"),
            role_bias=_decode_array(weights.get("role_bias"), 1, "role_bias"),
            sketch_thresholds=np.asarray(
                [float(thresholds.get(name, 0.5)) for name in sketch_names],
                dtype=np.float32,
            ),
            metadata=dict(data.get("metadata", {})),
            version=int(data.get("version", 0)),
        )

    @classmethod
    def load(cls, path: str) -> "SQLProposalModel":
        with open(path, "rb") as handle:
            payload = handle.read()
        model = cls.from_dict(json.loads(payload.decode("utf-8")))
        return replace(model, source_sha256=hashlib.sha256(payload).hexdigest())

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")


def semantic_signals_from_schema(
    model: SQLProposalModel,
    question: str,
    schema_columns: Sequence[Mapping[str, Any]],
    encode: Callable[[Sequence[str]], Sequence[Sequence[float]]],
    sketch_limit: int = 32,
):
    """Encode one planner schema and return proposer-backed ranking signals."""
    from engine.sql_rank import SemanticSignals

    columns = sorted(
        schema_columns,
        key=lambda column: (str(column["table"]), str(column["name"])),
    )
    tables = sorted({str(column["table"]) for column in columns})

    def value_type(column: Mapping[str, Any]) -> str:
        if column.get("is_date"):
            return "date"
        affinity = str(column.get("affinity", "")).upper()
        if affinity in {"INTEGER", "REAL", "NUMERIC"}:
            return "number"
        if affinity in {"TEXT", "CHAR", "VARCHAR"}:
            return "text"
        return "unknown"

    table_descriptors = [f"table {table}" for table in tables]
    column_types = [value_type(column) for column in columns]
    column_descriptors = [
        f"table {column['table']} column {column['name']} type {kind}"
        for column, kind in zip(columns, column_types)
    ]
    vectors = np.asarray(
        encode([question] + table_descriptors + column_descriptors),
        dtype=np.float32,
    )
    expected = 1 + len(tables) + len(columns)
    if vectors.shape != (expected, model.hidden_size):
        raise ValueError(
            f"proposer encoder returned shape {vectors.shape}, "
            f"expected {(expected, model.hidden_size)}"
        )
    question_vector = vectors[0]
    table_vectors = vectors[1:1 + len(tables)]
    column_vectors = vectors[1 + len(tables):]
    table_global = {
        table: model.score_table(question, question_vector, table, table_vectors[index])
        for index, table in enumerate(tables)
    }
    column_roles: dict[str, dict[tuple[str, str], float]] = {
        role: {} for role in model.role_names
    }
    for index, (column, kind) in enumerate(zip(columns, column_types)):
        scores = model.score_column_roles(
            question,
            question_vector,
            f"{column['table']} {column['name']}",
            kind,
            column_vectors[index],
        )
        key = (str(column["table"]), str(column["name"]))
        for role, score in scores.items():
            column_roles[role][key] = score
    return SemanticSignals(
        column_roles,
        table_global,
        model.propose_sketches(question_vector, sketch_limit),
    )
