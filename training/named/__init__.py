"""Training for engine/named: the reader's examples (data.py), augmentations (augment.py), multi-task training (train.py), the chooser's fit (choose.py), the whole-engine evaluation (evaluate.py), a GPU lease recipe (lease.py), and a small fake for tests and smoke runs (fake.py).

Set NAMED_PIN=1 to read the factory's data locally: its programs and pickled tables hold to the commit it pinned. Every run writes only under EXPERIMENTS_DIR/<experiment id>/ (gitignored). Nothing here writes engine/data: promotion is a separate, approved act.
"""
import os
from pathlib import Path

if os.environ.get("NAMED_PIN"):
    # The factory's rows and pickled tables are of one commit of engine/sheet and engine/program: read them with that commit's code.
    from training.factory.paths import pin
    pin()

EXPERIMENTS_DIR = Path(__file__).resolve().parent / "data" / "experiments"
