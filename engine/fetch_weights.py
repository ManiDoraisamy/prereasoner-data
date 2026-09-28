"""Fetch and verify the exact gitignored model artifacts named by weights_manifest.json.

The manifest's ``repository``, immutable 40-character ``revision``, and ``files`` map are the
single source of truth. The fetcher refuses mutable revisions, source overrides, unsafe paths,
and any download whose SHA-256 differs from the manifest. Small committed runtime artifacts are
staged from the checkout and verified in the same bundle before downloaded files are installed.

Usage:
    python -m engine.fetch_weights                 # verify or fetch the pinned bundle
    python -m engine.fetch_weights --force         # fetch again from that same pinned revision

To publish a new model bundle, first produce and review a complete weights manifest and upload
exactly its ``files`` entries to the repository at the recorded immutable revision. Do not use a
wildcard upload as the release contract: an unmanifested file is not part of a reproducible bundle.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from engine.artifact_provenance import (
    load_weights_manifest,
    resolve_artifact_path,
    validate_weight_bundle,
)

DATA_DIR = Path(os.environ.get("PREREASONER_DATA_DIR") or Path(__file__).resolve().parent / "data")

# The public Hugging Face repo holding the weights. Override with PREREASONER_WEIGHTS_REPO;
# HF_TOKEN is optional and only needed when an operator selects a private replacement.
_MANIFEST = load_weights_manifest(DATA_DIR)
DEFAULT_REPO = os.environ.get(
    "PREREASONER_WEIGHTS_REPO",
    (_MANIFEST or {}).get("repository", "prereasoner/prereasoner-weights"),
)
DEFAULT_REVISION = (_MANIFEST or {}).get("revision")

def _downloadable_files(manifest: dict) -> tuple[str, ...]:
    """Return safe, manifest-pinned paths to fetch from the immutable HF revision."""
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("weights manifest must declare downloadable files")
    paths: list[str] = []
    for relative, digest in files.items():
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError("weights manifest contains an invalid artifact path")
        posix_path = PurePosixPath(relative)
        windows_path = PureWindowsPath(relative)
        if (posix_path.is_absolute() or windows_path.is_absolute()
                or windows_path.drive or ".." in posix_path.parts
                or ".." in windows_path.parts or "\\" in relative
                or any(part in {"", ".", ".."} for part in relative.split("/"))):
            raise ValueError(f"weights manifest artifact path is not relative and safe: {relative!r}")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"weights manifest has an invalid SHA-256 for {relative!r}")
        paths.append(relative)
    return tuple(sorted(paths))


def _validate_fetch_source(manifest: dict, repo: str, revision: str | None) -> None:
    """Require downloads to use the immutable source declared by the bundle contract."""
    expected_repo = manifest.get("repository")
    expected_revision = manifest.get("revision")
    if not isinstance(expected_repo, str) or not expected_repo.strip():
        raise ValueError("weights manifest must pin its source repository")
    if repo != expected_repo:
        raise ValueError(
            f"requested repository {repo!r} differs from manifest repository {expected_repo!r}"
        )
    if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        raise ValueError("weights manifest must pin a full immutable 40-character Git revision")
    if revision != expected_revision:
        raise ValueError("requested revision differs from the immutable manifest revision")


def _stage_committed_artifacts(
    source: Path,
    staging: Path,
    manifest: dict,
) -> None:
    """Copy Git-tracked manifest entries beside downloads for whole-bundle validation."""
    for relative in manifest.get("committed_artifacts", {}):
        source_path = resolve_artifact_path(source, relative)
        if not source_path.is_file():
            raise RuntimeError(f"committed runtime artifact is missing: {source_path}")
        destination = resolve_artifact_path(staging, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)


def _present(rel: str) -> bool:
    return (DATA_DIR / rel).is_file()


def main() -> int:
    ap = argparse.ArgumentParser(description="Provision the gitignored model weights into engine/data/.")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="Hugging Face repo id holding the weights")
    ap.add_argument("--force", action="store_true", help="re-download even if the file already exists")
    ap.add_argument("--revision", default=DEFAULT_REVISION,
                    help="HF revision; defaults to the immutable revision in weights_manifest.json")
    args = ap.parse_args()

    if args.repo.startswith("PLACEHOLDER"):
        print("PREREASONER_WEIGHTS_REPO is not set and no --repo given.\n"
              "  Publish the weights to a Hugging Face repo first, then run:\n"
              "    PREREASONER_WEIGHTS_REPO=<owner>/<repo> python -m engine.fetch_weights", file=sys.stderr)
        return 2

    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("huggingface_hub is required: pip install huggingface_hub", file=sys.stderr)
        return 2

    if _MANIFEST is None:
        print("weights_manifest.json is required for an atomic verified download", file=sys.stderr)
        return 2
    if _MANIFEST.get("unpublished_local"):
        print(
            "weights_manifest.json describes a local-only candidate; publish the exact bundle and "
            "promote it with its immutable revision before fetching on another machine",
            file=sys.stderr,
        )
        return 2
    try:
        # Validate even on a cache hit so explicit CLI/environment overrides are never silently
        # reported as successful when the existing bundle prevented the requested fetch.
        _validate_fetch_source(_MANIFEST, args.repo, args.revision)
        downloads = _downloadable_files(_MANIFEST)
    except ValueError as exc:
        print(f"invalid weights manifest or source override: {exc}", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not args.force:
        try:
            fingerprint = validate_weight_bundle(DATA_DIR)
        except RuntimeError:
            fingerprint = None
        if fingerprint is not None:
            print(f"weights ready in {DATA_DIR}: manifest verified ({fingerprint[:12]})")
            return 0

    with tempfile.TemporaryDirectory(prefix=".weights-", dir=DATA_DIR) as temporary:
        staging = Path(temporary)
        _stage_committed_artifacts(DATA_DIR, staging, _MANIFEST)
        for rel in downloads:
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"  fetching {rel} from {args.repo}@{args.revision} ...", flush=True)
            path = hf_hub_download(
                repo_id=args.repo,
                filename=rel,
                revision=args.revision,
                # An empty token is not equivalent to anonymous access for httpx: it produces
                # an invalid `Authorization: Bearer ` header in a fresh Cloud Shell. Treat the
                # optional environment variable as absent unless it contains an actual token.
                token=os.environ.get("HF_TOKEN") or None,
            )
            shutil.copyfile(path, dest)
        fingerprint = validate_weight_bundle(staging, _MANIFEST)
        for rel in downloads:
            dest = DATA_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / rel, dest)
    print(f"weights ready in {DATA_DIR}: verified bundle {fingerprint[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
