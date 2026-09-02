"""Canonical artifact paths shared without importing database or training stages."""
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
CORPUS_PATH = DATA_DIR / "semantic_instances.jsonl"
MANIFEST_PATH = DATA_DIR / "semantic_manifest.json"
EMBEDDINGS_PATH = DATA_DIR / "schema_embeddings.npz"

# Training NEVER writes into engine/data/ — that directory is the promoted runtime bundle, and a training
# run that overwrites it in place has no baseline to roll back to and no separable promotion decision.
# Candidates land here, keyed by the corpus they were fitted to, and `python -m training.schema_org.promote`
# is the explicit, gated step that installs one. (This also removes an ordering hazard that shipped: the
# signature builder wrote straight into the live bundle, so running it without retraining left every class
# unservable in serving.)
EXPERIMENTS_DIR = DATA_DIR / "experiments"


def experiment_dir(corpus_sha256: str) -> Path:
    return EXPERIMENTS_DIR / corpus_sha256[:12]

