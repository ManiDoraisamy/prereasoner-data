"""Promote a readers bundle into the runtime: the ONE writer of engine/data/readers/.

Training writes candidates only under training/readers/data/experiments/<id>/, which is disposable and gitignored.
A candidate bundle holds:

  stage1/             the schema-item model (engine.readers.models.SchemaItemModel)
  stage2_<name>/      one directory per reading model (ReadingModel), with reader.json {"anchored": bool}: whether
                      it reads the question's values beside their columns (engine.readers.text.fit)
  rescorer/           the re-scorer (Rescorer)
  refusal.json        the refusal model fitted on the held-out calibration databases (training.readers.refusal)

each model directory a saved config.json, tokenizer files and model.pt. Promotion is a separate, explicit step:

  * every model must load in serving form (engine.readers.models.load) and every reader must say how it reads;
  * each model's encoder weights are stored in bfloat16, the precision serving runs them in; heads stay float32;
  * engine/data/readers is replaced as a whole (a sibling directory renamed into place), so a partial bundle never
    serves;
  * every file's SHA-256 is recorded in engine/data/weights_manifest.json under "readers/...". A release names the
    immutable Hugging Face revision that holds exactly these files (--revision, verified by download); a local
    bundle says it is unpublished (--local-only).

    python -m training.readers.promote <candidate dir> --local-only
    python -m training.readers.promote <candidate dir> --revision <immutable-commit>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import torch

from engine.artifact_provenance import json_artifact_bytes, sha256_file, validate_weight_bundle
from engine.config import DATA_DIR as RUNTIME_DIR

READERS = "readers"
MODEL_FILES = ("config.json", "model.pt", "sentencepiece.bpe.model", "tokenizer.json", "tokenizer_config.json")
REFUSAL_KEYS = ("features", "coef", "intercept", "threshold")
_IMMUTABLE_REVISION = re.compile(r"[0-9a-f]{40,64}")


def models_of(candidate: Path) -> dict[str, str]:
    """{model directory name: model kind} of a candidate bundle."""
    found = {"stage1": "schema_items", "rescorer": "rescorer"}
    found.update({path.name: "readings" for path in sorted(candidate.glob("stage2_*")) if path.is_dir()})
    return found


def gate(candidate: Path) -> list[str]:
    """Why ``candidate`` cannot be promoted (empty when it can)."""
    from engine.readers import models

    problems = []
    kinds = models_of(candidate)
    if not any(kind == "readings" for kind in kinds.values()):
        problems.append("no stage2_* reading model")
    for name, kind in kinds.items():
        directory = candidate / name
        missing = [file for file in MODEL_FILES if not (directory / file).is_file()]
        if missing:
            problems.append(f"{name}: missing {', '.join(missing)}")
            continue
        if kind == "readings":
            reader = directory / "reader.json"
            if not reader.is_file() or not isinstance(json.loads(reader.read_text(encoding="utf-8")).get("anchored"),
                                                      bool):
                problems.append(f"{name}: reader.json must say whether it reads anchored values")
        try:
            models.load(kind, directory)
        except Exception as exc:  # noqa: BLE001 - any load failure blocks promotion
            problems.append(f"{name}: does not load as {kind}: {type(exc).__name__}: {exc}")
    refusal = candidate / "refusal.json"
    if not refusal.is_file():
        problems.append("refusal.json missing")
    else:
        fitted = json.loads(refusal.read_text(encoding="utf-8"))
        if any(key not in fitted for key in REFUSAL_KEYS) or len(fitted["coef"]) != len(fitted["features"]):
            problems.append("refusal.json is not a fitted refusal model")
    return problems


def _serving_state(path: Path) -> dict:
    """``path``'s state dict with the encoder in bfloat16 and every head in float32."""
    state = torch.load(path, map_location="cpu", weights_only=True)
    return {key: (value.to(torch.bfloat16) if key.startswith("encoder.") and value.is_floating_point()
                  else value.float() if value.is_floating_point() else value)
            for key, value in state.items()}


def _stage(candidate: Path, staging: Path) -> None:
    for name in models_of(candidate):
        source, destination = candidate / name, staging / name
        destination.mkdir(parents=True)
        for file in MODEL_FILES + ("reader.json",):
            if file == "model.pt":
                torch.save(_serving_state(source / file), destination / file)
            elif (source / file).is_file():
                shutil.copyfile(source / file, destination / file)
    (staging / "refusal.json").write_bytes(
        json_artifact_bytes(json.loads((candidate / "refusal.json").read_text(encoding="utf-8")), indent=1))


def _verify_published(repository: str, revision: str, files: dict[str, str]) -> None:
    """Prove the exact staged files are downloadable at ``revision`` before the manifest names it."""
    if not _IMMUTABLE_REVISION.fullmatch(revision):
        raise ValueError("published revision must be an immutable 40-64 character commit hash")
    from huggingface_hub import hf_hub_download

    for relative, expected in files.items():
        downloaded = Path(hf_hub_download(repo_id=repository, filename=relative, revision=revision))
        if sha256_file(downloaded) != expected:
            raise ValueError(f"published {relative} does not match the promoted candidate")


def promote(candidate: Path, *, revision: str | None, local_only: bool) -> None:
    if bool(revision) == local_only:
        raise ValueError("provide exactly one immutable revision or local_only=True")
    problems = gate(candidate)
    if problems:
        raise SystemExit("candidate rejected:\n  " + "\n  ".join(problems))
    manifest_path = RUNTIME_DIR / "weights_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("runtime weights manifest is invalid")
    staging = Path(tempfile.mkdtemp(prefix=f".{READERS}.", dir=RUNTIME_DIR))
    try:
        _stage(candidate, staging)
        files = {f"{READERS}/{path.relative_to(staging).as_posix()}": sha256_file(path)
                 for path in sorted(staging.rglob("*")) if path.is_file()}
        if revision:
            _verify_published(manifest.get("repository", ""), revision, files)
        manifest["files"] = {key: value for key, value in manifest["files"].items()
                             if not key.startswith(f"{READERS}/")} | files
        manifest["revision"] = revision
        if local_only:
            manifest["unpublished_local"] = True
        else:
            manifest.pop("unpublished_local", None)
        current = RUNTIME_DIR / READERS
        retired = RUNTIME_DIR / f".{READERS}.retired"
        if retired.exists():
            shutil.rmtree(retired)
        if current.exists():
            os.replace(current, retired)
        os.replace(staging, current)
        manifest_path.write_bytes(json_artifact_bytes(manifest, indent=2))
        validate_weight_bundle(RUNTIME_DIR, manifest)
        if retired.exists():
            shutil.rmtree(retired)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    readers = sorted(name for name, kind in models_of(RUNTIME_DIR / READERS).items() if kind == "readings")
    print(f"promoted {candidate}: readers {', '.join(readers)}; {len(files)} files "
          f"({'unpublished local bundle' if local_only else 'revision ' + revision})", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("candidate", help="candidate bundle directory")
    release = parser.add_mutually_exclusive_group(required=True)
    release.add_argument("--revision", help="immutable published bundle revision")
    release.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    candidate = Path(args.candidate)
    if not candidate.is_dir():
        raise SystemExit(f"no such candidate: {candidate}")
    promote(candidate, revision=args.revision, local_only=args.local_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
