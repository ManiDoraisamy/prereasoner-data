"""Train the frozen Phase 6 SQL AST ranker on Spider denotations.

Training and validation are database-disjoint. Exported models perform fixed
feature extraction and dependency-free deterministic inference.

Run from the repository root after ``fetch_data.py --include-train``:

  python spider/probe/train_ast_ranker.py --dbs spider/data/dbs \
      --out engine/data/sql_ranker.json --cache spider/data/ranker_cache.jsonl
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
import sys
import time
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from .evalutil import build_mem_db, exec_sql_timed, load_capped
    from .spider_eval import compare, recursive_gold_table_names, spider_foreign_keys
except ImportError:  # direct script execution
    from evalutil import build_mem_db, exec_sql_timed, load_capped
    from spider_eval import compare, recursive_gold_table_names, spider_foreign_keys

from engine.sql_learned_rank import (
    DecisionTree,
    LinearRankerModel,
    RankerModel,
    TreeEnsembleRankerModel,
    TreeNode,
    learned_feature_vector,
    learned_question_features,
)
from engine.sql_search import SQLSearcher


CACHE_VERSION = 1


def _load_examples(paths: Sequence[str], allow_dev: bool) -> list[dict[str, Any]]:
    examples = []
    for path in paths:
        if os.path.basename(path).lower().startswith("dev") and not allow_dev:
            raise ValueError(
                f"refusing to train on Spider dev split {path!r}; pass --allow-dev only for diagnostics"
            )
        with open(path, encoding="utf-8") as handle:
            examples.extend(json.load(handle))
    return examples


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_or_encode_question_vectors(
    encoder,
    examples: Sequence[Mapping[str, Any]],
    proposer_sha256: str,
    cache_path: str,
    batch_size: int = 256,
) -> np.ndarray:
    """Encode questions with resumable, corpus-fingerprinted checkpoints."""
    questions = [str(example["question"]) for example in examples]
    # Fingerprint on the ENCODER ADAPTER too, not just the corpus + proposer id: the question vectors are
    # produced by THIS encoder, so a new adapter must invalidate the cache (else stale, coordinate-incompatible
    # vectors are silently reused — the same class of bug as the proposer/adapter mismatch).
    from engine.sql_proposal_runtime import _tree_sha256
    from engine.config import DATA_DIR
    adapter_sha256 = _tree_sha256(os.path.join(str(DATA_DIR), "qwen_lora"))
    fingerprint = hashlib.sha256(json.dumps(
        {"questions": questions, "proposer_sha256": proposer_sha256, "adapter_sha256": adapter_sha256},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    completed = 0
    vectors = None
    if cache_path and os.path.exists(cache_path):
        cached = np.load(cache_path, allow_pickle=False)
        cached_fingerprint = str(cached["fingerprint"].item())
        if cached_fingerprint != fingerprint:
            raise ValueError("question-vector cache does not match this training corpus")
        vectors = np.asarray(cached["vectors"], dtype=np.float32)
        completed = len(vectors)
        if completed > len(questions):
            raise ValueError("question-vector cache contains too many rows")
        print(f"resuming question embeddings at {completed}/{len(questions)}", flush=True)

    chunks = [vectors] if vectors is not None and len(vectors) else []
    for start in range(completed, len(questions), max(1, batch_size)):
        stop = min(len(questions), start + max(1, batch_size))
        chunks.append(encoder._encode(questions[start:stop]))
        vectors = np.concatenate(chunks, axis=0)
        chunks = [vectors]
        if cache_path:
            os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
            temporary = cache_path + ".tmp.npz"
            np.savez_compressed(temporary, fingerprint=fingerprint, vectors=vectors)
            os.replace(temporary, cache_path)
        print(f"  encoded {stop}/{len(questions)} questions", flush=True)
    if vectors is None:
        return np.empty((0, 0), dtype=np.float32)
    return vectors


def _execute(con, sql: str, timeout: float, row_limit: int):
    return exec_sql_timed(con, sql, timeout=timeout, max_rows=row_limit)


def build_training_groups(
    examples: Sequence[Mapping[str, Any]],
    metas: Mapping[str, Mapping[str, Any]],
    dbs: str,
    config: str,
    cap: int,
    pool: int,
    negative_pool: int,
    proposal_model=None,
    proposal_encoder=None,
    question_vectors: Sequence[Any] = (),
    profile_max_candidates: int = 32,
    profile_per_profile: int = 4,
    profile_generation_penalty: float = 5.0,
    profile_binding_quality_weight: float = 2.0,
    execution_timeout: float = 1.0,
    execution_row_limit: int = 10000,
    progress_every: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Generate candidates, execute them, and retain positives plus hard negatives."""
    groups = []
    stats: Counter[str] = Counter()
    db_cache: dict[str, dict[str, dict[str, Any]]] = {}
    active_db_id = None
    active_connection = None
    active_searcher = None
    active_tables = None
    started = time.time()
    from engine.sql_profile_expansion import ProfileSearchConfig

    profile_config = ProfileSearchConfig(
        max_candidates=profile_max_candidates,
        per_profile=profile_per_profile,
        generation_penalty=profile_generation_penalty,
        binding_quality_weight=profile_binding_quality_weight,
    )
    proposal_provider = None
    if proposal_model is not None:
        from engine.sql_proposal_runtime import ProposalSignalProvider

        proposal_provider = ProposalSignalProvider(proposal_model, proposal_encoder)
    try:
        for index, example in enumerate(examples, 1):
            db_id = str(example["db_id"])
            meta = metas.get(db_id)
            db_path = os.path.join(dbs, db_id + ".sqlite")
            if meta is None or not os.path.exists(db_path):
                stats["missing_database"] += 1
                continue

            close_after_example = config == "gold_tables"
            if close_after_example:
                if db_id not in db_cache:
                    db_cache[db_id] = load_capped(db_path, cap)
                capped = db_cache[db_id]
                names = [name.lower() for name in recursive_gold_table_names(example, metas)]
                tables = [capped[name] for name in names if name in capped] or list(capped.values())
                con = build_mem_db(tables)
                searcher = SQLSearcher.from_tables(
                    tables, spider_foreign_keys(meta), max_candidates=pool,
                )
            else:
                if active_db_id != db_id:
                    if active_connection is not None:
                        active_connection.close()
                    capped = load_capped(db_path, cap)
                    active_tables = list(capped.values())
                    active_connection = build_mem_db(active_tables)
                    active_searcher = SQLSearcher.from_tables(
                        active_tables, spider_foreign_keys(meta), max_candidates=pool,
                    )
                    active_db_id = db_id
                tables = active_tables
                con = active_connection
                searcher = active_searcher

            gold_rows, gold_error = _execute(
                con, str(example["query"]), execution_timeout, execution_row_limit
            )
            if gold_error is not None:
                if close_after_example:
                    con.close()
                stats["gold_error"] += 1
                continue

            signals = None
            if proposal_model is not None:
                signals = proposal_provider.signals(
                    str(example["question"]), searcher.schema, question_vectors[index - 1]
                )
            candidates = searcher.search(
                str(example["question"]),
                semantic_signals=signals,
                profile_config=profile_config,
            )
            records = []
            positive_count = 0
            for rank, candidate in enumerate(candidates):
                rows, error = _execute(
                    con, candidate.sql, execution_timeout, execution_row_limit
                )
                correct = bool(error is None and compare(gold_rows, rows).get("strict"))
                positive_count += int(correct)
                if rank < negative_pool or correct:
                    records.append({
                        "sql": candidate.sql,
                        "rank": rank,
                        "correct": correct,
                        "features": learned_feature_vector(
                            str(example["question"]), candidate, rank, len(candidates)
                        ),
                    })
            if close_after_example:
                con.close()

            stats["examples"] += 1
            stats["baseline_correct"] += int(bool(records and records[0]["correct"]))
            stats["oracle_correct"] += int(positive_count > 0)
            stats["positive_candidates"] += positive_count
            groups.append({
                "db_id": db_id,
                "question": str(example["question"]),
                "baseline_correct": bool(records and records[0]["correct"]),
                "oracle_correct": positive_count > 0,
                "candidates": records,
            })
            if progress_every and index % progress_every == 0:
                elapsed = time.time() - started
                print(
                    f"  generated {index}/{len(examples)} examples in {elapsed:.1f}s "
                    f"(oracle={stats['oracle_correct']})",
                    flush=True,
                )
    finally:
        if active_connection is not None:
            active_connection.close()
    return groups, dict(stats)


def split_by_database(
    groups: Sequence[Mapping[str, Any]], validation_ratio: float, seed: int
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], tuple[str, ...]]:
    db_ids = sorted({str(group["db_id"]) for group in groups})
    if len(db_ids) < 2:
        raise ValueError("schema-disjoint ranker validation requires at least two databases")
    ordered = sorted(
        db_ids,
        key=lambda db_id: hashlib.sha256(f"{seed}:{db_id}".encode("utf-8")).hexdigest(),
    )
    validation_count = min(len(db_ids) - 1, max(1, round(len(db_ids) * validation_ratio)))
    validation_dbs = frozenset(ordered[:validation_count])
    train = [group for group in groups if group["db_id"] not in validation_dbs]
    validation = [group for group in groups if group["db_id"] in validation_dbs]
    return train, validation, tuple(sorted(validation_dbs))


def summarize_groups(
    groups: Sequence[Mapping[str, Any]], expected_examples: int
) -> dict[str, int]:
    return {
        "examples": len(groups),
        "skipped_examples": max(0, expected_examples - len(groups)),
        "baseline_correct": sum(bool(group["baseline_correct"]) for group in groups),
        "oracle_correct": sum(bool(group["oracle_correct"]) for group in groups),
        "positive_candidates": sum(
            bool(candidate["correct"])
            for group in groups for candidate in group["candidates"]
        ),
    }


def _feature_space(groups: Iterable[Mapping[str, Any]]) -> tuple[tuple[str, ...], dict[str, float]]:
    sums: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    names = set()
    for group in groups:
        for candidate in group["candidates"]:
            for name, value in candidate["features"].items():
                names.add(name)
                sums[name] += float(value) ** 2
                counts[name] += 1
    ordered = tuple(sorted(names))
    scales = {
        name: max(0.25, math.sqrt(sums[name] / max(counts[name], 1)))
        for name in ordered
    }
    return ordered, scales


def _vector(
    features: Mapping[str, float], names: Sequence[str], scales: Mapping[str, float]
) -> np.ndarray:
    return np.asarray(
        [float(features.get(name, 0.0)) / float(scales.get(name, 1.0)) for name in names],
        dtype=np.float64,
    )


def _pair_matrix(
    groups: Sequence[Mapping[str, Any]],
    names: Sequence[str],
    scales: Mapping[str, float],
    negatives: int,
) -> np.ndarray:
    pairs = []
    for group in groups:
        candidates = group["candidates"]
        positives = [candidate for candidate in candidates if candidate["correct"]]
        negative_candidates = [candidate for candidate in candidates if not candidate["correct"]]
        if not positives or not negative_candidates:
            continue
        positive = min(positives, key=lambda candidate: (candidate["rank"], candidate["sql"]))
        positive_vector = _vector(positive["features"], names, scales)
        for negative in negative_candidates[:max(1, negatives)]:
            pairs.append(positive_vector - _vector(negative["features"], names, scales))
    if not pairs:
        raise ValueError("no positive/negative candidate pairs were available for ranker training")
    return np.stack(pairs)


def ranking_metrics(
    groups: Sequence[Mapping[str, Any]], model: RankerModel | None = None
) -> dict[str, float | int]:
    top1 = 0
    reciprocal_rank = 0.0
    oracle = 0
    for group in groups:
        candidates = list(group["candidates"])
        if model is not None:
            candidates.sort(key=lambda candidate: (
                -model.score(candidate["features"]), candidate["sql"]
            ))
        correct_ranks = [index for index, candidate in enumerate(candidates) if candidate["correct"]]
        if correct_ranks:
            oracle += 1
            reciprocal_rank += 1.0 / (correct_ranks[0] + 1.0)
            top1 += int(correct_ranks[0] == 0)
    total = len(groups)
    return {
        "examples": total,
        "oracle": oracle,
        "oracle_pct": round(100.0 * oracle / max(total, 1), 3),
        "top1": top1,
        "top1_pct": round(100.0 * top1 / max(total, 1), 3),
        "mrr": round(reciprocal_rank / max(total, 1), 6),
    }


def calibrate_promotion_gate(
    groups: Sequence[Mapping[str, Any]], model: RankerModel
) -> dict[str, Any]:
    """Select the lowest zero-loss margin with maximal corrective promotions."""
    rows = []
    for group in groups:
        candidates = list(group["candidates"])
        if not candidates:
            continue
        fallback = min(candidates, key=lambda candidate: (candidate["rank"], candidate["sql"]))
        generated = [
            candidate for candidate in candidates
            if candidate is not fallback
            and "heuristic_value.profile_binding_quality" in candidate["features"]
        ]
        if not generated:
            continue
        challenger = max(
            generated,
            key=lambda candidate: (model.score(candidate["features"]), candidate["sql"]),
        )
        rows.append((
            model.score(challenger["features"]) - model.score(fallback["features"]),
            bool(fallback["correct"]),
            bool(challenger["correct"]),
        ))
    thresholds = (0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0)
    audits = []
    for threshold in thresholds:
        selected = [row for row in rows if row[0] >= threshold]
        wins = sum(not fallback and challenger for _, fallback, challenger in selected)
        losses = sum(fallback and not challenger for _, fallback, challenger in selected)
        audits.append({
            "margin_threshold": threshold,
            "promotions": len(selected),
            "wins": wins,
            "losses": losses,
            "net_wins": wins - losses,
        })
    safe = [audit for audit in audits if audit["losses"] == 0]
    selected = max(
        safe or audits,
        key=lambda audit: (
            audit["net_wins"], audit["promotions"], -audit["margin_threshold"]
        ),
    )
    return {
        **selected,
        "eligible_examples": len(rows),
        "selection_rule": "max_net_wins_then_promotions_with_zero_observed_losses",
        "calibration_curve": audits,
    }


def fit_pairwise_ranker(
    train_groups: Sequence[Mapping[str, Any]],
    validation_groups: Sequence[Mapping[str, Any]],
    epochs: int = 250,
    learning_rate: float = 0.03,
    l2: float = 0.002,
    negatives: int = 12,
    metadata: Mapping[str, Any] | None = None,
) -> LinearRankerModel:
    """Fit pairwise logistic loss with deterministic full-batch Adam."""
    names, scales = _feature_space(train_groups)
    pairs = _pair_matrix(train_groups, names, scales, negatives)
    weights = np.zeros(len(names), dtype=np.float64)
    if "baseline_score" in names:
        baseline_index = names.index("baseline_score")
        weights[baseline_index] = scales["baseline_score"]

    first_moment = np.zeros_like(weights)
    second_moment = np.zeros_like(weights)
    best_weights = weights.copy()
    baseline_probe = LinearRankerModel(
        dict(zip(names, weights)), scales, {"training_epoch": 0}
    )
    baseline_metrics = ranking_metrics(validation_groups, baseline_probe)
    baseline_loss = float(np.logaddexp(0.0, -(pairs @ weights)).mean())
    best_key = (
        float(baseline_metrics["top1_pct"]),
        float(baseline_metrics["mrr"]),
        -baseline_loss,
    )
    best_epoch = 0
    for epoch in range(1, max(1, epochs) + 1):
        margins = pairs @ weights
        probability = np.empty_like(margins)
        positive = margins >= 0
        exp_negative = np.exp(-margins[positive])
        probability[positive] = exp_negative / (1.0 + exp_negative)
        exp_positive = np.exp(margins[~positive])
        probability[~positive] = 1.0 / (1.0 + exp_positive)
        gradient = -(pairs.T @ probability) / len(pairs) + l2 * weights

        first_moment = 0.9 * first_moment + 0.1 * gradient
        second_moment = 0.999 * second_moment + 0.001 * gradient * gradient
        corrected_first = first_moment / (1.0 - 0.9 ** epoch)
        corrected_second = second_moment / (1.0 - 0.999 ** epoch)
        weights -= learning_rate * corrected_first / (np.sqrt(corrected_second) + 1e-8)

        if epoch == 1 or epoch % 5 == 0 or epoch == epochs:
            probe = LinearRankerModel(
                dict(zip(names, weights)), scales, {"training_epoch": epoch}
            )
            metrics = ranking_metrics(validation_groups, probe)
            loss = float(np.logaddexp(0.0, -(pairs @ weights)).mean())
            key = (float(metrics["top1_pct"]), float(metrics["mrr"]), -loss)
            if key > best_key:
                best_key = key
                best_weights = weights.copy()
                best_epoch = epoch

    model_metadata = dict(metadata or {})
    model_metadata.update({
        "algorithm": "full_batch_pairwise_logistic_adam",
        "best_epoch": best_epoch,
        "epochs": epochs,
        "learning_rate": learning_rate,
        "l2": l2,
        "negative_pairs_per_example": negatives,
        "pair_count": len(pairs),
        "feature_count": len(names),
    })
    return LinearRankerModel(dict(zip(names, best_weights)), scales, model_metadata)


def _pointwise_matrix(
    groups: Sequence[Mapping[str, Any]], names: Sequence[str]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rows = []
    labels = []
    weights = []
    unit_scales = {name: 1.0 for name in names}
    for group in groups:
        candidates = group["candidates"]
        positive_count = sum(bool(candidate["correct"]) for candidate in candidates)
        negative_count = len(candidates) - positive_count
        if not positive_count or not negative_count:
            continue
        for candidate in candidates:
            correct = bool(candidate["correct"])
            rows.append(_vector(candidate["features"], names, unit_scales))
            labels.append(int(correct))
            weights.append(
                0.5 / positive_count if correct else 0.5 / negative_count
            )
    if not rows:
        raise ValueError("no mixed positive/negative groups were available for tree-ranker training")
    return np.stack(rows), np.asarray(labels), np.asarray(weights, dtype=np.float64)


def _export_gradient_boosting(
    classifier,
    names: Sequence[str],
    tree_count: int,
    metadata: Mapping[str, Any] | None = None,
) -> TreeEnsembleRankerModel:
    trees = []
    for estimator in classifier.estimators_[:tree_count, 0]:
        source = estimator.tree_
        nodes = []
        for index in range(source.node_count):
            feature_index = int(source.feature[index])
            if feature_index < 0:
                nodes.append(TreeNode(value=float(source.value[index, 0, 0])))
            else:
                nodes.append(TreeNode(
                    feature=str(names[feature_index]),
                    threshold=float(source.threshold[index]),
                    left=int(source.children_left[index]),
                    right=int(source.children_right[index]),
                ))
        trees.append(DecisionTree(tuple(nodes)))
    prior = float(classifier.init_.class_prior_[1])
    prior = min(max(prior, 1e-12), 1.0 - 1e-12)
    return TreeEnsembleRankerModel(
        trees=tuple(trees),
        learning_rate=float(classifier.learning_rate),
        base_score=math.log(prior / (1.0 - prior)),
        metadata=dict(metadata or {}),
    )


def fit_tree_ranker(
    train_groups: Sequence[Mapping[str, Any]],
    validation_groups: Sequence[Mapping[str, Any]],
    estimators: int = 120,
    learning_rate: float = 0.03,
    max_depth: int = 3,
    min_samples_leaf: int = 20,
    seed: int = 1729,
    metadata: Mapping[str, Any] | None = None,
    refit_on_validation: bool = True,
) -> RankerModel:
    """Fit pointwise boosted trees and select tree count on schema-held-out top-1."""
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        import sklearn
    except ImportError as exc:
        raise RuntimeError(
            "tree-ranker training requires scikit-learn from training/requirements.txt"
        ) from exc

    names, _ = _feature_space(train_groups)
    matrix, labels, sample_weights = _pointwise_matrix(train_groups, names)
    classifier = GradientBoostingClassifier(
        n_estimators=max(1, estimators),
        learning_rate=learning_rate,
        max_depth=max_depth,
        min_samples_leaf=max(1, min_samples_leaf),
        subsample=1.0,
        random_state=seed,
    )
    classifier.fit(matrix, labels, sample_weight=sample_weights)

    model_metadata = dict(metadata or {})
    model_metadata.update({
        "algorithm": "pointwise_gradient_boosted_trees",
        "scikit_learn_version": sklearn.__version__,
        "estimators_trained": estimators,
        "learning_rate": learning_rate,
        "max_depth": max_depth,
        "min_samples_leaf": min_samples_leaf,
        "feature_count": len(names),
        "training_row_count": len(matrix),
    })
    baseline_model: RankerModel = LinearRankerModel(
        {"baseline_score": 1.0}, metadata={**model_metadata, "best_tree_count": 0}
    )
    best_model = baseline_model
    baseline_metrics = ranking_metrics(validation_groups, baseline_model)
    best_key = (float(baseline_metrics["top1_pct"]), float(baseline_metrics["mrr"]))
    for tree_count in range(1, estimators + 1):
        if tree_count != 1 and tree_count % 5 and tree_count != estimators:
            continue
        candidate = _export_gradient_boosting(
            classifier, names, tree_count,
            {**model_metadata, "best_tree_count": tree_count},
        )
        metrics = ranking_metrics(validation_groups, candidate)
        key = (float(metrics["top1_pct"]), float(metrics["mrr"]))
        if key > best_key:
            best_key = key
            best_model = candidate
    if not isinstance(best_model, TreeEnsembleRankerModel):
        return best_model

    best_tree_count = len(best_model.trees)
    selection_metrics = {
        "train": ranking_metrics(train_groups, best_model),
        "validation": ranking_metrics(validation_groups, best_model),
    }
    if not refit_on_validation:
        return best_model.with_metadata({
            **best_model.metadata,
            "selection_metrics_before_refit": selection_metrics,
            "refit_on_all_training": False,
        })
    refit_groups = list(train_groups) + list(validation_groups)
    refit_matrix, refit_labels, refit_weights = _pointwise_matrix(refit_groups, names)
    refit_classifier = GradientBoostingClassifier(
        n_estimators=best_tree_count,
        learning_rate=learning_rate,
        max_depth=max_depth,
        min_samples_leaf=max(1, min_samples_leaf),
        subsample=1.0,
        random_state=seed,
    )
    refit_classifier.fit(refit_matrix, refit_labels, sample_weight=refit_weights)
    return _export_gradient_boosting(
        refit_classifier,
        names,
        best_tree_count,
        {
            **model_metadata,
            "best_tree_count": best_tree_count,
            "selection_metrics_before_refit": selection_metrics,
            "refit_on_all_training": True,
            "refit_row_count": len(refit_matrix),
        },
    )


def _cache_header(args, train_paths: Sequence[str]) -> dict[str, Any]:
    return {
        "kind": "phase6_cache",
        "version": CACHE_VERSION,
        "train": [os.path.abspath(path) for path in train_paths],
        "config": args.config,
        "cap": args.cap,
        "pool": args.pool,
        "negative_pool": args.negative_pool,
        "max_examples": args.max_examples,
        "proposer_model": os.path.abspath(args.proposer_model) if args.proposer_model else "",
        "proposer_sha256": _file_sha256(args.proposer_model) if args.proposer_model else "",
        "profile_max_candidates": args.profile_max_candidates,
        "profile_per_profile": args.profile_per_profile,
        "profile_generation_penalty": args.profile_generation_penalty,
        "profile_binding_quality_weight": args.profile_binding_quality_weight,
        "execution_timeout": args.execution_timeout,
        "execution_row_limit": args.execution_row_limit,
    }


def save_cache(path: str, header: Mapping[str, Any], groups: Sequence[Mapping[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(header), sort_keys=True) + "\n")
        for group in groups:
            handle.write(json.dumps(group, sort_keys=True) + "\n")


def load_cache(path: str, expected_header: Mapping[str, Any]) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        header = json.loads(next(handle))
        if header != dict(expected_header):
            raise ValueError("ranker cache settings do not match this training run; rebuild the cache")
        groups = [json.loads(line) for line in handle if line.strip()]
    for group in groups:
        question_features = learned_question_features(str(group["question"]))
        for candidate in group["candidates"]:
            candidate["features"].update(question_features)
    return groups


def main() -> None:
    parser = argparse.ArgumentParser()
    data = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
    parser.add_argument(
        "--train",
        action="append",
        default=[],
        help="Spider train JSON; repeat to combine additional training splits",
    )
    parser.add_argument("--tables", default=os.path.join(data, "tables.json"))
    parser.add_argument("--dbs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--cache", default="")
    parser.add_argument("--rebuild-cache", action="store_true")
    parser.add_argument("--allow-dev", action="store_true")
    parser.add_argument("--config", choices=["gold_tables", "whole_db"], default="gold_tables")
    parser.add_argument("--cap", type=int, default=5000)
    parser.add_argument("--pool", type=int, default=100)
    parser.add_argument("--negative-pool", type=int, default=24)
    parser.add_argument("--proposer-model", default="")
    parser.add_argument("--question-vector-cache", default="")
    parser.add_argument("--profile-max-candidates", type=int, default=32)
    parser.add_argument("--profile-per-profile", type=int, default=4)
    parser.add_argument("--profile-generation-penalty", type=float, default=5.0)
    parser.add_argument("--profile-binding-quality-weight", type=float, default=2.0)
    parser.add_argument("--execution-timeout", type=float, default=1.0)
    parser.add_argument("--execution-row-limit", type=int, default=10000)
    parser.add_argument("--max-examples", type=int, default=0)
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--epochs", type=int, default=250)
    parser.add_argument("--model-type", choices=["tree", "linear"], default="tree")
    parser.add_argument("--estimators", type=int, default=120)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--min-samples-leaf", type=int, default=20)
    parser.add_argument("--learning-rate", type=float, default=0.03)
    parser.add_argument("--l2", type=float, default=0.002)
    parser.add_argument("--negatives", type=int, default=12)
    parser.add_argument("--holdout-promotion-calibration", action="store_true")
    args = parser.parse_args()
    if args.profile_max_candidates < 1 or args.profile_per_profile < 1:
        parser.error("profile candidate budgets must be positive")
    if args.profile_generation_penalty < 0 or args.profile_binding_quality_weight < 0:
        parser.error("profile weights must be nonnegative")
    if args.execution_timeout <= 0:
        parser.error("execution timeout must be positive")
    if args.execution_row_limit < 1:
        parser.error("execution row limit must be positive")

    train_paths = args.train or [
        os.path.join(data, "train_spider.json"),
    ]
    examples = _load_examples(train_paths, args.allow_dev)
    if args.max_examples:
        examples = examples[:args.max_examples]
    with open(args.tables, encoding="utf-8") as handle:
        metas = {meta["db_id"]: meta for meta in json.load(handle)}

    header = _cache_header(args, train_paths)
    generation_stats = {}
    if args.cache and os.path.exists(args.cache) and not args.rebuild_cache:
        print(f"loading candidate cache {args.cache}", flush=True)
        groups = load_cache(args.cache, header)
    else:
        proposal_model = None
        proposal_encoder = None
        question_vectors = ()
        if args.proposer_model:
            from engine.encoder_overlay import EncoderQuery
            from engine.sql_proposal import SQLProposalModel

            proposal_model = SQLProposalModel.load(args.proposer_model)
            proposal_encoder = EncoderQuery()
            print("encoding ranker-training questions for profile proposals...", flush=True)
            vector_cache = args.question_vector_cache or (
                args.cache + ".questions.npz" if args.cache else ""
            )
            question_vectors = load_or_encode_question_vectors(
                proposal_encoder,
                examples,
                _file_sha256(args.proposer_model),
                vector_cache,
            )
        print(f"generating candidates for {len(examples)} training examples", flush=True)
        groups, generation_stats = build_training_groups(
            examples, metas, args.dbs, args.config, args.cap, args.pool,
            args.negative_pool,
            proposal_model=proposal_model,
            proposal_encoder=proposal_encoder,
            question_vectors=question_vectors,
            profile_max_candidates=args.profile_max_candidates,
            profile_per_profile=args.profile_per_profile,
            profile_generation_penalty=args.profile_generation_penalty,
            profile_binding_quality_weight=args.profile_binding_quality_weight,
            execution_timeout=args.execution_timeout,
            execution_row_limit=args.execution_row_limit,
        )
        if args.cache:
            save_cache(args.cache, header, groups)

    generation_stats = {**summarize_groups(groups, len(examples)), **generation_stats}

    train_groups, validation_groups, validation_dbs = split_by_database(
        groups, args.validation_ratio, args.seed
    )
    baseline = {
        "train": ranking_metrics(train_groups),
        "validation": ranking_metrics(validation_groups),
    }
    print("baseline", json.dumps(baseline, sort_keys=True), flush=True)
    training_metadata = {
        "dataset": "Spider 1.0 train",
        "config": args.config,
        "pool": args.pool,
        "cap": args.cap,
        "proposer_model_sha256": (
            _file_sha256(args.proposer_model) if args.proposer_model else ""
        ),
        "profile_max_candidates": args.profile_max_candidates,
        "profile_per_profile": args.profile_per_profile,
        "profile_generation_penalty": args.profile_generation_penalty,
        "profile_binding_quality_weight": args.profile_binding_quality_weight,
        "execution_timeout": args.execution_timeout,
        "execution_row_limit": args.execution_row_limit,
        "seed": args.seed,
        "train_examples": len(train_groups),
        "validation_examples": len(validation_groups),
        "train_database_count": len({group["db_id"] for group in train_groups}),
        "validation_database_count": len(validation_dbs),
        "validation_database_sha256": hashlib.sha256(
            "\n".join(validation_dbs).encode("utf-8")
        ).hexdigest(),
        "source_sha256": hashlib.sha256(
            "".join(_file_sha256(path) for path in train_paths).encode("ascii")
        ).hexdigest(),
    }
    if args.model_type == "tree":
        model = fit_tree_ranker(
            train_groups,
            validation_groups,
            estimators=args.estimators,
            learning_rate=args.learning_rate,
            max_depth=args.max_depth,
            min_samples_leaf=args.min_samples_leaf,
            seed=args.seed,
            metadata=training_metadata,
            refit_on_validation=not args.holdout_promotion_calibration,
        )
    else:
        model = fit_pairwise_ranker(
            train_groups,
            validation_groups,
            epochs=args.epochs,
            learning_rate=args.learning_rate,
            l2=args.l2,
            negatives=args.negatives,
            metadata=training_metadata,
        )
    learned = {
        "artifact_train": ranking_metrics(train_groups, model),
        "artifact_validation": ranking_metrics(validation_groups, model),
    }
    if model.metadata.get("selection_metrics_before_refit"):
        learned["selection_before_refit"] = model.metadata["selection_metrics_before_refit"]
    model_metadata = dict(model.metadata)
    model_metadata.update({
        "generation": generation_stats,
        "baseline_metrics": baseline,
        "learned_metrics": learned,
    })
    if args.holdout_promotion_calibration:
        model_metadata["promotion_gate"] = calibrate_promotion_gate(
            validation_groups, model
        )
    model = model.with_metadata(model_metadata)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    model.save(args.out)
    print("learned", json.dumps(learned, sort_keys=True))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
