"""Install a line model candidate: the ONE writer of engine/data/sheet_line_model.json. Training writes only to training/sheet/data/experiments/<id>/; this separate, explicit step replaces the one runtime artifact atomically and pins its hash in engine/data/weights_manifest.json under committed_artifacts. Rollback: promote the earlier candidate, or git checkout both files.

usage: python -m training.sheet.promote <experiment id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from training.sheet.train import EXPERIMENTS

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "engine" / "data" / "sheet_line_model.json"
MANIFEST = ROOT / "engine" / "data" / "weights_manifest.json"
NOTE = "tracked in git (not fetched); the sheet compiler's header decision, installed only by training/sheet/promote.py"


def promote(experiment: str) -> str:
    candidate = (EXPERIMENTS / experiment / "line_model.json").read_bytes()
    model = json.loads(candidate)
    assert {"base", "threshold", "steps"} <= set(model) and model["steps"], "not a line model"
    digest = hashlib.sha256(candidate).hexdigest()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["committed_artifacts"]["sheet_line_model.json"] = {"note": NOTE, "sha256": digest}
    manifest["committed_artifacts"] = dict(sorted(manifest["committed_artifacts"].items()))
    for path, data in ((RUNTIME, candidate), (MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
    return digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment")
    print("installed", promote(parser.parse_args().experiment))


if __name__ == "__main__":
    main()
