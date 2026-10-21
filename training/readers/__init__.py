"""Training pipeline for the own-data readers (engine/readers): labels, examples, the two reader trainers, the
re-scorer, refusal, and the GPU leases that run them. See README.md for the recipe of the shipped bundle.

Every run writes only under ``EXPERIMENTS_DIR/<experiment id>/`` (gitignored). Only the promotion step
(training.readers.promote) writes engine/data/readers.
"""
from pathlib import Path

EXPERIMENTS_DIR = Path(__file__).resolve().parent / "data" / "experiments"
