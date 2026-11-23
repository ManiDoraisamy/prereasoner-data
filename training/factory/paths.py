"""Where the factory reads and writes, and the one pin of the engine code it reads.

engine/sheet and engine/program are read as committed at one commit, recorded in the data folder's manifest, so a run that lasts days does not change under another agent's commit. Every stage calls pin() before it imports them; the rest of engine/ comes from the worktree.

The factory's folders are archived (training/archive.py): `python -m training.archive fetch readers-artifacts enron-derived`
restores them under PREREASONER_ARCHIVE; the Enron emails themselves come from figshare (emails_fetch.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from training.archive import archived

REPO = Path(__file__).resolve().parents[2].as_posix()
FACTORY = archived("prereasoner-readers-artifacts/factory/")
DESIGN = archived("prereasoner-readers-artifacts/evals/enron/design/")
QUESTION_SET = archived("prereasoner-readers-artifacts/evals/question_set/")
FILES = archived("enron_corpus/files/")
EVAL = archived("enron_corpus/eval/")
EMAILS = archived("enron_corpus/emails/")
NEVER = "stacey_white__39035"
SEED = 20261102
AUDIT_SEED = 20261106
MIN_FREE_BYTES = 50 * 1024 ** 3
MANIFEST = FACTORY + "manifest.json"
JOURNAL = FACTORY + "journal/"
MODELS = FACTORY + "models/"
TABS = FACTORY + "tabs/"
STAGED = FACTORY + "staged/"
OUT = FACTORY + "out/"
PUBLIC = FACTORY + "public/"
LOGS = FACTORY + "logs/"


def manifest() -> dict:
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as source:
            return json.load(source)
    return {}


def write_manifest(values: dict) -> None:
    current = manifest()
    current.update(values)
    os.makedirs(FACTORY, exist_ok=True)
    with open(MANIFEST + ".tmp", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(current, sink, indent=1, sort_keys=True)
    os.replace(MANIFEST + ".tmp", MANIFEST)


def commit_pinned() -> str:
    """The commit this stage reads engine/sheet and engine/program at: FACTORY_ENGINE_COMMIT for a side pinned apart, else the one set on first use. A side stays on one commit: its programs name positions of the tables compiled there."""
    pinned = os.environ.get("FACTORY_ENGINE_COMMIT") or manifest().get("engine_commit")
    if pinned:
        return pinned
    commit = subprocess.check_output(["git", "-C", REPO, "rev-parse", "HEAD"], text=True).strip()
    write_manifest({"engine_commit": commit})
    return commit


def pin() -> str:
    """Import engine.sheet and engine.program from an export of the pinned commit, however the worktree has moved. Returns the commit."""
    commit = commit_pinned()
    root = f"{FACTORY}engine_at/{commit[:7]}/"
    if not os.path.exists(root + "engine/program/__init__.py"):
        os.makedirs(root, exist_ok=True)
        archive = subprocess.run(["git", "-C", REPO, "archive", commit, "engine/sheet", "engine/program", "engine/data/sheet_line_model.json"], check=True, capture_output=True).stdout
        with open(root + "pinned.tar", "wb") as sink:
            sink.write(archive)
        subprocess.run(["tar", "-xf", "pinned.tar"], check=True, cwd=root)
    sys.dont_write_bytecode = True
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import engine
    if root + "engine" not in engine.__path__:
        engine.__path__.insert(0, root + "engine")
    return commit


__all__ = ["pin", "commit_pinned", "manifest", "write_manifest"]
