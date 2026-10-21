"""Cross-fold stage-2 examples: the training databases split into folds. Each fold file trains on the other folds
and marks its own questions as the held-out split ("calibration" in the trainer's terms), so the fold's run writes
outputs for questions on databases it never fit. Those outputs yield re-scorer candidates that look like serving
candidates. Only training rows are read, so the real calibration databases are in no fold.

usage: python -m training.readers.folds <stage2.jsonl> <out dir> [--folds 4] [--seed 20261019]
"""
import argparse
import json
import os
import random


def main():
    ap = argparse.ArgumentParser(description="Cross-fold stage-2 example files, split by database.")
    ap.add_argument("source", help="a stage2.jsonl from training.readers.examples")
    ap.add_argument("out_dir")
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20261019)
    args = ap.parse_args()
    rows = [json.loads(line) for line in open(args.source, encoding="utf-8")]
    train = [r for r in rows if r["split"] == "train"]
    databases = sorted({r["db_id"] for r in train})
    random.Random(args.seed).shuffle(databases)
    fold_of = {db: i % args.folds for i, db in enumerate(databases)}
    os.makedirs(args.out_dir, exist_ok=True)
    for fold in range(args.folds):
        with open(os.path.join(args.out_dir, f"fold{fold}.jsonl"), "w", encoding="utf-8", newline="\n") as sink:
            held = 0
            for r in train:
                row = dict(r, split="calibration" if fold_of[r["db_id"]] == fold else "train")
                held += row["split"] == "calibration"
                sink.write(json.dumps(row) + "\n")
        print(f"fold{fold}: {held} held-out questions on {sum(1 for d in databases if fold_of[d] == fold)} databases")


if __name__ == "__main__":
    main()
