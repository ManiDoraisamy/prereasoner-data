"""The split, read from design/corpora_family_map.json and split_primary, seed 20261102, and the tabs each side uses: up to three workbooks a family on train, one on validation, at most six filled visible tabs a workbook (corpora_families.py).

side_of() says which side a workbook is on. Nothing else in the factory decides: a frozen, quarantined or dropped workbook is never listed, never opened.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict

from training.factory.paths import DESIGN, EVAL, NEVER, SEED

TRAIN = "train"
VAL = "val"
FROZEN = "frozen"
STUDIED = "studied_family"
DROPPED = "dropped"
MAX_TABS = 6
CAP = {TRAIN: 3, VAL: 1}


def _hash64(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8", "ignore"), digest_size=8).digest(), "little")


def mailbox(name: str) -> str:
    return name.split("__", 1)[0]


def load_family_map() -> dict[str, object]:
    with open(DESIGN + "corpora_family_map.json", encoding="utf-8") as source:
        return json.load(source)


def _studied_files() -> set[str]:
    """The studied workbooks' names. Only the file field of each record is read."""
    with open(EVAL + "results.jsonl", encoding="utf-8") as source:
        return {json.loads(line)["file"] for line in source}


def sides() -> dict[str, str]:
    """Every pool workbook's side, by the rule of corpora_families.py: studied families first, a family on two sides stays on the frozen side, then validation."""
    family_of = load_family_map()
    with open(DESIGN + "corpora_families.json", encoding="utf-8") as source:
        primary = json.load(source)["split_primary"]
    frozen_boxes, val_boxes = set(primary["frozen_list"]), set(primary["val_list"])
    dev = {family_of[f] for f in _studied_files() if f in family_of}
    boxes: dict[str, list[str]] = defaultdict(list)
    for name in family_of:
        boxes[mailbox(name)].append(name)
    frozen_fams = {family_of[f] for b in frozen_boxes for f in boxes.get(b, [])} - dev
    val_fams = {family_of[f] for b in val_boxes for f in boxes.get(b, [])} - frozen_fams - dev
    out = {}
    for name, fam in family_of.items():
        box = mailbox(name)
        if fam in dev:
            out[name] = STUDIED
        elif box in frozen_boxes:
            out[name] = FROZEN if fam in frozen_fams else DROPPED
        elif box in val_boxes:
            out[name] = VAL if fam in val_fams and fam not in frozen_fams else DROPPED
        else:
            out[name] = TRAIN if fam not in frozen_fams and fam not in val_fams else DROPPED
    return out


def filled_tabs() -> dict[str, list[str]]:
    """The visible tabs with at least five filled rows, per workbook, in workbook order, from the corpus profile."""
    out = {}
    with open(EVAL + "profiles.jsonl", encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if "error" in record:
                continue
            out[record["file"]] = [s["sheet"] for s in record["sheets"] if s["state"] == "visible" and s["rows"] >= 5]
    return out


def picks(side: str, cap: int | None = None) -> list[dict]:
    """The side's chosen workbooks: [{file, family, rank, tabs}], rank being the workbook's draw inside its family, so rank 0 covers every family before rank 1 begins. The draw is corpora_families.py's."""
    family_of = load_family_map()
    side_of = sides()
    tabs = filled_tabs()
    cap = cap if cap is not None else CAP[side]
    families: dict[object, list[str]] = defaultdict(list)
    for name, where in side_of.items():
        if where == side and not name.startswith(NEVER):
            families[family_of[name]].append(name)
    out = []
    for fam in sorted(families):
        rng = random.Random(_hash64(str(fam)) ^ SEED)
        pool = sorted(families[fam])
        rng.shuffle(pool)
        for rank, name in enumerate(pool[:cap]):
            out.append({"file": name, "family": fam, "rank": rank, "tabs": tabs.get(name, [])[:MAX_TABS]})
    return out


def all_train_workbooks() -> list[dict]:
    """Every train workbook, for the anchors, which need no cap: a family's copies are deduplicated by their words instead."""
    family_of = load_family_map()
    return [{"file": name, "family": family_of[name]} for name, where in sorted(sides().items()) if where == TRAIN and not name.startswith(NEVER)]


__all__ = ["CAP", "DROPPED", "FROZEN", "MAX_TABS", "STUDIED", "TRAIN", "VAL", "all_train_workbooks", "load_family_map", "mailbox", "picks", "sides"]
