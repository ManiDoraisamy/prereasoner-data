"""The leak check, by script, on what was written: no train family on the validation or frozen side, no frozen or studied family anywhere in the out files, no question in a workbook the split does not place on that side. Reads the out files and the family map; a row's family is looked up again from its file name, not trusted.

usage: python -m training.factory.leakage     Writes FACTORY/out/leakage.json; exits 1 on any leak.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter, defaultdict

from training.factory.paths import OUT
from training.factory.split import DROPPED, FROZEN, STUDIED, TRAIN, VAL, load_family_map, sides


def check() -> dict:
    family_of = load_family_map()
    side_of = sides()
    family_sides = defaultdict(set)
    for name, side in side_of.items():
        family_sides[str(family_of[name])].add(side)
    families = {TRAIN: set(), VAL: set()}
    wrong_side: Counter = Counter()
    rows: Counter = Counter()
    for side in (TRAIN, VAL):
        path = f"{OUT}{side}.jsonl"
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row.get("source", "enron") != "enron":
                    continue
                rows[side] += 1
                name = row["file"]
                where = side_of.get(name, "unknown")
                if where != side:
                    wrong_side[f"{side}_row_in_{where}"] += 1
                families[side].add(str(family_of.get(name)))
    anchors = f"{OUT}anchors.jsonl"
    anchor_rows = 0
    if os.path.exists(anchors):
        with open(anchors, encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                anchor_rows += 1
                where = side_of.get(row["file"], "unknown")
                if where != TRAIN:
                    wrong_side[f"anchor_in_{where}"] += 1
                families[TRAIN].add(str(family_of.get(row["file"])))
    frozen_families = {fam for fam, members in family_sides.items() if FROZEN in members}
    studied_families = {fam for fam, members in family_sides.items() if STUDIED in members}
    dropped_families = {fam for fam, members in family_sides.items() if members == {DROPPED}}
    return {
        "rows": dict(rows), "anchor_rows": anchor_rows,
        "train_families": len(families[TRAIN]), "val_families": len(families[VAL]),
        "train_in_val": len(families[TRAIN] & families[VAL]),
        "train_in_frozen": len(families[TRAIN] & frozen_families),
        "val_in_frozen": len(families[VAL] & frozen_families),
        "train_in_studied": len(families[TRAIN] & studied_families),
        "val_in_studied": len(families[VAL] & studied_families),
        "train_in_dropped": len(families[TRAIN] & dropped_families),
        "val_in_dropped": len(families[VAL] & dropped_families),
        "rows_on_a_wrong_side": dict(wrong_side),
        "books_seen": sum(1 for _ in glob.glob(f"{OUT}*.jsonl")),
    }


def main() -> None:
    report = check()
    with open(f"{OUT}leakage.json", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(report, sink, indent=1, sort_keys=True)
    print(json.dumps(report, indent=0, sort_keys=True))
    leaks = sum(report[key] for key in report if "_in_" in key and isinstance(report[key], int)) + sum(report["rows_on_a_wrong_side"].values())
    sys.exit(1 if leaks else 0)


if __name__ == "__main__":
    main()
