"""fetch_weights.py — provision the gitignored model weights for a fresh clone.

The small config/JSON artifacts (alloc.json, families.json, props_thr.json, sql_*.json, taxonomy.csv,
word_*.json) are committed. The large binaries are gitignored and must be fetched once:

    encoder.pt              the trained RelationalModel readout (~72 MB)
    encoder_meta.pt         {alloc, cfg} for the readout
    qwen_lora/              the LoRA adapter for the Qwen2.5-0.5B encoder (~17 MB)
    anchor_assignment.npz   per-dim Youden-J thresholds for /api/dimension
    primitives.npz          the learned 10-primitive head

Usage:
    python -m engine.fetch_weights                 # download any missing weights into engine/data/
    python -m engine.fetch_weights --force         # re-download even if present
    PREREASONER_WEIGHTS_REPO=<hf-repo-id> python -m engine.fetch_weights

The Hugging Face repo id defaults to the value of PREREASONER_WEIGHTS_REPO (recommended) or the constant
below. Publish the weights once with `huggingface_hub.upload_folder(folder_path=engine/data, repo_id=...,
allow_patterns=['*.pt','*.npz','qwen_lora/*'])`, then a clone runs this to provision them.
"""
from __future__ import annotations
import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from engine.artifact_provenance import load_weights_manifest, validate_weight_bundle

DATA_DIR = Path(os.environ.get("PREREASONER_DATA_DIR") or Path(__file__).resolve().parent / "data")

# The Hugging Face repo holding the weights. Override with PREREASONER_WEIGHTS_REPO.
# NOTE: this repo is currently PRIVATE — set HF_TOKEN (a token with read access) to fetch from it.
_MANIFEST = load_weights_manifest(DATA_DIR)
DEFAULT_REPO = os.environ.get(
    "PREREASONER_WEIGHTS_REPO",
    (_MANIFEST or {}).get("repository", "prereasoner/prereasoner-weights"),
)
DEFAULT_REVISION = (_MANIFEST or {}).get("revision")

# (relative path under the HF repo == relative path under engine/data/, size for the log)
WEIGHTS = [
    "encoder.pt",
    "encoder_meta.pt",
    "anchor_assignment.npz",
    "primitives.npz",
    "qwen_lora/adapter_config.json",
    "qwen_lora/adapter_model.safetensors",
]


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

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not args.force:
        try:
            fingerprint = validate_weight_bundle(DATA_DIR)
        except RuntimeError:
            fingerprint = None
        if fingerprint is not None:
            print(f"weights ready in {DATA_DIR}: manifest verified ({fingerprint[:12]})")
            return 0

    if _MANIFEST is None:
        print("weights_manifest.json is required for an atomic verified download", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix=".weights-", dir=DATA_DIR) as temporary:
        staging = Path(temporary)
        for rel in WEIGHTS:
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"  fetching {rel} from {args.repo}@{args.revision} ...", flush=True)
            path = hf_hub_download(
                repo_id=args.repo,
                filename=rel,
                revision=args.revision,
                token=os.environ.get("HF_TOKEN"),
            )
            shutil.copyfile(path, dest)
        fingerprint = validate_weight_bundle(staging, _MANIFEST)
        for rel in WEIGHTS:
            dest = DATA_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging / rel, dest)
    print(f"weights ready in {DATA_DIR}: verified bundle {fingerprint[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
