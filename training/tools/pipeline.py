"""
pipeline.py — the SELF-CONTAINED release pipeline + transaction for the shipped model's data artifacts.

The training+release path, every step reading training/data only:
  1. build_from_entity  — capped.entity -> assignment/inference/units/alloc/taxonomy. The ONE external source (the
                          capped DB) and the only non-deterministic step (no ORDER BY), so it is run ONCE by hand and
                          the units are kept — this pipeline does NOT re-run it (a re-run would desync the units the
                          shipped model trained on).
  2. anchor_head        — ridge head -> anchor_assignment.npz (93 dims == alloc) + fill inference.csv PASS.
  3. calibrate_route / calibrate_dims — served thresholds (route_thresholds.json / dim_thresholds.json).
  4. validate_data + validate_route   — the GATES (data clean + served model routes the demo distribution).
  (reanchor — the RelBlock readout — is run by hand when the readout changes; it saves {alloc,cfg} on BOTH the
   checkpoint and final paths. The shipped encoder.pt is the proven warm-started body + re-anchored readout.
   Serving-bundle assembly moved to the engine package — copy training/data artifacts into engine/data, see
   training/README.md.)

TRANSACTION: snapshots ALL artifacts (CSVs, npz, thresholds, encoder.pt / encoder_meta.pt) to data/.bak FIRST and
restores them on ANY step failure — so a half-finished anchor/calibrate can never leave a model/artifact mismatch on
disk. DB steps are skipped (not failed) when WORLD_PG_PASSWORD is absent.

  $env:PYTHONUTF8=1; python -m training.tools.pipeline
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "training/data"
SNAP = ["assignment.csv", "inference.csv", "anchor_assignment.npz", "encoder_meta.pt", "encoder.pt",
        "route_thresholds.json", "dim_thresholds.json", "route_eval.json", "alloc.json", "taxonomy.csv"]
STEPS = [  # (module, args, needs_db)
    ("training.anchor.anchor_head", [], False),
    ("training.calibrate.calibrate_route", [], True),
    ("training.calibrate.calibrate_dims", [], True),
    ("training.calibrate.validate_data", [], False),
    ("training.calibrate.validate_route", [], True),
]


def main():
    bak = DATA / ".bak"
    if bak.exists():
        shutil.rmtree(bak)
    bak.mkdir(parents=True)
    for f in SNAP:
        if (DATA / f).exists():
            shutil.copy2(DATA / f, bak / f)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(ROOT)}
    try:
        for mod, args, needs_db in STEPS:
            if needs_db and not env.get("WORLD_PG_PASSWORD"):
                print(f"\n=== SKIP {mod} (no WORLD_PG_PASSWORD) ===", flush=True)
                continue
            print(f"\n=== {mod} {' '.join(args)} ===", flush=True)
            if subprocess.run([sys.executable, "-m", mod, *args], env=env, cwd=str(ROOT)).returncode != 0:
                raise SystemExit(f"step FAILED: {mod}")
        print("\npipeline: release pipeline GREEN — gates passed; artifacts consistent. "
              "Copy the training/data artifacts into engine/data to ship (see training/README.md).")
    except BaseException as e:
        print(f"\npipeline: {e}\n  ROLLBACK — restoring snapshot from {bak}", flush=True)
        for f in SNAP:
            if (bak / f).exists():
                shutil.copy2(bak / f, DATA / f)
        raise


if __name__ == "__main__":
    main()
