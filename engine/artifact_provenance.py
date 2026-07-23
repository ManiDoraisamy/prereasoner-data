"""Content fingerprints and validation for shipped model artifacts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


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


def canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    for relative, expected in sorted(manifest["files"].items()):
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
