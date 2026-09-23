"""Install one SQL selection bundle into the runtime: the proposer adapter + its fitted arbiter.

The ONE writer of engine/data/sql_proposer/ and engine/data/sql_arbiter.json. It refuses an
arbiter that was fit on pools from a different adapter, copies both atomically, and records
their hashes in engine/data/weights_manifest.json: the adapter files as fetched weights (they are
published to the weights repository, not to git) and the arbiter as a committed artifact.

A published promotion pins the immutable Hugging Face revision that contains exactly these
adapter bytes. ``--local-only`` installs for final evaluation but marks the manifest so a fresh
clone cannot mistake the local candidate for a fetchable release.

    python -m training.rank.promote --adapter training/proposer/data/experiments/<id> \\
        --arbiter training/rank/data/experiments/<id>/sql_arbiter.json --local-only
    python -m training.rank.promote ... --revision <immutable-hf-commit>
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from engine.artifact_provenance import (
    ADAPTER_FILES, adapter_sha256, sha256_file, validate_weight_bundle,
)
from engine.sql_rank import SQLArbiter

REPO = Path(__file__).resolve().parents[2]
DEFAULT_DESTINATION = REPO / "engine" / "data"


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{destination.name}.", dir=destination.parent,
                                     delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def promote(adapter: Path, arbiter_path: Path, destination: Path, *,
            revision: str | None, local_only: bool) -> dict:
    if bool(revision) == local_only:
        raise ValueError("provide exactly one of an immutable revision or local_only=True")
    adapter_identity = adapter_sha256(adapter)       # raises when a model file is missing
    payload = json.loads(arbiter_path.read_text(encoding="utf-8"))
    SQLArbiter.from_payload(payload, str(arbiter_path))       # features + pool contract
    fitted_on = (payload.get("fit") or {}).get("proposer_adapter_sha256")
    if fitted_on != adapter_identity:
        raise ValueError(f"arbiter was fit on proposer {fitted_on}, not this adapter "
                         f"({adapter_identity}); label pools with this adapter and refit")
    manifest_path = destination / "weights_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("destination weights_manifest.json is invalid")

    for name in ADAPTER_FILES:
        _atomic_copy(adapter / name, destination / "sql_proposer" / name)
        manifest["files"][f"sql_proposer/{name}"] = sha256_file(destination / "sql_proposer" / name)
    _atomic_copy(arbiter_path, destination / "sql_arbiter.json")
    manifest.setdefault("committed_artifacts", {})["sql_arbiter.json"] = {
        "note": "tracked in git (not fetched); fitted SQL arbiter installed only by "
                "training/rank/promote.py",
        "sha256": sha256_file(destination / "sql_arbiter.json"),
    }
    manifest["revision"] = revision
    if local_only:
        manifest["unpublished_local"] = True
    else:
        manifest.pop("unpublished_local", None)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                     prefix=".weights_manifest.", dir=destination,
                                     delete=False) as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, manifest_path)
    fingerprint = validate_weight_bundle(destination)
    return {"adapter_sha256": adapter_identity, "bundle": fingerprint, "revision": revision}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--adapter", type=Path, required=True)
    ap.add_argument("--arbiter", type=Path, required=True)
    ap.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--revision", help="immutable Hugging Face commit holding the adapter")
    target.add_argument("--local-only", action="store_true")
    args = ap.parse_args()
    result = promote(args.adapter, args.arbiter, args.destination,
                     revision=args.revision, local_only=args.local_only)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
