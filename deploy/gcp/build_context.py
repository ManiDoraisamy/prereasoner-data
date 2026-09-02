"""Create an immutable Cloud Build context from HEAD plus manifested weights."""
from __future__ import annotations

import argparse
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SOURCE_ALLOWLIST = (
    ".dockerignore",
    "Dockerfile",
    "Dockerfile.orchestrator",
    "LICENSE",
    "THIRD_PARTY.md",
    "cloudbuild.yaml",
    "requirements.txt",
    "engine",
    "db",
    "regress",
    "mcp_server",
    "orchestrator",
)

from engine.artifact_provenance import load_weights_manifest, validate_weight_bundle  # noqa: E402


def _git(*args: str) -> str:
    return subprocess.check_output(("git", "-C", str(ROOT), *args), text=True).strip()


def require_clean_head() -> str:
    dirty = _git("status", "--porcelain", "--untracked-files=all")
    if dirty:
        paths = ", ".join(line[3:] for line in dirty.splitlines()[:8])
        raise RuntimeError(f"release builds require a clean worktree; changed paths: {paths}")
    return _git("rev-parse", "HEAD")


def create_context(output: Path) -> tuple[str, str]:
    commit = require_clean_head()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"build context must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    archive = subprocess.check_output((
        "git", "-C", str(ROOT), "archive", "--format=tar", commit,
        "--", *SOURCE_ALLOWLIST,
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
    (output / "engine" / "data" / "build_provenance.json").write_text(json.dumps({
        "source_commit": commit,
        "weights_manifest_sha256": fingerprint,
    }, sort_keys=True, indent=2) + "\n", encoding="ascii")
    return commit, fingerprint


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    commit, fingerprint = create_context(args.output)
    print(f"build context ready: commit={commit} weights={fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
