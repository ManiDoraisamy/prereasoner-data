"""The re-scorer's training file: union pools of cross-fold candidates, and the BIRD share questions.

Serving pools the candidates of two stage-2 readers, so the re-scorer trains on pools of that kind: for every question
held out by the cross-fold readers of either recipe (training.readers.folds), the union of their candidates
(training.readers.union.merge), the recipes merged in the order given; a question only one recipe held out keeps that
one's pool. Union pools come in (source, index) order.

BIRD states a percentage as 100 times the fraction a share reading computes, so with ``--bird`` a BIRD question's
share candidate is also right when 100 times its value (its first row, ``preview``) is gold's, the check labels_share
made its labels by. Cross-fold readers of a recipe with shares (v7) hold the 195 BIRD share questions in their pools;
``--share-weight N`` writes each union pool whose right answer is a share N times, so the few share questions weigh
in the listwise loss. Cross-fold readers without shares (v4, v6) hold none; ``--shares`` then adds the shipped
readers' candidates on the share questions' training rows (training.readers.candidates --bird, one file per reader),
merged per question in the first reader file's order and written ``--share-copies`` times (5 by default). Copies
carry distinct indexes.

The trainer reads no ``preview``; it is dropped. The shipped "us" re-scorer's file was built as
    python -m training.readers.rescorer_data rescorer_train_us.jsonl.gz --folds <v6 fold 1-3 candidates>
        --folds <v4 fold 0-3 candidates> --shares <v7a share candidates> <v5 share candidates> --bird <BIRD train>

usage: python -m training.readers.rescorer_data <out.jsonl.gz> --folds <a fold candidates.jsonl> ... --folds <b ...>
           [--bird <BIRD train dir>] [--share-weight 1] [--shares <reader share candidates.jsonl> ...]
           [--share-copies 5]
"""
import argparse
import gzip
import json
import sqlite3
from pathlib import Path

from training.readers.union import merge


class ShareLabels:
    """A BIRD question's share candidate that ran is also right when 100 times its value is gold's."""

    def __init__(self, bird):
        self.bird = bird
        self.questions = json.load(open(Path(bird) / "train.json", encoding="utf-8"))

    def gold(self, db_id, sql):
        path = Path(self.bird) / "train_databases" / db_id / f"{db_id}.sqlite"
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            connection.set_progress_handler(lambda: 1, 50_000_000)
            row = connection.execute(sql).fetchone()
            return None if row is None else row[0]
        except sqlite3.Error:
            return None
        finally:
            connection.close()

    def relabel(self, record):
        shares = [c for c in record["candidates"]
                  if c["read_as"].startswith("share of") and c.get("status") == "ran" and not c.get("right")]
        if record["source"] != "bird_train" or not shares:
            return
        gold = self.gold(record["db_id"], self.questions[int(record["index"])]["SQL"])
        if gold is None:
            return
        for candidate in shares:
            try:
                value = float(str(candidate.get("preview", "")).strip("[]() ,'"))
            except ValueError:
                continue
            if abs(float(gold) - 100.0 * value) <= 1e-6 * max(1.0, abs(float(gold))):
                candidate["right"] = True


def _records(paths, labels=None):
    found = {}
    for path in paths:
        for line in open(path, encoding="utf-8"):
            record = json.loads(line)
            if labels is not None:
                labels.relabel(record)
            found[(record["source"], record["index"])] = record
    return found


def _merged(parts):
    for part in parts:
        for candidate in part["candidates"]:
            candidate.pop("preview", None)
    return {**parts[0], "candidates": merge([part["candidates"] for part in parts])}


def union_pools(recipes, labels=None):
    """Each question's candidates merged across ``recipes`` (lists of candidate files), in (source, index) order."""
    loaded = [_records(paths, labels) for paths in recipes]
    keys = sorted({key for records in loaded for key in records})
    return [_merged([records[key] for records in loaded if key in records]) for key in keys]


def share_pools(paths, labels):
    """The share questions' pools from each reader's file, relabelled, merged per question in the first file's order."""
    by_reader, order = [], []
    for path in paths:
        records = {}
        for line in open(path, encoding="utf-8"):
            record = json.loads(line)
            labels.relabel(record)
            key = (record["source"], record["index"])
            records[key] = record
            if not by_reader:
                order.append(key)
        by_reader.append(records)
    return [_merged([records[key] for records in by_reader if key in records]) for key in order]


def right_share(pool):
    """Whether a pool's right answer is a share."""
    return any(c.get("right") and c["read_as"].startswith("share of") for c in pool["candidates"])


def main():
    ap = argparse.ArgumentParser(description="Build the re-scorer's training file.")
    ap.add_argument("out")
    ap.add_argument("--folds", nargs="+", action="append", required=True,
                    help="one recipe's cross-fold candidate files; give it once per recipe")
    ap.add_argument("--bird", help="BIRD's train directory: relabels BIRD share candidates (required with --shares)")
    ap.add_argument("--share-weight", type=int, default=1,
                    help="write each union pool whose right answer is a share this many times")
    ap.add_argument("--shares", nargs="+", default=[], help="each shipped reader's candidates on the share rows")
    ap.add_argument("--share-copies", type=int, default=5)
    args = ap.parse_args()
    labels = ShareLabels(args.bird) if args.bird else None
    pools = union_pools(args.folds, labels)
    shares = share_pools(args.shares, labels) if args.shares else []
    weighted = [pool for pool in pools if right_share(pool)]
    with gzip.open(args.out, "wt", encoding="utf-8", newline="\n") as sink:
        for pool in pools:
            sink.write(json.dumps(pool) + "\n")
        for copy in range(1, args.share_weight):
            for pool in weighted:
                sink.write(json.dumps({**pool, "index": f"{pool['index']}#{copy}"}) + "\n")
        for copy in range(args.share_copies):
            for pool in shares:
                sink.write(json.dumps({**pool, "index": f"{pool['index']}#{copy}"}) + "\n")
    print(f"{len(pools)} union pools ({len(weighted)} with a right share, x {args.share_weight}); {len(shares)} share "
          f"pools x {args.share_copies} ({sum(any(c.get('right') for c in p['candidates']) for p in shares)} with a "
          f"right candidate) -> {args.out}")


if __name__ == "__main__":
    main()
