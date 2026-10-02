"""Record and verify the source identity of committed dependency locks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "dependency_locks.json"
LOCKS = {
    "requirements.lock.txt": ("requirements.txt", "linux-x86_64-python3.11"),
    "requirements-ci.lock.txt": ("requirements-ci.txt", "linux-x86_64-python3.11"),
    "requirements-ci-windows.lock.txt": ("requirements-ci.txt", "windows-x86_64-python3.11"),
    "orchestrator/requirements.lock.txt": (
        "orchestrator/requirements.txt",
        "linux-x86_64-python3.11",
    ),
    "db/sync/requirements-core.lock.txt": (
        "db/sync/requirements-core.txt",
        "linux-x86_64-python3.11",
    ),
    "db/sync/requirements.lock.txt": (
        "db/sync/requirements.txt",
        "linux-x86_64-python3.11",
    ),
    "deploy/gcp/requirements.lock.txt": (
        "deploy/gcp/requirements.txt",
        "linux-x86_64-python3.11",
    ),
    "training/requirements.lock.txt": (
        "training/requirements.txt",
        "linux-x86_64-python3.11-cpu",
    ),
}


def _sha256(relative: str) -> str:
    digest = hashlib.sha256()
    with (ROOT / relative).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _current_manifest() -> dict:
    return {
        "schema_version": 1,
        "generator": {"tool": "uv", "version": "0.9.26"},
        "locks": {
            lock: {
                "input": source,
                "input_sha256": _sha256(source),
                "lock_sha256": _sha256(lock),
                "target": target,
            }
            for lock, (source, target) in sorted(LOCKS.items())
        },
    }


def record() -> None:
    encoded = json.dumps(_current_manifest(), indent=2, sort_keys=True) + "\n"
    MANIFEST.write_text(encoded, encoding="utf-8", newline="\n")


def check() -> None:
    recorded = json.loads(MANIFEST.read_text(encoding="utf-8"))
    current = _current_manifest()
    if recorded != current:
        raise SystemExit(
            "dependency locks do not match deploy/dependency_locks.json; "
            "regenerate the affected lock and run python -m deploy.dependency_locks --record"
        )
    for relative in LOCKS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        if "--hash=sha256:" not in text:
            raise SystemExit(f"dependency lock has no package hashes: {relative}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="record the current input and lock hashes")
    args = parser.parse_args()
    if args.record:
        record()
    check()


if __name__ == "__main__":
    main()
