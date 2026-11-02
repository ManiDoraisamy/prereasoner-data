"""Training for engine/named: the reader's examples (data.py), augmentations (augment.py), multi-task training (train.py), the chooser's fit (choose.py), the whole-engine evaluation (evaluate.py), a GPU lease recipe (lease.py), and a small fake for tests and smoke runs (fake.py).

The factory's rows name tables and positions of its pickled models, and those pickles read under this checkout's classes as long as engine/sheet/model.py and values.py stand as they did (lease.py:ensure_same_tables). Every run writes only under EXPERIMENTS_DIR/<experiment id>/ (gitignored). Nothing here writes engine/data: promotion is a separate, approved act.
"""
from pathlib import Path

EXPERIMENTS_DIR = Path(__file__).resolve().parent / "data" / "experiments"
