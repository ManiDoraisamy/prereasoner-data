"""Build ontology-constrained class signatures and honest support states."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from engine.artifact_provenance import canonical_json_sha256
from engine.schema_org import load_contract
from training.schema_org.instances import read_jsonl
from training.schema_org.paths import CORPUS_PATH, MANIFEST_PATH, experiment_dir

SIGNATURES_NAME = "schema_class_signatures.json"
MIN_TRAIN = 25
MIN_VALIDATION = 10
MIN_PROPERTY_COUNT = 5
MIN_PROPERTY_FREQUENCY = 0.10
MIN_LIFT = 1.25


def _class_data_ready(n_train: int, n_validation: int, candidates, real_sources) -> bool:
    """Determine calibration eligibility without observing the test split."""
    return (
        n_train >= MIN_TRAIN
        and n_validation >= MIN_VALIDATION
        and len(candidates) >= 2
        and bool(real_sources)
    )


def _real_selection_sources(source_counts) -> list[str]:
    """Return non-template sources observed before the untouched test split."""
    return sorted(
        source for source, splits in source_counts.items()
        if source != "product_templates"
        and (splits.get("train", 0) or splits.get("validation", 0))
    )


def build(*, corpus_path: str | Path = CORPUS_PATH,
          manifest_path: str | Path = MANIFEST_PATH,
          output_path: str | Path | None = None) -> dict:
    contract = load_contract()
    instances = list(read_jsonl(corpus_path))
    split_counts = defaultdict(Counter)
    source_counts = defaultdict(Counter)
    source_split_counts = defaultdict(lambda: defaultdict(Counter))
    class_property = defaultdict(Counter)
    global_property = Counter()
    train_instances = 0
    for item in instances:
        for class_uri in item.classes:
            split_counts[class_uri][item.split] += 1
            source_counts[class_uri][item.source] += 1
            source_split_counts[class_uri][item.source][item.split] += 1
            if item.split == "train":
                class_property[class_uri].update(item.properties)
        if item.split == "train":
            global_property.update(item.properties)
            train_instances += 1

    classes = []
    for uri in contract.class_order:
        schema_class = contract.classes[uri]
        support = split_counts[uri]
        n_train = support["train"]
        compatible = set(schema_class.compatible_properties)
        candidates = []
        for prop, count in class_property[uri].items():
            if prop not in compatible or count < MIN_PROPERTY_COUNT or not n_train:
                continue
            frequency = count / n_train
            global_frequency = global_property[prop] / max(train_instances, 1)
            lift = frequency / max(global_frequency, 1 / max(train_instances, 1))
            if frequency >= MIN_PROPERTY_FREQUENCY and lift >= MIN_LIFT:
                candidates.append({
                    "property": prop, "frequency": round(frequency, 6),
                    "lift": round(lift, 6),
                    "weight": round(math.log1p(lift) * frequency, 6),
                    "count": count,
                })
        candidates.sort(key=lambda row: (-row["weight"], row["property"]))
        real_sources = _real_selection_sources(source_split_counts[uri])
        # Test is report-only. It must not decide which classes reach calibration.
        data_ready = _class_data_ready(
            n_train, support["validation"], candidates, real_sources,
        )
        classes.append({
            "uri": uri, "name": schema_class.name,
            "parents": schema_class.parents, "ancestors": schema_class.ancestors,
            "support": {split: support[split] for split in ("train", "validation", "test")},
            "sources": dict(sorted(source_counts[uri].items())),
            "real_sources": real_sources,
            "signature": candidates,
            "state": (
                "data_ready_uncalibrated" if data_ready else
                "observed_insufficient" if sum(support.values()) else "representable_unobserved"
            ),
            "servable": False,
            "threshold": None,
        })
    corpus_manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if output_path is None:                    # candidates are keyed by the corpus they were fitted to
        output_path = experiment_dir(corpus_manifest["corpus"]["sha256"]) / SIGNATURES_NAME
    artifact = {
        "schema_version": 1,
        # Explicit: nothing here is servable until train_property_head calibrates per-class thresholds and
        # clears this flag. ClassDecoder refuses to load a pending artifact rather than abstaining on
        # everything, which would be indistinguishable from "no upload matched".
        "property_model_pending": True,
        "ontology_version": contract.version,
        "ontology_contract_sha256": contract.contract_sha256,
        "corpus_sha256": corpus_manifest["corpus"]["sha256"],
        "gates": {
            "min_train": MIN_TRAIN, "min_validation": MIN_VALIDATION,
            "min_property_count": MIN_PROPERTY_COUNT,
            "min_property_frequency": MIN_PROPERTY_FREQUENCY, "min_lift": MIN_LIFT,
        },
        "classes": classes,
    }
    artifact["artifact_sha256"] = canonical_json_sha256(artifact)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, sort_keys=True, ensure_ascii=True,
                                      separators=(",", ":")) + "\n", encoding="utf-8")
    states = Counter(row["state"] for row in classes)
    print(f"class signatures: {dict(sorted(states.items()))} -> {output_path}", flush=True)
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(CORPUS_PATH))
    parser.add_argument("--manifest", default=str(MANIFEST_PATH))
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    build(corpus_path=args.corpus, manifest_path=args.manifest, output_path=args.output)


if __name__ == "__main__":
    main()
