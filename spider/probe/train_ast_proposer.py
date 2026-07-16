"""Train deterministic SQL AST sketch and schema-link proposal heads.

The language model is a frozen encoder. Training fits small, inspectable heads
for counted AST sketch features, table selection, and role-specific column
selection. Fit, threshold-calibration, and validation databases are disjoint.

Run from the repository root after ``build_ast_proposal_data.py``::

  python spider/probe/train_ast_proposer.py \
      --out spider/data/sql_proposer.json \
      --report spider/results/ast_proposer.json
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
import random
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import torch
from torch import nn

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.config import BASE_MODEL_ID
from engine.sql_proposal import PAIR_EXTRA_FEATURES, SQLProposalModel, pair_extra_features


CACHE_VERSION = 1
ROLE_NAMES = ("projection", "aggregate", "filter", "group", "having", "order", "join")


@dataclass(frozen=True)
class Entity:
    key: str
    db_id: str
    name: str
    descriptor: str
    value_type: str = ""
    table: str = ""


@dataclass(frozen=True)
class Embeddings:
    questions: np.ndarray
    question_index: Mapping[str, int]
    tables: np.ndarray
    table_index: Mapping[str, int]
    columns: np.ndarray
    column_index: Mapping[str, int]
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class PairDataset:
    question_indices: np.ndarray
    entity_indices: np.ndarray
    extras: np.ndarray
    labels: np.ndarray
    weights: np.ndarray

    def __len__(self) -> int:
        return len(self.question_indices)


class ProposalHeads(nn.Module):
    def __init__(
        self,
        hidden_size: int,
        sketch_count: int,
        count_classes: int,
        profile_count: int,
        role_count: int,
    ):
        super().__init__()
        pair_size = hidden_size + len(PAIR_EXTRA_FEATURES)
        self.sketch_feature_count = sketch_count
        self.count_classes = count_classes
        self.sketch_presence = nn.Linear(hidden_size, sketch_count)
        self.sketch_count = nn.Linear(hidden_size, sketch_count * count_classes)
        self.sketch_profile = nn.Linear(hidden_size, profile_count)
        self.table = nn.Linear(pair_size, 1)
        self.roles = nn.Linear(pair_size, role_count)


def _load_jsonl(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _load_schemas(path: str) -> dict[str, dict[str, Any]]:
    records = _load_jsonl(path)
    schemas = {str(record["db_id"]): record for record in records}
    if len(schemas) != len(records):
        raise ValueError("proposal schema file contains duplicate database IDs")
    return schemas


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(path: str) -> str:
    if not os.path.exists(path):
        return "missing"
    if os.path.isfile(path):
        return _sha256(path)
    digest = hashlib.sha256()
    for directory, directories, names in os.walk(path):
        directories.sort()
        for name in sorted(names):
            item = os.path.join(directory, name)
            relative = os.path.relpath(item, path).replace("\\", "/")
            digest.update(relative.encode("utf-8"))
            digest.update(_sha256(item).encode("ascii"))
    return digest.hexdigest()


def _normalized(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return values / np.maximum(norms, 1e-12)


def _table_key(db_id: str, table: str) -> str:
    return f"{db_id}|table:{table}"


def _column_key(db_id: str, qualified: str) -> str:
    return f"{db_id}|column:{qualified}"


def schema_entities(
    schemas: Mapping[str, Mapping[str, Any]],
) -> tuple[list[Entity], list[Entity], dict[str, list[int]], dict[str, list[int]]]:
    tables: list[Entity] = []
    columns: list[Entity] = []
    table_by_db: dict[str, list[int]] = {}
    column_by_db: dict[str, list[int]] = {}
    for db_id in sorted(schemas):
        schema = schemas[db_id]
        table_by_db[db_id] = []
        column_by_db[db_id] = []
        for table in schema["tables"]:
            table_name = str(table["name"])
            original_table = str(table["original_name"])
            table_by_db[db_id].append(len(tables))
            tables.append(Entity(
                key=_table_key(db_id, table_name),
                db_id=db_id,
                name=original_table,
                descriptor=f"table {original_table}",
                table=table_name,
            ))
            for column in table["columns"]:
                column_name = str(column["name"])
                original_column = str(column["original_name"])
                value_type = str(column.get("type", "unknown"))
                qualified = f"{table_name}.{column_name}"
                column_by_db[db_id].append(len(columns))
                columns.append(Entity(
                    key=_column_key(db_id, qualified),
                    db_id=db_id,
                    name=f"{original_table} {original_column}",
                    descriptor=(
                        f"table {original_table} column {original_column} type {value_type}"
                    ),
                    value_type=value_type,
                    table=table_name,
                ))
    return tables, columns, table_by_db, column_by_db


def _cache_provenance(
    train_path: str,
    validation_path: str,
    schema_path: str,
    adapter: str,
) -> dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "base_model": BASE_MODEL_ID,
        "adapter_sha256": _tree_sha256(adapter),
        "train_sha256": _sha256(train_path),
        "validation_sha256": _sha256(validation_path),
        "schemas_sha256": _sha256(schema_path),
    }


def _encode(
    encoder: Any,
    texts: Sequence[str],
    max_length: int,
    batch_size: int,
) -> np.ndarray:
    values = encoder.encode(texts, max_len=max_length, grad=False, bs=batch_size)
    return _normalized(values.detach().cpu().numpy())


def build_embedding_cache(
    path: str,
    records: Sequence[Mapping[str, Any]],
    tables: Sequence[Entity],
    columns: Sequence[Entity],
    provenance: Mapping[str, Any],
    adapter: str,
    device: torch.device,
    batch_size: int,
) -> None:
    from engine.encoder import LiveQwen

    question_ids = [str(record["example_id"]) for record in records]
    if len(set(question_ids)) != len(question_ids):
        raise ValueError("proposal examples must have unique example IDs")
    started = time.time()
    encoder = LiveQwen(device, warm_lora=adapter, serving=True)
    question_values = _encode(
        encoder, [str(record["question"]) for record in records], 64, batch_size
    )
    table_values = _encode(encoder, [entity.descriptor for entity in tables], 32, batch_size)
    column_values = _encode(
        encoder, [entity.descriptor for entity in columns], 32, batch_size
    )
    metadata = dict(provenance)
    metadata.update({
        "hidden_size": int(question_values.shape[1]),
        "questions": len(question_ids),
        "tables": len(tables),
        "columns": len(columns),
        "seconds": round(time.time() - started, 3),
    })
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    np.savez_compressed(
        path,
        metadata=np.asarray(json.dumps(metadata, sort_keys=True)),
        question_ids=np.asarray(question_ids),
        questions=question_values.astype(np.float16),
        table_keys=np.asarray([entity.key for entity in tables]),
        tables=table_values.astype(np.float16),
        column_keys=np.asarray([entity.key for entity in columns]),
        columns=column_values.astype(np.float16),
    )


def load_embeddings(path: str, provenance: Mapping[str, Any]) -> Embeddings:
    with np.load(path, allow_pickle=False) as data:
        metadata = json.loads(str(data["metadata"].item()))
        for key, expected in provenance.items():
            if metadata.get(key) != expected:
                raise ValueError(f"embedding cache mismatch for {key!r}")
        question_ids = [str(value) for value in data["question_ids"].tolist()]
        table_keys = [str(value) for value in data["table_keys"].tolist()]
        column_keys = [str(value) for value in data["column_keys"].tolist()]
        return Embeddings(
            questions=_normalized(data["questions"]),
            question_index={key: index for index, key in enumerate(question_ids)},
            tables=_normalized(data["tables"]),
            table_index={key: index for index, key in enumerate(table_keys)},
            columns=_normalized(data["columns"]),
            column_index={key: index for index, key in enumerate(column_keys)},
            metadata=metadata,
        )


def split_fit_calibration(
    records: Sequence[Mapping[str, Any]], ratio: float, seed: int
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    database_ids = sorted({str(record["db_id"]) for record in records})
    if len(database_ids) < 2:
        raise ValueError("fit/calibration split requires at least two databases")
    ordered = sorted(
        database_ids,
        key=lambda db_id: hashlib.sha256(f"{seed}:{db_id}".encode()).hexdigest(),
    )
    count = min(len(database_ids) - 1, max(1, round(len(database_ids) * ratio)))
    calibration_ids = frozenset(ordered[:count])
    fit = [record for record in records if record["db_id"] not in calibration_ids]
    calibration = [record for record in records if record["db_id"] in calibration_ids]
    return fit, calibration


def sketch_vocabulary(records: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({
        str(name)
        for record in records
        for name in record["target"]["sketch"]
    }))


def _profile_key(record: Mapping[str, Any]) -> str:
    return json.dumps(record["target"]["sketch"], sort_keys=True, separators=(",", ":"))


def profile_vocabulary(records: Sequence[Mapping[str, Any]]) -> tuple[dict[str, int], ...]:
    return tuple(
        json.loads(value)
        for value in sorted({_profile_key(record) for record in records})
    )


def profile_targets(
    records: Sequence[Mapping[str, Any]],
    profiles: Sequence[Mapping[str, int]],
) -> np.ndarray:
    index = {
        json.dumps(profile, sort_keys=True, separators=(",", ":")): position
        for position, profile in enumerate(profiles)
    }
    return np.asarray([index.get(_profile_key(record), -1) for record in records], dtype=np.int64)


def sketch_targets(
    records: Sequence[Mapping[str, Any]],
    embeddings: Embeddings,
    names: Sequence[str],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    indices = np.asarray(
        [embeddings.question_index[str(record["example_id"])] for record in records],
        dtype=np.int64,
    )
    counts = np.zeros((len(records), len(names)), dtype=np.float32)
    name_index = {name: index for index, name in enumerate(names)}
    for row, record in enumerate(records):
        for name, count in record["target"]["sketch"].items():
            if name in name_index:
                counts[row, name_index[name]] = float(count)
    return indices, (counts > 0).astype(np.float32), counts.astype(np.int64)


def _pair_dataset(
    records: Sequence[Mapping[str, Any]],
    entities: Sequence[Entity],
    entity_by_db: Mapping[str, Sequence[int]],
    entity_embeddings: np.ndarray,
    embeddings: Embeddings,
    roles: Sequence[str] | None,
    max_column_candidates: int,
) -> PairDataset:
    question_indices: list[int] = []
    entity_indices: list[int] = []
    extras: list[np.ndarray] = []
    labels: list[list[float]] = []
    weights: list[list[float]] = []
    role_index = {role: index for index, role in enumerate(roles or ("table",))}

    for record in records:
        db_id = str(record["db_id"])
        question = str(record["question"])
        q_index = embeddings.question_index[str(record["example_id"])]
        q_vector = embeddings.questions[q_index]
        candidates = list(entity_by_db[db_id])
        if roles is None:
            targets = set(str(value) for value in record["target"]["tables"])
            candidate_targets = {
                index: [float(entities[index].table in targets)] for index in candidates
            }
        else:
            role_targets = {
                role: set(str(value) for value in record["target"]["roles"].get(role, ()))
                for role in roles
            }
            positives = set().union(*role_targets.values())
            ranked = sorted(
                candidates,
                key=lambda index: (
                    -int(
                        f"{entities[index].table}."
                        in " ".join(sorted(positives))
                    ),
                    -float(pair_extra_features(
                        question,
                        q_vector,
                        entities[index].name,
                        entity_embeddings[index],
                        entities[index].value_type,
                    )[1]),
                    entities[index].key,
                ),
            )
            positive_indices = {
                index
                for index in candidates
                if entities[index].key.split("|column:", 1)[1] in positives
            }
            kept = list(sorted(positive_indices))
            kept.extend(index for index in ranked if index not in positive_indices)
            candidates = kept[:max(max_column_candidates, len(positive_indices))]
            candidate_targets = {}
            for index in candidates:
                qualified = entities[index].key.split("|column:", 1)[1]
                candidate_targets[index] = [
                    float(qualified in role_targets[role]) for role in roles
                ]

        positive_entities = {
            index for index, target in candidate_targets.items() if any(target)
        }
        positive_types = {entities[index].value_type for index in positive_entities}
        positive_tables = {entities[index].table for index in positive_entities}
        for index in candidates:
            target = candidate_targets[index]
            row_weights = []
            for role, role_position in role_index.items():
                positive = bool(target[role_position])
                hard_negative = (
                    not positive
                    and roles is not None
                    and (
                        entities[index].table in positive_tables
                        or entities[index].value_type in positive_types
                    )
                )
                row_weights.append(2.0 if hard_negative else 1.0)
            question_indices.append(q_index)
            entity_indices.append(index)
            extras.append(pair_extra_features(
                question,
                q_vector,
                entities[index].name,
                entity_embeddings[index],
                entities[index].value_type,
            ))
            labels.append(target)
            weights.append(row_weights)

    width = len(roles or ("table",))
    return PairDataset(
        question_indices=np.asarray(question_indices, dtype=np.int64),
        entity_indices=np.asarray(entity_indices, dtype=np.int64),
        extras=np.asarray(extras, dtype=np.float32).reshape(-1, len(PAIR_EXTRA_FEATURES)),
        labels=np.asarray(labels, dtype=np.float32).reshape(-1, width),
        weights=np.asarray(weights, dtype=np.float32).reshape(-1, width),
    )


def _positive_weights(labels: np.ndarray, cap: float = 20.0) -> np.ndarray:
    positives = labels.sum(axis=0)
    negatives = len(labels) - positives
    return np.clip(negatives / np.maximum(positives, 1.0), 1.0, cap).astype(np.float32)


def _batches(length: int, size: int, generator: np.random.Generator) -> Iterable[np.ndarray]:
    order = generator.permutation(length)
    for start in range(0, length, size):
        yield order[start:start + size]


def train_sketch_heads(
    heads: ProposalHeads,
    question_values: np.ndarray,
    indices: np.ndarray,
    presence: np.ndarray,
    counts: np.ndarray,
    profiles: np.ndarray,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    seed: int,
) -> list[float]:
    parameters = (
        list(heads.sketch_presence.parameters())
        + list(heads.sketch_count.parameters())
        + list(heads.sketch_profile.parameters())
    )
    optimizer = torch.optim.AdamW(parameters, lr=learning_rate, weight_decay=1e-4)
    positive_weight = torch.from_numpy(_positive_weights(presence))
    profile_frequency = np.bincount(profiles, minlength=heads.sketch_profile.out_features)
    profile_weight = np.sqrt(
        len(profiles)
        / np.maximum(heads.sketch_profile.out_features * profile_frequency, 1)
    )
    profile_weight = torch.from_numpy(np.clip(profile_weight, 0.5, 5.0).astype(np.float32))
    rng = np.random.default_rng(seed)
    history = []
    heads.train()
    for _ in range(epochs):
        total = 0.0
        seen = 0
        for rows in _batches(len(indices), batch_size, rng):
            x = torch.from_numpy(question_values[indices[rows]])
            y_presence = torch.from_numpy(presence[rows])
            y_count = torch.from_numpy(counts[rows]).long()
            y_profile = torch.from_numpy(profiles[rows]).long()
            logits = heads.sketch_presence(x)
            raw_presence = nn.functional.binary_cross_entropy_with_logits(
                logits, y_presence, pos_weight=positive_weight, reduction="none"
            )
            predicted_count = heads.sketch_count(x).reshape(
                -1, heads.sketch_feature_count, heads.count_classes
            )
            active = y_presence.bool()
            count_loss = (
                nn.functional.cross_entropy(
                    predicted_count[active], y_count[active] - 1, reduction="mean"
                )
                if active.any()
                else torch.zeros((), dtype=x.dtype)
            )
            profile_loss = nn.functional.cross_entropy(
                heads.sketch_profile(x), y_profile, weight=profile_weight
            )
            loss = raw_presence.mean() + 0.25 * count_loss + profile_loss
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += float(loss.detach()) * len(rows)
            seen += len(rows)
        history.append(total / max(seen, 1))
    return history


def train_pair_head(
    layer: nn.Linear,
    dataset: PairDataset,
    question_values: np.ndarray,
    entity_values: np.ndarray,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    seed: int,
) -> list[float]:
    optimizer = torch.optim.AdamW(layer.parameters(), lr=learning_rate, weight_decay=1e-4)
    positive_weight = torch.from_numpy(_positive_weights(dataset.labels))
    rng = np.random.default_rng(seed)
    history = []
    layer.train()
    for _ in range(epochs):
        total = 0.0
        seen = 0
        for rows in _batches(len(dataset), batch_size, rng):
            interactions = (
                question_values[dataset.question_indices[rows]]
                * entity_values[dataset.entity_indices[rows]]
            )
            values = np.concatenate((interactions, dataset.extras[rows]), axis=1)
            x = torch.from_numpy(values)
            labels = torch.from_numpy(dataset.labels[rows])
            weights = torch.from_numpy(dataset.weights[rows])
            logits = layer(x)
            raw = nn.functional.binary_cross_entropy_with_logits(
                logits, labels, pos_weight=positive_weight, reduction="none"
            )
            loss = (raw * weights).sum() / weights.sum().clamp(min=1.0)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += float(loss.detach()) * len(rows)
            seen += len(rows)
        history.append(total / max(seen, 1))
    return history


def _sketch_outputs(
    heads: ProposalHeads, question_values: np.ndarray, indices: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    heads.eval()
    with torch.no_grad():
        values = torch.from_numpy(question_values[indices])
        probabilities = torch.sigmoid(heads.sketch_presence(values)).numpy()
        counts = heads.sketch_count(values).reshape(
            -1, heads.sketch_feature_count, heads.count_classes
        ).numpy()
        profiles = heads.sketch_profile(values).numpy()
    return probabilities, counts, profiles


def calibrate_thresholds(probabilities: np.ndarray, labels: np.ndarray) -> np.ndarray:
    thresholds = np.full(labels.shape[1], 0.5, dtype=np.float32)
    grid = np.linspace(0.1, 0.9, 33)
    for feature in range(labels.shape[1]):
        truth = labels[:, feature].astype(bool)
        best = (-1.0, -1.0, 0.5)
        for threshold in grid:
            predicted = probabilities[:, feature] >= threshold
            tp = int(np.logical_and(predicted, truth).sum())
            fp = int(np.logical_and(predicted, ~truth).sum())
            fn = int(np.logical_and(~predicted, truth).sum())
            f1 = 2 * tp / max(2 * tp + fp + fn, 1)
            candidate = (f1, -abs(float(threshold) - 0.5), float(threshold))
            if candidate > best:
                best = candidate
        thresholds[feature] = best[2]
    return thresholds


def sketch_metrics(
    probabilities: np.ndarray,
    predicted_count_logits: np.ndarray,
    presence: np.ndarray,
    counts: np.ndarray,
    thresholds: np.ndarray,
    names: Sequence[str],
) -> dict[str, Any]:
    predicted = probabilities >= thresholds
    truth = presence.astype(bool)
    tp = int(np.logical_and(predicted, truth).sum())
    fp = int(np.logical_and(predicted, ~truth).sum())
    fn = int(np.logical_and(~predicted, truth).sum())
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    predicted_counts = np.argmax(predicted_count_logits, axis=2) + 1
    true_counts = counts
    active = np.logical_or(predicted, truth)
    counted_exact = np.logical_or(~active, predicted_counts == true_counts)
    features = {}
    for index, name in enumerate(names):
        feature_truth = truth[:, index]
        feature_predicted = predicted[:, index]
        feature_tp = int(np.logical_and(feature_predicted, feature_truth).sum())
        feature_fp = int(np.logical_and(feature_predicted, ~feature_truth).sum())
        feature_fn = int(np.logical_and(~feature_predicted, feature_truth).sum())
        features[name] = {
            "support": int(feature_truth.sum()),
            "presence_f1": float(
                2 * feature_tp / max(2 * feature_tp + feature_fp + feature_fn, 1)
            ),
            "gold_count_accuracy": float(
                (predicted_counts[feature_truth, index] == true_counts[feature_truth, index]).mean()
            ) if feature_truth.any() else 0.0,
        }
    return {
        "presence_exact": float(np.all(predicted == truth, axis=1).mean()),
        "micro_precision": float(precision),
        "micro_recall": float(recall),
        "micro_f1": float(2 * precision * recall / max(precision + recall, 1e-12)),
        "count_cell_accuracy": float(counted_exact[active].mean()) if active.any() else 1.0,
        "gold_count_accuracy": float(
            (predicted_counts[truth] == true_counts[truth]).mean()
        ) if truth.any() else 1.0,
        "counted_sketch_exact": float(
            np.all(np.logical_and(predicted == truth, counted_exact), axis=1).mean()
        ),
        "features": features,
    }


def profile_metrics(scores: np.ndarray, targets: np.ndarray) -> dict[str, Any]:
    order = np.argsort(-scores, axis=1, kind="stable")
    seen = targets >= 0
    result: dict[str, Any] = {
        "profiles": int(scores.shape[1]),
        "seen_examples": int(seen.sum()),
        "unseen_examples": int((~seen).sum()),
        "seen_fraction": float(seen.mean()),
    }
    for limit in (1, 5, 16, 32):
        width = min(limit, scores.shape[1])
        hits = np.any(order[:, :width] == targets[:, None], axis=1)
        result[f"recall_at_{limit}"] = float(hits.mean())
        result[f"seen_recall_at_{limit}"] = float(hits[seen].mean()) if seen.any() else 0.0
    return result


def _pair_scores(
    layer: nn.Linear,
    dataset: PairDataset,
    question_values: np.ndarray,
    entity_values: np.ndarray,
    batch_size: int,
) -> np.ndarray:
    layer.eval()
    chunks = []
    with torch.no_grad():
        for start in range(0, len(dataset), batch_size):
            rows = slice(start, start + batch_size)
            interactions = (
                question_values[dataset.question_indices[rows]]
                * entity_values[dataset.entity_indices[rows]]
            )
            x = np.concatenate((interactions, dataset.extras[rows]), axis=1)
            chunks.append(torch.sigmoid(layer(torch.from_numpy(x))).numpy())
    return np.concatenate(chunks) if chunks else np.empty_like(dataset.labels)


def ranking_metrics(
    dataset: PairDataset,
    scores: np.ndarray,
    role_names: Sequence[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for role_index, role in enumerate(role_names):
        reciprocal_ranks = []
        top1 = []
        recall3 = []
        start = 0
        while start < len(dataset):
            question_index = dataset.question_indices[start]
            end = start + 1
            while end < len(dataset) and dataset.question_indices[end] == question_index:
                end += 1
            labels = dataset.labels[start:end, role_index].astype(bool)
            if labels.any():
                order = np.lexsort((dataset.entity_indices[start:end], -scores[start:end, role_index]))
                ranked_labels = labels[order]
                first = int(np.flatnonzero(ranked_labels)[0])
                reciprocal_ranks.append(1.0 / (first + 1))
                top1.append(float(ranked_labels[0]))
                recall3.append(float(ranked_labels[:3].sum() / labels.sum()))
            start = end
        result[role] = {
            "examples": len(reciprocal_ranks),
            "mrr": float(np.mean(reciprocal_ranks)) if reciprocal_ranks else 0.0,
            "top1_hit": float(np.mean(top1)) if top1 else 0.0,
            "recall_at_3": float(np.mean(recall3)) if recall3 else 0.0,
        }
    values = [metrics for metrics in result.values() if metrics["examples"]]
    result["macro"] = {
        name: float(np.mean([metrics[name] for metrics in values]))
        for name in ("mrr", "top1_hit", "recall_at_3")
    }
    return result


def _round_metrics(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round_metrics(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_metrics(item) for item in value]
    return value


def _limit_by_database(
    records: Sequence[Mapping[str, Any]], maximum: int
) -> list[Mapping[str, Any]]:
    if not maximum or len(records) <= maximum:
        return list(records)
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        buckets.setdefault(str(record["db_id"]), []).append(record)
    selected = []
    positions = Counter()
    database_ids = sorted(buckets)
    while len(selected) < maximum:
        added = False
        for db_id in database_ids:
            position = positions[db_id]
            if position < len(buckets[db_id]):
                selected.append(buckets[db_id][position])
                positions[db_id] += 1
                added = True
                if len(selected) == maximum:
                    break
        if not added:
            break
    return selected


def _artifact(
    heads: ProposalHeads,
    sketch_names: Sequence[str],
    sketch_profiles: Sequence[Mapping[str, int]],
    thresholds: np.ndarray,
    metadata: Mapping[str, Any],
) -> SQLProposalModel:
    return SQLProposalModel(
        sketch_names=tuple(sketch_names),
        role_names=ROLE_NAMES,
        sketch_presence_weight=heads.sketch_presence.weight.detach().numpy().astype(np.float32),
        sketch_presence_bias=heads.sketch_presence.bias.detach().numpy().astype(np.float32),
        sketch_count_weight=heads.sketch_count.weight.detach().numpy().reshape(
            heads.sketch_feature_count, heads.count_classes, -1
        ).astype(np.float32),
        sketch_count_bias=heads.sketch_count.bias.detach().numpy().reshape(
            heads.sketch_feature_count, heads.count_classes
        ).astype(np.float32),
        sketch_profiles=tuple(dict(profile) for profile in sketch_profiles),
        sketch_profile_weight=(
            heads.sketch_profile.weight.detach().numpy().astype(np.float32)
        ),
        sketch_profile_bias=heads.sketch_profile.bias.detach().numpy().astype(np.float32),
        table_weight=heads.table.weight.detach().numpy().reshape(-1).astype(np.float32),
        table_bias=float(heads.table.bias.detach().item()),
        role_weight=heads.roles.weight.detach().numpy().astype(np.float32),
        role_bias=heads.roles.bias.detach().numpy().astype(np.float32),
        sketch_thresholds=thresholds.astype(np.float32),
        metadata=dict(metadata),
    )


def main() -> None:
    data = os.path.join(ROOT, "spider", "data")
    results = os.path.join(ROOT, "spider", "results")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", default=os.path.join(data, "ast_proposals_train.jsonl"))
    parser.add_argument(
        "--validation", default=os.path.join(data, "ast_proposals_validation.jsonl")
    )
    parser.add_argument("--schemas", default=os.path.join(data, "ast_proposal_schemas.jsonl"))
    parser.add_argument("--adapter", default=os.path.join(ROOT, "engine", "data", "qwen_lora"))
    parser.add_argument("--cache", default=os.path.join(data, "ast_proposal_embeddings.npz"))
    parser.add_argument("--out", default=os.path.join(data, "sql_proposer.json"))
    parser.add_argument("--report", default=os.path.join(results, "ast_proposer.json"))
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--encode-batch-size", type=int, default=96)
    parser.add_argument("--learning-rate", type=float, default=3e-3)
    parser.add_argument("--calibration-ratio", type=float, default=0.1)
    parser.add_argument("--max-column-candidates", type=int, default=32)
    parser.add_argument("--max-train", type=int, default=0)
    parser.add_argument("--max-validation", type=int, default=0)
    parser.add_argument("--seed", type=int, default=19)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--rebuild-cache", action="store_true")
    args = parser.parse_args()

    if (
        args.epochs < 1
        or args.batch_size < 1
        or args.encode_batch_size < 1
        or args.max_column_candidates < 1
    ):
        parser.error("epochs, batch sizes, and max column candidates must be positive")
    if args.learning_rate <= 0:
        parser.error("learning rate must be positive")
    if args.max_train < 0 or args.max_validation < 0:
        parser.error("max train and max validation must be nonnegative")
    if not 0.0 < args.calibration_ratio < 0.5:
        parser.error("calibration ratio must be between zero and 0.5")
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA was requested but is unavailable")

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.use_deterministic_algorithms(True)
    device = torch.device(args.device)

    train_records = _limit_by_database(_load_jsonl(args.train), args.max_train)
    validation_records = _limit_by_database(
        _load_jsonl(args.validation), args.max_validation
    )
    schemas = _load_schemas(args.schemas)
    records = train_records + validation_records
    missing_schemas = sorted({str(record["db_id"]) for record in records} - set(schemas))
    if missing_schemas:
        raise ValueError(f"proposal schemas are missing databases: {missing_schemas}")

    tables, columns, table_by_db, column_by_db = schema_entities(schemas)
    provenance = _cache_provenance(args.train, args.validation, args.schemas, args.adapter)
    if args.rebuild_cache or not os.path.exists(args.cache):
        print("encoding questions and schema entities with frozen Qwen", flush=True)
        build_embedding_cache(
            args.cache,
            _load_jsonl(args.train) + _load_jsonl(args.validation),
            tables,
            columns,
            provenance,
            args.adapter,
            device,
            args.encode_batch_size,
        )
    embeddings = load_embeddings(args.cache, provenance)

    fit, calibration = split_fit_calibration(
        train_records, args.calibration_ratio, args.seed
    )
    validation_db_ids = {str(record["db_id"]) for record in validation_records}
    if validation_db_ids & {str(record["db_id"]) for record in train_records}:
        raise ValueError("training and validation proposal databases overlap")
    sketch_names = sketch_vocabulary(train_records)
    sketch_profiles = profile_vocabulary(fit)
    count_classes = int(max(
        count
        for record in train_records
        for count in record["target"]["sketch"].values()
    ))
    fit_indices, fit_presence, fit_counts = sketch_targets(fit, embeddings, sketch_names)
    calibration_indices, calibration_presence, calibration_counts = sketch_targets(
        calibration, embeddings, sketch_names
    )
    validation_indices, validation_presence, validation_counts = sketch_targets(
        validation_records, embeddings, sketch_names
    )
    fit_profile_targets = profile_targets(fit, sketch_profiles)
    calibration_profile_targets = profile_targets(calibration, sketch_profiles)
    validation_profile_targets = profile_targets(validation_records, sketch_profiles)

    print(
        f"fit={len(fit)} calibration={len(calibration)} validation={len(validation_records)} "
        f"sketch_features={len(sketch_names)} profiles={len(sketch_profiles)}",
        flush=True,
    )
    fit_tables = _pair_dataset(
        fit, tables, table_by_db, embeddings.tables, embeddings, None,
        args.max_column_candidates,
    )
    fit_columns = _pair_dataset(
        fit, columns, column_by_db, embeddings.columns, embeddings, ROLE_NAMES,
        args.max_column_candidates,
    )
    validation_tables = _pair_dataset(
        validation_records, tables, table_by_db, embeddings.tables, embeddings, None,
        args.max_column_candidates,
    )
    validation_columns = _pair_dataset(
        validation_records, columns, column_by_db, embeddings.columns, embeddings,
        ROLE_NAMES, max(len(columns), args.max_column_candidates),
    )
    print(
        f"training pairs: tables={len(fit_tables)} columns={len(fit_columns)}",
        flush=True,
    )

    heads = ProposalHeads(
        embeddings.questions.shape[1],
        len(sketch_names),
        count_classes,
        len(sketch_profiles),
        len(ROLE_NAMES),
    )
    started = time.time()
    sketch_history = train_sketch_heads(
        heads, embeddings.questions, fit_indices, fit_presence, fit_counts,
        fit_profile_targets,
        args.epochs, args.batch_size, args.learning_rate, args.seed,
    )
    table_history = train_pair_head(
        heads.table, fit_tables, embeddings.questions, embeddings.tables,
        args.epochs, args.batch_size, args.learning_rate, args.seed + 1,
    )
    role_history = train_pair_head(
        heads.roles, fit_columns, embeddings.questions, embeddings.columns,
        args.epochs, args.batch_size, args.learning_rate, args.seed + 2,
    )

    (
        calibration_probabilities,
        calibration_predicted_counts,
        calibration_profile_scores,
    ) = _sketch_outputs(
        heads, embeddings.questions, calibration_indices
    )
    thresholds = calibrate_thresholds(calibration_probabilities, calibration_presence)
    (
        validation_probabilities,
        validation_predicted_counts,
        validation_profile_scores,
    ) = _sketch_outputs(
        heads, embeddings.questions, validation_indices
    )
    table_scores = _pair_scores(
        heads.table, validation_tables, embeddings.questions, embeddings.tables,
        args.batch_size,
    )
    role_scores = _pair_scores(
        heads.roles, validation_columns, embeddings.questions, embeddings.columns,
        args.batch_size,
    )

    validation_sketch_metrics = sketch_metrics(
        validation_probabilities, validation_predicted_counts,
        validation_presence, validation_counts, thresholds, sketch_names,
    )
    validation_profile_metrics = profile_metrics(
        validation_profile_scores, validation_profile_targets
    )
    table_metrics = ranking_metrics(validation_tables, table_scores, ("table",))
    role_metrics = ranking_metrics(validation_columns, role_scores, ROLE_NAMES)
    elapsed = time.time() - started
    hard_negatives = int(np.logical_and(fit_columns.labels == 0, fit_columns.weights > 1).sum())
    report = _round_metrics({
        "version": 1,
        "objective": "deterministic top-k AST sketch and role-aware schema linking",
        "encoder": {
            "base_model": BASE_MODEL_ID,
            "adapter_sha256": provenance["adapter_sha256"],
            "hidden_size": embeddings.questions.shape[1],
            "frozen": True,
        },
        "data": {
            "train_sha256": provenance["train_sha256"],
            "validation_sha256": provenance["validation_sha256"],
            "schemas_sha256": provenance["schemas_sha256"],
        },
        "split": {
            "fit_examples": len(fit),
            "fit_databases": len({record["db_id"] for record in fit}),
            "calibration_examples": len(calibration),
            "calibration_databases": len({record["db_id"] for record in calibration}),
            "validation_examples": len(validation_records),
            "validation_databases": len(validation_db_ids),
            "database_disjoint": True,
        },
        "training": {
            "seed": args.seed,
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "learning_rate": args.learning_rate,
            "max_column_candidates": args.max_column_candidates,
            "table_pairs": len(fit_tables),
            "column_pairs": len(fit_columns),
            "role_hard_negative_cells": hard_negatives,
            "sketch_profiles": len(sketch_profiles),
            "seconds": elapsed,
            "loss": {
                "sketch_first": sketch_history[0],
                "sketch_last": sketch_history[-1],
                "table_first": table_history[0],
                "table_last": table_history[-1],
                "roles_first": role_history[0],
                "roles_last": role_history[-1],
            },
        },
        "calibration": {
            "source": "training databases held out from weight fitting",
            "sketch": sketch_metrics(
                calibration_probabilities, calibration_predicted_counts,
                calibration_presence, calibration_counts, thresholds, sketch_names,
            ),
            "profile_beam": profile_metrics(
                calibration_profile_scores, calibration_profile_targets
            ),
        },
        "validation": {
            "sketch": validation_sketch_metrics,
            "profile_beam": validation_profile_metrics,
            "tables": table_metrics,
            "column_roles": role_metrics,
        },
    })
    metadata = {
        "base_model": BASE_MODEL_ID,
        "adapter_sha256": provenance["adapter_sha256"],
        "threshold_source": "database-disjoint calibration subset",
        "training_data": {
            "train_sha256": provenance["train_sha256"],
            "validation_sha256": provenance["validation_sha256"],
            "schemas_sha256": provenance["schemas_sha256"],
        },
        "maximum_feature_count": count_classes,
        "validation_metrics": report["validation"],
    }
    artifact = _artifact(
        heads, sketch_names, sketch_profiles, thresholds, metadata
    )
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    artifact.save(args.out)
    os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report["validation"], indent=2, sort_keys=True), flush=True)
    print(f"wrote {args.out}", flush=True)
    print(f"wrote {args.report}", flush=True)


if __name__ == "__main__":
    main()
