"""Train and calibrate the explicit Schema.org named-property head.

The Qwen encoder is frozen.  Only the URI-indexed linear property head is trained, so this
cannot regress SQL intent or the existing world router.  Run on a GPU for embedding speed;
the head itself is small and deterministic.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import subprocess
from collections import Counter
from pathlib import Path

import numpy as np

from engine.artifact_provenance import (
    canonical_json_sha256,
    semantic_encoder_fingerprint,
)
from engine.config import BASE_MODEL_ID, BASE_MODEL_REVISION, DATA_DIR
from engine.schema_decode import ClassDecoder
from engine.schema_org import load_contract
from training.schema_org.instances import group_id, read_jsonl
from training.schema_org.paths import (
    CORPUS_PATH,
    EMBEDDINGS_PATH,
    MANIFEST_PATH,
    experiment_dir,
)
from training.schema_org.signatures import SIGNATURES_NAME

CACHE_PATH = EMBEDDINGS_PATH
ROOT = Path(__file__).resolve().parents[2]
TRAINER_INPUTS = (
    "training/schema_org/train_property_head.py",
    "training/schema_org/instances.py",
    "training/schema_org/signatures.py",
    "training/schema_org/paths.py",
    "training/tools/run_schema_training.sh",
    "engine/schema_model.py",
    "engine/schema_decode.py",
    "engine/encoder.py",
    "engine/schema_org.py",
    "engine/artifact_provenance.py",
    "training/requirements.txt",
)
MIN_PROPERTY_TRAIN = 25
MIN_PROPERTY_VALIDATION = 10
# Held-out floors additionally counted in DISTINCT SPLIT GROUPS. Column instances add instances without
# adding groups, so an instance-count floor can be satisfied by clones of a single row group — the
# threshold would then certify statistical support that does not exist. Groups are independent
# observations; instances are not.
MIN_PROPERTY_VALIDATION_GROUPS = 10
MIN_PROPERTY_PRECISION = 0.95
MIN_PROPERTY_RECALL = 0.60
MIN_CLASS_PRECISION = 0.97
MIN_CLASS_RECALL = 0.70
MIN_CLASS_EVIDENCE_PROPERTIES = 2
MIN_CLASS_EVIDENCE_COVERAGE = 0.25
MIN_CLASS_TEST_PRECISION = 0.90
MIN_CLASS_TEST_RECALL = 0.60
MIN_CLASS_TEST_EVIDENCE_COVERAGE = 0.20


def _training_properties(property_order, property_support, property_groups) -> tuple[str, ...]:
    """Select dimensions from train/validation only; test remains untouched evidence."""
    return tuple(
        uri for uri in property_order
        if property_support["train"][uri] >= MIN_PROPERTY_TRAIN
        and property_support["validation"][uri] >= MIN_PROPERTY_VALIDATION
        and len(property_groups.get(uri, {}).get("validation", ()))
        >= MIN_PROPERTY_VALIDATION_GROUPS
    )


def _precision_threshold(scores: np.ndarray, labels: np.ndarray,
                         min_precision: float, *, margin: bool = True) -> tuple[float, bool]:
    """Choose the highest-recall validation threshold satisfying precision.

    Test labels must never be passed here: they are reserved for the final release gate.
    The grouped scan makes ties deterministic and avoids evaluating between score values.
    """
    positives = int(labels.sum())
    if positives == 0:
        return 1.0, False
    order = np.argsort(-scores, kind="stable")
    sorted_scores = scores[order]
    sorted_labels = labels[order].astype(np.int64)
    tp = 0
    fp = 0
    candidates = []
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and sorted_scores[end] == sorted_scores[start]:
            end += 1
        group = sorted_labels[start:end]
        tp += int(group.sum())
        fp += len(group) - int(group.sum())
        precision = tp / max(tp + fp, 1)
        recall = tp / positives
        if precision >= min_precision:
            # `lower` is the next DISTINCT score below this group. Everything strictly between the two
            # separates the identical set of instances, so the whole interval is free margin.
            lower = float(sorted_scores[end]) if end < len(order) else 0.0
            candidates.append((recall, precision, float(sorted_scores[start]), lower))
        start = end
    if not candidates:
        return float(np.nextafter(float(scores.max()), math.inf)), False
    recall, precision, chosen, lower = max(candidates, key=lambda item: (item[0], item[1], -item[2]))
    # Place the boundary in the MIDDLE of the separating interval, not at its upper edge.
    #
    # Pinning the threshold to the lowest positive score is what made the head brittle out of distribution.
    # Measured on the promoted artifact: 14 of 75 trained dims are perfectly separable on validation, and
    # every one had its threshold at min(positive) — `currentExchangeRate` separated with a gap of 0.876
    # (negatives topped out at 0.124, positives started at 0.999) and the threshold sat at 0.9994. An unseen
    # ECB table scoring 0.9986 — an emphatic firing, 8x further from the negatives than from the threshold —
    # fell below it and the class abstained. Every point in that interval yields the SAME true and false
    # positive counts on validation, so the midpoint is free: identical held-out metrics, maximal distance
    # from both classes. Where the classes are not separable the interval is narrow and this is a no-op.
    # `margin` applies only to independently decoded property thresholds. Class thresholds are calibrated
    # on a joint logistic score and are kept at an observed validation boundary; moving that boundary below
    # the selected score can admit a different combination of dimensions that validation never certified.
    threshold = (chosen + lower) / 2.0 if margin and lower < chosen else chosen
    return threshold, True


def _metrics(scores: np.ndarray, labels: np.ndarray, threshold: float) -> dict:
    predicted = scores >= threshold
    labels = labels.astype(bool)
    tp = int((predicted & labels).sum()); fp = int((predicted & ~labels).sum())
    fn = int((~predicted & labels).sum()); tn = int((~predicted & ~labels).sum())
    precision = tp / max(tp + fp, 1); recall = tp / max(tp + fn, 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-12)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(precision, 6), "recall": round(recall, 6),
            "f1": round(f1, 6)}


def _class_metrics(
    scores: np.ndarray, truth: np.ndarray, property_sets: list[frozenset[str]],
    signature: list[dict], threshold: float,
) -> tuple[dict, np.ndarray]:
    """Evaluate class inference only where its named property evidence is observable.

    Source identity tells us an entity's class even when a packed facet exposes only one broad property.
    That fragment is still useful property supervision, but it cannot establish a class superposition and
    must not be counted as a class false negative. Negatives always remain in scope. Coverage is reported
    and gated separately so a tiny, convenient subset cannot certify a class.
    """
    signature_properties = {item["property"] for item in signature}
    positive = truth.astype(bool)
    positive_count = int(positive.sum())
    evidenced_positive = np.array([
        bool(is_positive) and len(signature_properties & properties) >= MIN_CLASS_EVIDENCE_PROPERTIES
        for is_positive, properties in zip(positive, property_sets)
    ])
    scope = ~positive | evidenced_positive
    metrics = _metrics(scores[scope], truth[scope], threshold)
    eligible = int(evidenced_positive.sum())
    metrics.update({
        "all_positives": positive_count,
        "evidence_eligible_positives": eligible,
        "evidence_coverage": round(eligible / max(positive_count, 1), 6),
        "min_evidence_properties": MIN_CLASS_EVIDENCE_PROPERTIES,
    })
    return metrics, scope


def _fit_continuous_class_model(
    signature: list[dict], train_profiles: list[dict[str, float]],
    train_truth: np.ndarray, validation_profiles: list[dict[str, float]],
    validation_truth: np.ndarray, validation_property_sets: list[frozenset[str]],
) -> tuple[list[dict], float, float, bool, dict, float]:
    """Fit and calibrate one interpretable class superposition without test data.

    Every feature is an ontology-named property probability. Training fits signed
    contributions and a bias; validation chooses both regularization and the highest-
    recall threshold satisfying the class precision floor. No hidden feature enters
    the score, and untouched test labels are not accepted by this API.
    """
    from sklearn.linear_model import LogisticRegression

    ordered = sorted(copy.deepcopy(signature), key=lambda item: item["property"])
    if len(ordered) < 2:
        empty_scores = np.zeros(len(validation_truth))
        metrics, _scope = _class_metrics(
            empty_scores, validation_truth, validation_property_sets, ordered, 1.0,
        )
        return ordered, 0.0, 1.0, False, metrics, 0.0

    def matrix(profiles):
        return np.array([
            [float(profile.get(item["property"], 0.0)) for item in ordered]
            for profile in profiles
        ], dtype=np.float64)

    train_x = matrix(train_profiles)
    validation_x = matrix(validation_profiles)
    if not int(train_truth.sum()) or int(train_truth.sum()) == len(train_truth):
        metrics, _scope = _class_metrics(
            np.zeros(len(validation_truth)), validation_truth,
            validation_property_sets, ordered, 1.0,
        )
        return ordered, 0.0, 1.0, False, metrics, 0.0

    candidates = []
    for regularization in (0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1.0, 3.0,
                           10.0, 30.0, 100.0, 300.0, 1000.0):
        model = LogisticRegression(
            C=regularization, class_weight="balanced", solver="liblinear",
            random_state=19, max_iter=3000,
        )
        model.fit(train_x, train_truth)
        validation_scores = model.predict_proba(validation_x)[:, 1]
        provisional, scope = _class_metrics(
            validation_scores, validation_truth, validation_property_sets, ordered, 1.0,
        )
        threshold, feasible = _precision_threshold(
            validation_scores[scope], validation_truth[scope],
            MIN_CLASS_PRECISION,
        )
        metrics, _scope = _class_metrics(
            validation_scores, validation_truth, validation_property_sets, ordered, threshold,
        )
        assert provisional["evidence_coverage"] == metrics["evidence_coverage"]
        rank = (
            int(feasible), metrics["recall"] * metrics["evidence_coverage"],
            metrics["recall"], metrics["precision"], -regularization,
        )
        candidates.append((rank, regularization, model, threshold, feasible, metrics))

    _rank, regularization, model, threshold, feasible, metrics = max(
        candidates, key=lambda item: item[0]
    )
    for item, coefficient in zip(ordered, model.coef_[0]):
        item["ontology_weight"] = item["weight"]
        item["weight"] = round(float(coefficient), 8)
    return (
        ordered, round(float(model.intercept_[0]), 8), threshold, feasible, metrics,
        regularization,
    )


def _error_sources(scores: np.ndarray, labels: np.ndarray, threshold: float,
                   indices: np.ndarray, instances, scope: np.ndarray | None = None) -> dict:
    """Compact diagnostics; test errors are reported but never used for calibration."""
    predicted = scores >= threshold
    labels = labels.astype(bool)
    false_positive = Counter()
    false_negative = Counter()
    for local_index, corpus_index in enumerate(indices):
        if scope is not None and not scope[local_index]:
            continue
        source = instances[int(corpus_index)].source
        if predicted[local_index] and not labels[local_index]:
            false_positive[source] += 1
        elif labels[local_index] and not predicted[local_index]:
            false_negative[source] += 1
    return {
        "false_positive_sources": dict(sorted(false_positive.items())),
        "false_negative_sources": dict(sorted(false_negative.items())),
    }


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _trainer_identity() -> dict:
    """Bind a candidate to immutable Git blobs, independent of checkout line endings."""
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, encoding="utf-8",
    ).strip()
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True, encoding="utf-8",
    ).strip()
    files = {}
    for relative in TRAINER_INPUTS:
        content = subprocess.check_output(
            ["git", "show", f"{commit}:{relative}"], cwd=ROOT,
            stderr=subprocess.DEVNULL,
        )
        files[relative] = hashlib.sha256(content).hexdigest()
    return {
        "entrypoint": "training.schema_org.train_property_head",
        "repository_commit": commit,
        "worktree_clean": not status,
        "source_files": files,
        "source_files_sha256": canonical_json_sha256(files),
    }


def _runtime_identity(torch, device, runner_image: str) -> dict:
    cuda = torch.version.cuda
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "numpy": np.__version__,
        "scikit_learn": importlib.metadata.version("scikit-learn"),
        "torch": str(torch.__version__),
        "cuda_runtime": str(cuda) if cuda else None,
        "cudnn": torch.backends.cudnn.version() if cuda else None,
        "device": str(device),
        "device_name": torch.cuda.get_device_name(device) if device.type == "cuda" else "cpu",
        "runner_image": runner_image,
    }


def _cached_embeddings(cache_path: Path, encoder_artifact_sha256: str) -> dict[str, np.ndarray]:
    if not cache_path.exists():
        return {}
    cached = np.load(cache_path, allow_pickle=False)
    if (
        "encoder_artifact_sha256" not in cached.files
        or str(cached["encoder_artifact_sha256"].item()) != encoder_artifact_sha256
        or "text_hashes" not in cached.files
    ):
        return {}
    return dict(zip([str(h) for h in cached["text_hashes"]], cached["embeddings"]))


def _embeddings(instances, *, cache_path: Path, device, batch_size: int,
                encoder_artifact_sha256: str) -> np.ndarray:
    """Per-text cache bound to the exact encoder adapter that produced every embedding."""
    texts = [item.text for item in instances]
    hashes = [_text_hash(text) for text in texts]
    texts_hash = hashlib.sha256("\0".join(texts).encode("utf-8")).hexdigest()
    known = _cached_embeddings(cache_path, encoder_artifact_sha256)
    missing = [i for i, h in enumerate(hashes) if h not in known]
    if missing:
        from engine.encoder import LiveQwen
        print(f"encoding {len(missing)} new texts ({len(texts) - len(missing)} cached)", flush=True)
        encoder = LiveQwen(device, warm_lora=str(DATA_DIR / "qwen_lora"), serving=True)
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            encoded = encoder.encode([texts[i] for i in batch], max_len=128,
                                     grad=False, bs=batch_size).cpu().numpy()
            for i, row in zip(batch, encoded):
                known[hashes[i]] = row.astype(np.float32)
            print(f"embedded {min(start + batch_size, len(missing))}/{len(missing)}", flush=True)
    else:
        print(f"using cached embeddings {cache_path}", flush=True)
    embeddings = np.stack([known[h] for h in hashes]).astype(np.float32)
    if not missing and cache_path.exists():
        return embeddings          # nothing new to persist; recompressing ~170MB buys nothing
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    unique = dict(zip(hashes, embeddings))                          # one row per distinct text
    np.savez_compressed(cache_path, embeddings=np.stack(list(unique.values())),
                        text_hashes=np.array(list(unique.keys())),
                        texts_sha256=np.array(texts_hash),
                        encoder_artifact_sha256=np.array(encoder_artifact_sha256))
    return embeddings


def main() -> None:
    import torch

    from engine.schema_model import NamedPropertyHead

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(CORPUS_PATH))
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--signatures", default=None)
    parser.add_argument("--cache", default=str(CACHE_PATH))
    parser.add_argument("--epochs", type=int, default=160)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=2e-3)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--runner-image",
        default=os.environ.get("PREREASONER_TRAINING_IMAGE", "local"),
        help="immutable container image digest, or 'local' for a non-container run",
    )
    args = parser.parse_args()

    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    contract = load_contract()
    corpus_manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    # Candidates go to a disposable, corpus-keyed experiment directory. engine/data/ is the PROMOTED
    # bundle and is written only by training.schema_org.promote, after the gates pass.
    out_dir = experiment_dir(corpus_manifest["corpus"]["sha256"])
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "schema_property_head.pt"
    meta_path = out_dir / "schema_property_model.json"
    signatures_path = Path(args.signatures) if args.signatures else out_dir / SIGNATURES_NAME
    instances = list(read_jsonl(args.corpus))
    property_support = {split: Counter() for split in ("train", "validation", "test")}
    property_groups: dict[str, dict[str, set]] = {}
    for item in instances:
        property_support[item.split].update(item.properties)
        group = group_id(item.instance_id)
        for uri in item.properties:
            property_groups.setdefault(uri, {}).setdefault(item.split, set()).add(group)

    def _groups(uri, split):
        return len(property_groups.get(uri, {}).get(split, ()))

    properties = _training_properties(contract.property_order, property_support, property_groups)
    if not properties:
        raise ValueError("no Schema.org properties clear the support gates")
    prop_index = {uri: index for index, uri in enumerate(properties)}
    labels = np.zeros((len(instances), len(properties)), dtype=np.float32)
    for row, item in enumerate(instances):
        for uri in item.properties:
            index = prop_index.get(uri)
            if index is not None:
                labels[row, index] = 1.0

    encoder_artifact_sha256 = semantic_encoder_fingerprint(
        DATA_DIR, BASE_MODEL_ID, BASE_MODEL_REVISION
    )
    embeddings = _embeddings(
        instances, cache_path=Path(args.cache), device=device,
        batch_size=args.batch_size, encoder_artifact_sha256=encoder_artifact_sha256,
    )
    split_index = {
        split: np.array([i for i, item in enumerate(instances) if item.split == split], dtype=np.int64)
        for split in ("train", "validation", "test")
    }
    x = torch.from_numpy(embeddings).to(device)
    y = torch.from_numpy(labels).to(device)
    head = NamedPropertyHead(embeddings.shape[1], properties).module.to(device)
    train_idx = torch.from_numpy(split_index["train"]).to(device)
    validation_idx = torch.from_numpy(split_index["validation"]).to(device)
    positives = y[train_idx].sum(0)
    negatives = len(train_idx) - positives
    pos_weight = (negatives / positives.clamp(min=1)).clamp(max=30)
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    optimizer = torch.optim.AdamW(head.parameters(), lr=args.lr, weight_decay=1e-4)
    generator = torch.Generator(device="cpu").manual_seed(args.seed)
    best_loss = math.inf; best_state = None; stale = 0
    for epoch in range(args.epochs):
        head.train()
        order = train_idx[torch.randperm(len(train_idx), generator=generator, device="cpu").to(device)]
        for start in range(0, len(order), args.batch_size):
            batch = order[start:start + args.batch_size]
            loss = loss_fn(head(x[batch]), y[batch])
            optimizer.zero_grad(); loss.backward(); optimizer.step()
        head.eval()
        with torch.no_grad():
            validation_loss = float(loss_fn(head(x[validation_idx]), y[validation_idx]).item())
        if validation_loss < best_loss - 1e-5:
            best_loss = validation_loss
            best_state = copy.deepcopy(head.state_dict()); stale = 0
        else:
            stale += 1
        if epoch % 10 == 0 or epoch == args.epochs - 1:
            print(f"epoch={epoch:03d} validation_bce={validation_loss:.6f} best={best_loss:.6f}", flush=True)
        if stale >= 25:
            break
    assert best_state is not None
    head.load_state_dict(best_state); head.eval()
    with torch.no_grad():
        probabilities = torch.sigmoid(head(x)).cpu().numpy()

    thresholds = {}
    property_metrics = {}
    qualified_properties = set()
    for uri, index in prop_index.items():
        validation_scores = probabilities[split_index["validation"], index]
        validation_labels = labels[split_index["validation"], index]
        threshold, feasible = _precision_threshold(
            validation_scores, validation_labels, MIN_PROPERTY_PRECISION
        )
        validation_metrics = _metrics(validation_scores, validation_labels, threshold)
        qualified = (
            feasible
            and validation_metrics["precision"] >= MIN_PROPERTY_PRECISION
            and validation_metrics["recall"] >= MIN_PROPERTY_RECALL
        )
        if qualified:
            qualified_properties.add(uri)
        thresholds[uri] = threshold
        property_metrics[uri] = {
            "support": {split: property_support[split][uri]
                        for split in ("train", "validation", "test")},
            "group_support": {split: _groups(uri, split)
                              for split in ("train", "validation", "test")},
            "threshold": round(threshold, 8),
            "calibration_feasible": feasible,
            "qualified": qualified,
            "validation": validation_metrics,
            "test": _metrics(probabilities[split_index["test"], index],
                             labels[split_index["test"], index], threshold),
        }

    signatures = json.loads(signatures_path.read_text(encoding="utf-8"))
    by_uri = {row["uri"]: row for row in signatures["classes"]}
    class_metrics = {}
    profiles = [dict(zip(properties, row)) for row in probabilities]
    validation_profiles = [profiles[int(index)] for index in split_index["validation"]]
    train_profiles = [profiles[int(index)] for index in split_index["train"]]
    validation_property_sets = [
        frozenset(instances[int(index)].properties) for index in split_index["validation"]
    ]
    for uri, row in by_uri.items():
        if row["state"] != "data_ready_uncalibrated":
            continue
        row["raw_signature"] = row["signature"]
        row["signature"] = [item for item in row["signature"]
                            if item["property"] in prop_index]
        if len(row["signature"]) < 2:
            row["threshold"] = None
            row["servable"] = False
            row["state"] = "calibration_failed"
            class_metrics[uri] = {
                "threshold": None,
                "reason": "fewer_than_two_trained_properties",
                "trained_property_count": len(row["signature"]),
            }
            continue
        truth = np.array([1 if uri in item.classes else 0 for item in instances], dtype=np.int8)
        (
            row["signature"], row["bias"], threshold, feasible,
            validation_metrics, row["regularization_c"],
        ) = _fit_continuous_class_model(
            row["signature"], train_profiles, truth[split_index["train"]],
            validation_profiles, truth[split_index["validation"]],
            validation_property_sets,
        )
        row["score_model"] = "logistic_property_probability"
        scores = np.array([
            ClassDecoder.score_signature(
                profile, row["signature"], thresholds,
                bias=row["bias"], score_model=row["score_model"],
            )
            for profile in profiles
        ])
        test_scores = scores[split_index["test"]]
        test_truth = truth[split_index["test"]]
        test_property_sets = [
            frozenset(instances[int(index)].properties) for index in split_index["test"]
        ]
        test_metrics, test_scope = _class_metrics(
            test_scores, test_truth, test_property_sets, row["signature"], threshold,
        )
        test_metrics.update(_error_sources(
            test_scores, test_truth, threshold, split_index["test"], instances, test_scope,
        ))
        servable = (
            feasible
            and validation_metrics["precision"] >= MIN_CLASS_PRECISION
            and validation_metrics["recall"] >= MIN_CLASS_RECALL
            and validation_metrics["evidence_coverage"] >= MIN_CLASS_EVIDENCE_COVERAGE
        )
        row["threshold"] = round(threshold, 8)
        row["servable"] = servable
        row["state"] = "servable" if servable else "calibration_failed"
        class_metrics[uri] = {
            "threshold": round(threshold, 8),
            "calibration_feasible": feasible,
            "trained_property_count": len(row["signature"]),
            "regularization_c": row["regularization_c"],
            "validation": validation_metrics,
            "test": test_metrics,
        }
    signatures["schema_version"] = 2
    signatures.pop("artifact_sha256", None)
    signatures["property_model_pending"] = False
    signatures["artifact_sha256"] = canonical_json_sha256(signatures)
    signatures_path.write_text(json.dumps(signatures, sort_keys=True, ensure_ascii=True,
                                          separators=(",", ":")) + "\n", encoding="utf-8")

    torch.save(best_state, model_path)
    meta = {
        "schema_version": 1,
        "model_kind": "frozen-qwen-linear-named-property-head",
        "base_model": BASE_MODEL_ID,
        "base_model_revision": BASE_MODEL_REVISION,
        "encoder_artifact_sha256": encoder_artifact_sha256,
        "ontology_version": contract.version,
        "ontology_contract_sha256": contract.contract_sha256,
        "corpus_sha256": corpus_manifest["corpus"]["sha256"],
        "input_dim": int(embeddings.shape[1]),
        "all_property_count": len(contract.properties),
        "trained_properties": properties,
        "unsupported_properties": tuple(uri for uri in contract.property_order if uri not in prop_index),
        "thresholds": thresholds,
        "calibration_gates": {
            "property_min_precision": MIN_PROPERTY_PRECISION,
            "property_min_recall": MIN_PROPERTY_RECALL,
            "property_min_validation_groups": MIN_PROPERTY_VALIDATION_GROUPS,
            "class_min_precision": MIN_CLASS_PRECISION,
            "class_min_recall": MIN_CLASS_RECALL,
            "class_min_evidence_properties": MIN_CLASS_EVIDENCE_PROPERTIES,
            "class_min_evidence_coverage": MIN_CLASS_EVIDENCE_COVERAGE,
        },
        "release_gates": {
            "class_test_min_precision": MIN_CLASS_TEST_PRECISION,
            "class_test_min_recall": MIN_CLASS_TEST_RECALL,
            "class_test_min_evidence_coverage": MIN_CLASS_TEST_EVIDENCE_COVERAGE,
            "class_weight_model": "logistic_on_named_property_probabilities",
        },
        "property_metrics": property_metrics,
        "qualified_properties": tuple(uri for uri in properties if uri in qualified_properties),
        "class_metrics": class_metrics,
        "seed": args.seed,
    }
    state_bytes = model_path.read_bytes()
    meta["weights_sha256"] = hashlib.sha256(state_bytes).hexdigest()
    meta["artifact_sha256"] = canonical_json_sha256(meta)
    meta_path.write_text(json.dumps(meta, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")) + "\n", encoding="utf-8")
    artifact_paths = {
        "schema_property_head.pt": model_path,
        "schema_property_model.json": meta_path,
        "schema_class_signatures.json": signatures_path,
    }
    training_manifest = {
        "schema_version": 1,
        "generator": corpus_manifest.get("generator"),
        "trainer": _trainer_identity(),
        "runtime": _runtime_identity(torch, device, args.runner_image),
        "corpus": corpus_manifest["corpus"],
        "source_manifest": corpus_manifest.get("source_manifest"),
        "split_policy": corpus_manifest.get("split_policy"),
        "model": {
            "base_model": BASE_MODEL_ID,
            "base_model_revision": BASE_MODEL_REVISION,
            "encoder_artifact_sha256": encoder_artifact_sha256,
            "ontology_version": contract.version,
            "ontology_contract_sha256": contract.contract_sha256,
        },
        "training": {
            "seed": args.seed,
            "epochs_requested": args.epochs,
            "epochs_completed": epoch + 1,
            "batch_size": args.batch_size,
            "learning_rate": args.lr,
            "best_validation_bce": best_loss,
            "selection_data": ("train", "validation"),
            "evaluation_data": ("test",),
        },
        "metrics": {"properties": property_metrics, "classes": class_metrics},
        "artifacts": {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in artifact_paths.items()
        },
    }
    training_manifest["artifact_sha256"] = canonical_json_sha256(training_manifest)
    (out_dir / "schema_training_manifest.json").write_text(
        json.dumps(training_manifest, sort_keys=True, ensure_ascii=True,
                   separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    passed_properties = len(qualified_properties)
    servable_classes = sum(row["servable"] for row in by_uri.values())
    print(
        f"named property head: {len(properties)}/{len(contract.properties)} trained; "
        f"{passed_properties} properties validation-qualified; {servable_classes} classes servable\n"
        f"candidate written to {out_dir}\n"
        f"promote with: python -m training.schema_org.promote {out_dir.name}",
        flush=True,
    )


if __name__ == "__main__":
    main()
