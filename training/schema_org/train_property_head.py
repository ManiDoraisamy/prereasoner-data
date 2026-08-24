"""Train and calibrate the explicit Schema.org named-property head.

The Qwen encoder is frozen.  Only the URI-indexed linear property head is trained, so this
cannot regress SQL intent or the existing world router.  Run on a GPU for embedding speed;
the head itself is small and deterministic.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path
import random

import numpy as np
import torch

from engine.config import BASE_MODEL_ID, BASE_MODEL_REVISION, DATA_DIR
from engine.encoder import LiveQwen
from engine.schema_decode import ClassDecoder
from engine.schema_model import NamedPropertyHead
from engine.artifact_provenance import canonical_json_sha256, semantic_encoder_fingerprint
from engine.schema_org import load_contract
from training.schema_org.instances import group_id, read_jsonl
from training.schema_org.paths import (
    CORPUS_PATH, EMBEDDINGS_PATH, MANIFEST_PATH, experiment_dir,
)
from training.schema_org.signatures import SIGNATURES_NAME


CACHE_PATH = EMBEDDINGS_PATH
MIN_PROPERTY_TRAIN = 25
MIN_PROPERTY_VALIDATION = 5
MIN_PROPERTY_TEST = 5
# Held-out floors additionally counted in DISTINCT SPLIT GROUPS. Column instances add instances without
# adding groups, so an instance-count floor can be satisfied by clones of a single row group — the
# threshold would then certify statistical support that does not exist. Groups are independent
# observations; instances are not.
MIN_PROPERTY_VALIDATION_GROUPS = 5
MIN_PROPERTY_TEST_GROUPS = 5
MIN_PROPERTY_PRECISION = 0.90
MIN_PROPERTY_RECALL = 0.60
MIN_CLASS_PRECISION = 0.90
MIN_CLASS_RECALL = 0.60


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
    # `margin` applies ONLY to per-property thresholds, which are compared against raw model scores and so
    # must tolerate distribution shift. CLASS thresholds are compared against a weighted fraction of
    # ALREADY-thresholded property firing — a discrete quantity over a handful of signature properties —
    # and widening there does not buy robustness, it lowers the evidentiary bar. Measured: with margin
    # applied, ExchangeRateSpecification's threshold fell to 0.289, under the 0.45 that `currency` alone
    # scores, so the class would have decoded from a currency column with no exchange rate present. The
    # necessity check in tests/test_schema_decode.py caught it.
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


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(CORPUS_PATH))
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--signatures", default=None)
    parser.add_argument("--cache", default=str(CACHE_PATH))
    parser.add_argument("--epochs", type=int, default=160)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=2e-3)
    parser.add_argument("--seed", type=int, default=17)
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

    properties = tuple(uri for uri in contract.property_order
                       if property_support["train"][uri] >= MIN_PROPERTY_TRAIN
                       and property_support["validation"][uri] >= MIN_PROPERTY_VALIDATION
                       and property_support["test"][uri] >= MIN_PROPERTY_TEST
                       and _groups(uri, "validation") >= MIN_PROPERTY_VALIDATION_GROUPS
                       and _groups(uri, "test") >= MIN_PROPERTY_TEST_GROUPS)
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
    for uri, row in by_uri.items():
        if row["state"] != "data_ready_uncalibrated":
            continue
        row["raw_signature"] = row["signature"]
        row["signature"] = [item for item in row["signature"]
                            if item["property"] in qualified_properties]
        if len(row["signature"]) < 2:
            row["threshold"] = None
            row["servable"] = False
            row["state"] = "calibration_failed"
            class_metrics[uri] = {
                "threshold": None,
                "reason": "fewer_than_two_validation_qualified_properties",
                "qualified_property_count": len(row["signature"]),
            }
            continue
        scores = np.array([ClassDecoder.score_signature(profile, row["signature"], thresholds) for profile in profiles])
        truth = np.array([1 if uri in item.classes else 0 for item in instances], dtype=np.int8)
        threshold, feasible = _precision_threshold(
            scores[split_index["validation"]], truth[split_index["validation"]],
            MIN_CLASS_PRECISION, margin=False,
        )
        validation_metrics = _metrics(
            scores[split_index["validation"]], truth[split_index["validation"]], threshold
        )
        test_metrics = _metrics(scores[split_index["test"]], truth[split_index["test"]], threshold)
        servable = (
            feasible
            and validation_metrics["precision"] >= MIN_CLASS_PRECISION
            and validation_metrics["recall"] >= MIN_CLASS_RECALL
        )
        row["threshold"] = round(threshold, 8)
        row["servable"] = servable
        row["state"] = "servable" if servable else "calibration_failed"
        class_metrics[uri] = {
            "threshold": round(threshold, 8),
            "calibration_feasible": feasible,
            "qualified_property_count": len(row["signature"]),
            "validation": validation_metrics,
            "test": test_metrics,
        }
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
            "property_min_test_groups": MIN_PROPERTY_TEST_GROUPS,
            "class_min_precision": MIN_CLASS_PRECISION,
            "class_min_recall": MIN_CLASS_RECALL,
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
