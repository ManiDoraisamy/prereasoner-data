"""Create an immutable, allowlisted Cloud Build context from a clean Git HEAD."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SOURCE_ALLOWLIST = (
    ".dockerignore",
    "Dockerfile",
    "Dockerfile.orchestrator",
    "LICENSE",
    "THIRD_PARTY.md",
    "cloudbuild.yaml",
    "requirements.lock.txt",
    "engine",
    "db",
    "regress",
    "mcp_server",
    "orchestrator",
)
SOURCE_SYNC_ALLOWLIST = (
    ".dockerignore",
    "Dockerfile.sync",
    "LICENSE",
    "THIRD_PARTY.md",
    "cloudbuild.sync.yaml",
    "db",
    "engine/__init__.py",
    "engine/enrichment/__init__.py",
    "engine/enrichment/registry.py",
)

from engine.artifact_provenance import (  # noqa: E402
    load_weights_manifest,
    validate_weight_bundle,
)


def _git(*args: str) -> str:
    return subprocess.check_output(("git", "-C", str(ROOT), *args), text=True).strip()


def require_clean_head() -> str:
    dirty = _git("status", "--porcelain", "--untracked-files=all")
    if dirty:
        paths = ", ".join(line[3:] for line in dirty.splitlines()[:8])
        raise RuntimeError(f"release builds require a clean worktree; changed paths: {paths}")
    return _git("rev-parse", "HEAD")


def create_context(output: Path, target: str = "engine") -> tuple[str, str]:
    commit = require_clean_head()
    if target not in {"engine", "sync"}:
        raise ValueError(f"unknown build target: {target}")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"build context must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    allowlist = SOURCE_ALLOWLIST if target == "engine" else SOURCE_SYNC_ALLOWLIST
    archive = subprocess.check_output((
        "git", "-C", str(ROOT), "archive", "--format=tar", commit,
        "--", *allowlist,
    ))
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as bundle:
        for member in bundle.getmembers():
            if not (member.isfile() or member.isdir()):
                raise RuntimeError(f"unsupported archive member: {member.name}")
            destination = (output / member.name).resolve()
            if output.resolve() not in destination.parents and destination != output.resolve():
                raise RuntimeError(f"unsafe archive member: {member.name}")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError(f"archive file has no content: {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            destination.chmod(member.mode & 0o777)

    if target == "engine":
        data = ROOT / "engine" / "data"
        manifest = load_weights_manifest(data)
        if manifest is None:
            raise RuntimeError("engine/data/weights_manifest.json is required")
        fingerprint = validate_weight_bundle(data, manifest)
        for relative in manifest["files"]:
            source = data / relative
            destination = output / "engine" / "data" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        validate_weight_bundle(output / "engine" / "data", manifest)
        provenance = output / "engine" / "data" / "build_provenance.json"
    else:
        fingerprint = "source-only"
        provenance = output / "db" / "sync" / "build_provenance.json"
    provenance.write_text(json.dumps({
        "build_target": target,
        "source_commit": commit,
        "weights_manifest_sha256": fingerprint,
    }, sort_keys=True, indent=2) + "\n", encoding="ascii")
    return commit, fingerprint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", choices=("engine", "sync"), default="engine")
    args = parser.parse_args()
    commit, fingerprint = create_context(args.output, args.target)
    print(f"build context ready: target={args.target} commit={commit} weights={fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
