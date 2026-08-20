"""Promote a trained Schema.org candidate into the runtime bundle — the ONE writer of engine/data/.

Training writes only to `training/schema_org/data/experiments/<corpus>/`, which is disposable and
gitignored. This module is the separate, explicit, gated step that installs a candidate, so that:

  * the promoted bundle always has a baseline to roll back to (the previous files are kept as
    `<name>.previous` until the next promotion);
  * a candidate cannot reach serving by being written, only by passing the gates below;
  * the artifacts move together — the head, its calibrated thresholds, and the class signatures are
    fitted to ONE corpus, and installing a partial set produces a pair that loads without complaint and
    then behaves incoherently. (Writing signatures straight into the bundle out of order with training
    is exactly how every class silently became unservable in serving.)

    python -m training.schema_org.promote                 # list candidates
    python -m training.schema_org.promote <corpus-prefix>  # gate + install
    python -m training.schema_org.promote --rollback       # restore the previous bundle
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

from engine.config import DATA_DIR as RUNTIME_DIR
from engine.schema_org import load_contract
from training.schema_org.paths import EXPERIMENTS_DIR

ARTIFACTS = ("schema_property_head.pt", "schema_property_model.json",
             "schema_class_signatures.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def gate(candidate: Path) -> list[str]:
    """Every reason this candidate must not be promoted. Empty list == promotable."""
    problems = []
    for name in ARTIFACTS:
        if not (candidate / name).exists():
            problems.append(f"missing artifact: {name}")
    if problems:
        return problems
    meta = json.loads((candidate / "schema_property_model.json").read_text(encoding="utf-8"))
    signatures = json.loads((candidate / "schema_class_signatures.json").read_text(encoding="utf-8"))
    contract = load_contract()

    if meta.get("ontology_contract_sha256") != contract.contract_sha256:
        problems.append("property model was fitted against a different ontology contract")
    if signatures.get("ontology_contract_sha256") != contract.contract_sha256:
        problems.append("class signatures were built against a different ontology contract")
    if meta.get("corpus_sha256") != signatures.get("corpus_sha256"):
        problems.append(
            f"artifacts describe different corpora: model {str(meta.get('corpus_sha256'))[:12]} "
            f"vs signatures {str(signatures.get('corpus_sha256'))[:12]}"
        )
    if signatures.get("property_model_pending", True):
        problems.append("class signatures were never calibrated (property_model_pending is set)")
    if _sha256(candidate / "schema_property_head.pt") != meta.get("weights_sha256"):
        problems.append("weights do not match the hash recorded in the property model meta")
    if not sum(1 for row in signatures["classes"] if row.get("servable")):
        problems.append("no class is servable — promoting this would abstain on every table")
    if not meta.get("trained_properties"):
        problems.append("no property dimensions were trained")
    return problems


def promote(candidate: Path, *, force: bool = False) -> None:
    problems = gate(candidate)
    if problems and not force:
        raise SystemExit("candidate rejected:\n  " + "\n  ".join(problems))
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # Re-promoting the SAME candidate would otherwise overwrite the rollback generation with a copy of the
    # candidate itself, so `--rollback` would restore what it was meant to undo. Rotate only on a real change.
    unchanged = all((RUNTIME_DIR / name).exists()
                    and _sha256(RUNTIME_DIR / name) == _sha256(candidate / name) for name in ARTIFACTS)
    for name in ARTIFACTS:
        live = RUNTIME_DIR / name
        if live.exists() and not unchanged:                 # keep exactly one rollback generation
            shutil.copy2(live, RUNTIME_DIR / (name + ".previous"))
        shutil.copy2(candidate / name, live)
    if unchanged:
        print("bundle already matched this candidate; rollback generation left intact", flush=True)
    meta = json.loads((RUNTIME_DIR / "schema_property_model.json").read_text(encoding="utf-8"))
    print(f"promoted {candidate.name}: corpus {meta['corpus_sha256'][:12]}, "
          f"{len(meta['trained_properties'])} trained property dims, "
          f"weights {meta['weights_sha256'][:12]}\n"
          f"rollback: python -m training.schema_org.promote --rollback", flush=True)


def rollback() -> None:
    restored = []
    for name in ARTIFACTS:
        previous = RUNTIME_DIR / (name + ".previous")
        if previous.exists():
            shutil.copy2(previous, RUNTIME_DIR / name)
            restored.append(name)
    if not restored:
        raise SystemExit("no previous bundle to roll back to")
    print(f"rolled back: {', '.join(restored)}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", nargs="?", help="experiment directory name (corpus sha prefix)")
    parser.add_argument("--force", action="store_true",
                        help="install despite failed gates (records nothing; use only to reproduce a bug)")
    parser.add_argument("--rollback", action="store_true", help="restore the previous promoted bundle")
    args = parser.parse_args()
    if args.rollback:
        rollback()
        return 0
    if not args.candidate:
        if not EXPERIMENTS_DIR.exists():
            print("no candidates yet; run training.schema_org.train_property_head first")
            return 0
        for entry in sorted(EXPERIMENTS_DIR.iterdir()):
            if entry.is_dir():
                problems = gate(entry)
                print(f"  {entry.name}  {'PROMOTABLE' if not problems else 'blocked: ' + problems[0]}")
        return 0
    candidate = Path(args.candidate)
    if not candidate.exists():
        candidate = EXPERIMENTS_DIR / args.candidate
    if not candidate.exists():
        raise SystemExit(f"no such candidate: {args.candidate}")
    promote(candidate, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
