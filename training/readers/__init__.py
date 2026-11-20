"""The experiments folder and the one GPU lease loop (lease.py) the training packages share. The readers' own
pipeline (labels, examples, trainers, re-scorer, refusal, promotion) went with the readers when the Decisions path
replaced them (DECISIONS.md, 2026-11-20); their measured runs stay under ``EXPERIMENTS_DIR``.

Every run writes only under ``EXPERIMENTS_DIR/<experiment id>/`` (gitignored).
"""
from pathlib import Path

EXPERIMENTS_DIR = Path(__file__).resolve().parent / "data" / "experiments"
