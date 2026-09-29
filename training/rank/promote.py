"""Stage one complete, validated CPU SQL bundle for an immutable release image.

The destination must not exist. We never hot-swap individual files in a live
bundle: validation happens in a sibling temporary directory, then one directory
rename publishes it. Deploying the tested image is the production switch.

    python -m training.rank.promote --source-bundle engine/data \
        --arbiter training/rank/data/experiments/<id>/sql_arbiter.json \
        --destination training/rank/data/experiments/<id>/bundle
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile

from engine.artifact_provenance import sha256_file, validate_weight_bundle, write_json_artifact
from engine.sql_rank import SQLArbiter
from engine.xiyan_sql_proposer import load_contract


def promote(source_bundle: Path, arbiter_path: Path, destination: Path) -> dict:
    """Validate model binding, stage a complete bundle, publish to a new path."""
    source_bundle, destination = source_bundle.resolve(), destination.resolve()
    if destination.exists():
        raise ValueError("destination already exists; runtime bundles are immutable")
    if destination == source_bundle or source_bundle in destination.parents:
        raise ValueError("candidate destination must be outside the source bundle")
    validate_weight_bundle(source_bundle)
    contract = load_contract(source_bundle / "xiyan_sql_proposer.json")
    model_path = source_bundle / "xiyan_sql_proposer.gguf"
    model_sha = sha256_file(model_path)
    if model_sha != contract["gguf"]["sha256"] or model_path.stat().st_size != contract["gguf"]["size_bytes"]:
        raise ValueError("source GGUF differs from its immutable contract")
    payload = json.loads(arbiter_path.read_text(encoding="utf-8"))
    arbiter = SQLArbiter.from_payload(payload, str(arbiter_path))
    arbiter_sha = sha256_file(arbiter_path)
    fitted_on = (payload.get("fit") or {}).get("proposer_adapter_sha256")
    matched = fitted_on == model_sha
    if matched:
        if (arbiter.proposer_beams != 1
                or arbiter.proposer_max_new_tokens != contract["generation"]["max_new_tokens"]
                or payload["fit"].get("likelihood_policy") != contract["likelihood_policy"]):
            raise ValueError("arbiter does not match the deployed generation/scoring contract")
        if payload.get("validation", {}).get("metric") != "saved_pool_serving_selection":
            raise ValueError("arbiter lacks serving-faithful validation")
    elif (arbiter_sha != contract["selector"]["sha256"]
          or fitted_on != contract["selector"]["fit_source_proposer_sha256"]
          or contract["selector"]["model_matched_arbiter"]):
        raise ValueError("refusing a new mismatched selector; only the frozen measured baseline may be restaged")
    contract = copy.deepcopy(contract)
    contract["selector"].update(sha256=arbiter_sha, fit_source_proposer_sha256=fitted_on,
                                model_matched_arbiter=matched)
    manifest = json.loads((source_bundle / "weights_manifest.json").read_text(encoding="utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sql-bundle-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "bundle"
        shutil.copytree(source_bundle, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(arbiter_path, staged / "sql_arbiter.json")
        write_json_artifact(staged / "xiyan_sql_proposer.json", contract, indent=2)
        for name in ("sql_arbiter.json", "xiyan_sql_proposer.json"):
            manifest.setdefault("committed_artifacts", {})[name] = {
                "sha256": sha256_file(staged / name),
                "note": "tracked release metadata staged by training/rank/promote.py",
            }
        write_json_artifact(staged / "weights_manifest.json", manifest, indent=2)
        fingerprint = validate_weight_bundle(staged)
        load_contract(staged / "xiyan_sql_proposer.json")
        if sha256_file(staged / "xiyan_sql_proposer.gguf") != model_sha:
            raise ValueError("staged GGUF hash differs from its contract")
        os.rename(staged, destination)
    return {"bundle": fingerprint, "model_sha256": model_sha, "arbiter_sha256": arbiter_sha,
            "model_matched_arbiter": matched, "destination": str(destination),
            "release_gates_passed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--arbiter", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(promote(args.source_bundle, args.arbiter, args.destination), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
