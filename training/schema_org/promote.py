"""Promote a trained Schema.org candidate into the runtime bundle — the ONE writer of engine/data/.

Training writes only to `training/schema_org/data/experiments/<corpus>/`, which is disposable and
gitignored. This module is the separate, explicit, gated step that installs a candidate, so that:

  * a candidate cannot reach serving by being written, only by passing the gates below;
  * the artifacts move together — the head, its calibrated thresholds, and the class signatures are
    fitted to ONE corpus, and installing a partial set produces a pair that loads without complaint and
    then behaves incoherently. (Writing signatures straight into the bundle out of order with training
    is exactly how every class silently became unservable in serving.)

    python -m training.schema_org.promote                 # list candidates
    python -m training.schema_org.promote <corpus-prefix> --revision <immutable-commit>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from engine.artifact_provenance import (
    canonical_json_sha256, semantic_encoder_fingerprint, sha256_file, validate_weight_bundle,
)
from engine.config import BASE_MODEL_ID, BASE_MODEL_REVISION, DATA_DIR as RUNTIME_DIR
from engine.schema_org import load_contract
from training.schema_org.paths import EXPERIMENTS_DIR, MANIFEST_PATH

ARTIFACTS = ("schema_property_head.pt", "schema_property_model.json",
             "schema_class_signatures.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_json_identity(value: dict) -> bool:
    recorded = value.get("artifact_sha256")
    payload = dict(value)
    payload.pop("artifact_sha256", None)
    return bool(recorded) and recorded == canonical_json_sha256(payload)


def gate(candidate: Path) -> list[str]:
    """Every reason this candidate must not be promoted. Empty list == promotable."""
    problems = []
    for name in ARTIFACTS:
        if not (candidate / name).exists():
            problems.append(f"missing artifact: {name}")
    if problems:
        return problems
    meta = json.loads((candidate / "schema_property_model.json").read_text(encoding="utf-8"))
    signatures = json.loads((candidate / "schema_class_signatures.json").read_text(encoding="utf-8"))
    contract = load_contract()
    corpus_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    if meta.get("ontology_contract_sha256") != contract.contract_sha256:
        problems.append("property model was fitted against a different ontology contract")
    if signatures.get("ontology_contract_sha256") != contract.contract_sha256:
        problems.append("class signatures were built against a different ontology contract")
    if meta.get("corpus_sha256") != signatures.get("corpus_sha256"):
        problems.append(
            f"artifacts describe different corpora: model {str(meta.get('corpus_sha256'))[:12]} "
            f"vs signatures {str(signatures.get('corpus_sha256'))[:12]}"
        )
    if meta.get("corpus_sha256") != corpus_manifest.get("corpus", {}).get("sha256"):
        problems.append("candidate does not match the committed semantic corpus manifest")
    if meta.get("base_model") != BASE_MODEL_ID or meta.get("base_model_revision") != BASE_MODEL_REVISION:
        problems.append("candidate base model identity differs from the pinned training/serving identity")
    expected_encoder = semantic_encoder_fingerprint(
        RUNTIME_DIR, BASE_MODEL_ID, BASE_MODEL_REVISION
    )
    if meta.get("encoder_artifact_sha256") != expected_encoder:
        problems.append(
            "property head was not trained with the currently promoted encoder adapter"
        )
    if signatures.get("property_model_pending", True):
        problems.append("class signatures were never calibrated (property_model_pending is set)")
    if _sha256(candidate / "schema_property_head.pt") != meta.get("weights_sha256"):
        problems.append("weights do not match the hash recorded in the property model meta")
    if not _valid_json_identity(meta):
        problems.append("property model metadata artifact hash is invalid")
    if not _valid_json_identity(signatures):
        problems.append("class signature artifact hash is invalid")
    if not sum(1 for row in signatures["classes"] if row.get("servable")):
        problems.append("no class is servable — promoting this would abstain on every table")
    if not meta.get("trained_properties"):
        problems.append("no property dimensions were trained")
    gates = meta.get("calibration_gates") or {}
    qualified = set(meta.get("qualified_properties") or ())
    metrics = meta.get("property_metrics") or {}
    for uri in qualified:
        row = metrics.get(uri) or {}
        validation = row.get("validation") or {}
        groups = row.get("group_support") or {}
        if (
            not row.get("qualified")
            or validation.get("precision", 0) < gates.get("property_min_precision", 1)
            or validation.get("recall", 0) < gates.get("property_min_recall", 1)
            or groups.get("validation", 0) < gates.get("property_min_validation_groups", 1)
        ):
            problems.append(f"qualified property does not satisfy recorded gates: {uri}")
    class_metrics = meta.get("class_metrics") or {}
    for row in signatures.get("classes", ()):
        if not row.get("servable"):
            continue
        validation = (class_metrics.get(row.get("uri")) or {}).get("validation") or {}
        if (
            validation.get("precision", 0) < gates.get("class_min_precision", 1)
            or validation.get("recall", 0) < gates.get("class_min_recall", 1)
        ):
            problems.append(f"servable class does not satisfy recorded gates: {row.get('uri')}")
    return problems


def _atomic_copy(source: Path, destination: Path) -> None:
    with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", dir=destination.parent,
                                     delete=False) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_bytes(source.read_bytes())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=f".{path.name}.",
                                     dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def promote(candidate: Path, *, revision: str | None, local_only: bool) -> None:
    if bool(revision) == local_only:
        raise ValueError("provide exactly one immutable revision or local_only=True")
    problems = gate(candidate)
    if problems:
        raise SystemExit("candidate rejected:\n  " + "\n  ".join(problems))
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = RUNTIME_DIR / "weights_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("runtime weights manifest is invalid")
    manifest["files"]["schema_property_head.pt"] = sha256_file(
        candidate / "schema_property_head.pt"
    )
    for name in ("schema_property_model.json", "schema_class_signatures.json"):
        if name not in manifest.get("committed_artifacts", {}):
            raise ValueError(f"manifest does not own committed artifact {name}")
        manifest["committed_artifacts"][name]["sha256"] = sha256_file(candidate / name)
    manifest["revision"] = revision
    if local_only:
        manifest["unpublished_local"] = True
    else:
        manifest.pop("unpublished_local", None)
    for name in ARTIFACTS:
        _atomic_copy(candidate / name, RUNTIME_DIR / name)
    _atomic_json(manifest_path, manifest)
    validate_weight_bundle(RUNTIME_DIR, manifest)
    meta = json.loads((RUNTIME_DIR / "schema_property_model.json").read_text(encoding="utf-8"))
    print(f"promoted {candidate.name}: corpus {meta['corpus_sha256'][:12]}, "
          f"{len(meta['trained_properties'])} trained property dims, "
          f"weights {meta['weights_sha256'][:12]}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", nargs="?", help="experiment directory name (corpus sha prefix)")
    release = parser.add_mutually_exclusive_group()
    release.add_argument("--revision", help="immutable published bundle revision")
    release.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    if not args.candidate:
        if not EXPERIMENTS_DIR.exists():
            print("no candidates yet; run training.schema_org.train_property_head first")
            return 0
        for entry in sorted(EXPERIMENTS_DIR.iterdir()):
            if entry.is_dir():
                problems = gate(entry)
                print(f"  {entry.name}  {'PROMOTABLE' if not problems else 'blocked: ' + problems[0]}")
        return 0
    candidate = Path(args.candidate)
    if not candidate.exists():
        candidate = EXPERIMENTS_DIR / args.candidate
    if not candidate.exists():
        raise SystemExit(f"no such candidate: {args.candidate}")
    if not args.revision and not args.local_only:
        raise SystemExit("promotion requires --revision or --local-only")
    promote(candidate, revision=args.revision, local_only=args.local_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
