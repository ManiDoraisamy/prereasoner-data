"""Content fingerprints and validation for shipped model artifacts."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

WEIGHTS_MANIFEST = "weights_manifest.json"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(path: str | Path) -> str:
    root = Path(path)
    if not root.exists():
        return "missing"
    if root.is_file():
        return sha256_file(root)
    digest = hashlib.sha256()
    for directory, directories, names in os.walk(root):
        directories.sort()
        for name in sorted(names):
            item = Path(directory) / name
            relative = item.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(sha256_file(item).encode("ascii"))
    return digest.hexdigest()


def semantic_encoder_fingerprint(
    data_dir: str | Path, base_model_id: str, base_model_revision: str
) -> str:
    """Identity of the encoder inputs a separately trained head depends on."""
    return canonical_json_sha256({
        "base_model_id": base_model_id,
        "base_model_revision": base_model_revision,
        "qwen_lora_sha256": sha256_tree(Path(data_dir) / "qwen_lora"),
    })


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def json_artifact_bytes(value: Any, *, indent: int | None = None) -> bytes:
    """Serialize a JSON artifact identically on every operating system."""
    options: dict[str, Any] = {
        "allow_nan": False,
        "ensure_ascii": True,
        "sort_keys": True,
    }
    if indent is None:
        options["separators"] = (",", ":")
    else:
        options["indent"] = indent
    return (json.dumps(value, **options) + "\n").encode("utf-8")


def write_json_artifact(path: str | Path, value: Any, *, indent: int | None = None) -> None:
    """Write canonical UTF-8 JSON without platform newline translation."""
    Path(path).write_bytes(json_artifact_bytes(value, indent=indent))


def load_weights_manifest(data_dir: str | Path) -> dict[str, Any] | None:
    path = Path(data_dir) / WEIGHTS_MANIFEST
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), Mapping):
        raise RuntimeError(f"invalid model weights manifest: {path}")
    return manifest


def validate_weight_bundle(
    data_dir: str | Path,
    manifest: Mapping[str, Any] | None = None,
) -> str | None:
    """Validate every manifested file and return the immutable bundle fingerprint."""
    root = Path(data_dir)
    manifest = dict(manifest or load_weights_manifest(root) or {})
    if not manifest:
        return None
    failures = []
    expected_files = dict(manifest["files"])
    for relative, record in manifest.get("committed_artifacts", {}).items():
        if not isinstance(record, Mapping) or not isinstance(record.get("sha256"), str):
            failures.append(f"{relative}: invalid committed-artifact record")
            continue
        expected_files[relative] = record["sha256"]
    for relative, expected in sorted(expected_files.items()):
        path = root / relative
        if not path.is_file():
            failures.append(f"{relative}: missing")
            continue
        actual = sha256_file(path)
        if actual != expected:
            failures.append(f"{relative}: expected {expected[:12]}, got {actual[:12]}")
    if failures:
        raise RuntimeError(
            "model weight bundle does not match weights_manifest.json: "
            + "; ".join(failures)
        )
    return canonical_json_sha256(manifest)


def fingerprint_paths(paths: Mapping[str, str | Path | None]) -> dict[str, str | None]:
    return {
        name: sha256_file(path) if path and Path(path).is_file() else None
        for name, path in sorted(paths.items())
    }
