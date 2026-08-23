"""Atomically install one gated property/intent/calculation checkpoint.

The large files remain external to git. A published promotion pins the immutable Hugging Face
revision that contains exactly these hashes. ``--local-only`` is useful for final evaluation, but
marks the manifest so a fresh clone cannot mistake the local candidate for a fetchable release.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from engine.artifact_provenance import sha256_file, validate_weight_bundle


REPO = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = Path(__file__).resolve().parent / "data"
DEFAULT_DESTINATION = REPO / "engine" / "data"
FILES = {
    "encoder_props.pt": "encoder.pt",
    "encoder_props_meta.pt": "encoder_meta.pt",
    "qwen_lora_props/adapter_config.json": "qwen_lora/adapter_config.json",
    "qwen_lora_props/adapter_model.safetensors": "qwen_lora/adapter_model.safetensors",
    "props_thr.json": "props_thr.json",
}


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", dir=destination.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f".{path.name}.", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def promote(source: Path, destination: Path, *, revision: str | None, local_only: bool) -> dict:
    if bool(revision) == local_only:
        raise ValueError("provide exactly one of an immutable revision or local_only=True")
    missing = [relative for relative in FILES if not (source / relative).is_file()]
    if missing:
        raise FileNotFoundError(f"checkpoint is incomplete: {missing}")
    thresholds = json.loads((source / "props_thr.json").read_text(encoding="utf-8"))
    if not isinstance(thresholds, dict) or not thresholds:
        raise ValueError("props_thr.json must contain calibrated property thresholds")
    manifest_path = destination / "weights_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("destination weights_manifest.json is invalid")

    for source_name, destination_name in FILES.items():
        _atomic_copy(source / source_name, destination / destination_name)
    for destination_name in FILES.values():
        if destination_name in manifest["files"]:
            manifest["files"][destination_name] = sha256_file(destination / destination_name)
    manifest["revision"] = revision
    if local_only:
        manifest["unpublished_local"] = True
    else:
        manifest.pop("unpublished_local", None)
    manifest["training_corpora"] = {
        name: sha256_file(source / name)
        for name in (
            "calculation_contrastive_eval.jsonl",
            "calculation_contrastive_train.jsonl",
            "intent_aug_train.jsonl",
            "intent_calibration.jsonl",
            "intent_eval.jsonl",
        )
        if (source / name).is_file()
    }
    _atomic_json(manifest_path, manifest)
    validate_weight_bundle(destination, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    release = parser.add_mutually_exclusive_group(required=True)
    release.add_argument("--revision", help="immutable published Hugging Face commit")
    release.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    manifest = promote(
        args.source.resolve(),
        args.destination.resolve(),
        revision=args.revision,
        local_only=args.local_only,
    )
    state = "local-only candidate" if args.local_only else f"published revision {args.revision}"
    print(f"promoted {state}; bundle files verified ({len(manifest['files'])} hashes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
