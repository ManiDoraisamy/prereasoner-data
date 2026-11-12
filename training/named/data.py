"""The reader's examples: the factory's questions and labels with the compiled tables they read, one record per tab, thinned to what a training step reads, as gzipped pickle shards one folder can carry to a pod.

Family-held-out only. export refuses a workbook whose side is not the asked side (frozen and studied workbooks are never touched: training/factory/split.py says which is which, and only its train and val sides have rows at all). read_examples refuses a row of a side it was not asked for, and split_families and check_held_out keep a family on one side.

usage: python -m training.named.data export --side train|val --out <dir> [--limit-tabs N] [--max-lines 400]
Prints counts only, never a cell or a question.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import pickle
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

ALLOWED_SIDES = ("train", "val", "fake", "pub_eia", "pub_sec")          # never a frozen side, never the other-industry source held out for the frozen test
SHARD_TABS = 200
MANIFEST = "manifest.json"


@dataclass
class TabExample:
    """One tab: the tables a question on it may read, under the ids their programs use; its questions; its columns' schema.org labels by '<table>.<position>'."""
    key: str
    family: str
    file: str
    tab: str
    side: str
    tables: list[tuple[str, str, object]]      # (catalog id, kind, Table)
    rows: list[dict]
    labels: dict[str, dict]


def family_share(family: str, seed: int) -> float:
    """A family's place in [0, 1), the same on any machine."""
    return int.from_bytes(hashlib.blake2b(f"{seed}|{family}".encode("utf-8", "surrogatepass"), digest_size=8).digest(), "little") / 2 ** 64


def split_families(examples: list[TabExample], held_share: float, seed: int) -> tuple[list[TabExample], list[TabExample]]:
    """Whole families to one side or the other."""
    kept, held = [], []
    for example in examples:
        (held if family_share(example.family, seed) < held_share else kept).append(example)
    check_held_out(kept, held)
    return kept, held


def check_held_out(train: list[TabExample], held: list[TabExample]) -> None:
    """No family on both sides, and nothing but train and val rows."""
    shared = {example.family for example in train} & {example.family for example in held}
    if shared:
        raise ValueError(f"{len(shared)} families on both sides")
    for example in (*train, *held):
        if example.side not in ALLOWED_SIDES:
            raise ValueError(f"a {example.side} tab may not be read here")


def thin(table, keep_words: set[str], max_lines: int):
    """A table cut to max_lines records for training: lines holding a value a question links to, then the rest by digest. Printed lines stay. Answers are not read from a thinned table; the evaluation reads whole ones."""
    from engine.program.semantics import cell_fold
    if len(table.lines) <= max_lines:
        return table

    def rank(line) -> tuple:
        held = {cell_fold(cell) for cell in line.cells.values()}
        return (0 if held & keep_words else 1, hashlib.blake2b(str(line.row).encode(), digest_size=8).digest(), line.row)

    kept = sorted(sorted(table.lines, key=rank)[:max_lines], key=lambda line: line.row)
    return replace(table, lines=kept)


# Each side's row files: the side's own, and gold rows set apart over a family's quota, true and paid for. Quotas shape what is measured, not what may be learned from.
ROW_FILES = {"train": ("train.jsonl", "train.over_quota.jsonl"), "val": ("val.jsonl",), "pub_eia": ("pub_eia.jsonl", "pub_eia.over_quota.jsonl"), "pub_sec": ("pub_sec.jsonl", "pub_sec.over_quota.jsonl")}
NEVER_READ = "held_out_other_industry"


def tab_records(side: str, limit_tabs: int | None, max_lines: int, out_dir: str | Path) -> dict:
    """Write one side's shards. Reads the factory's rows and labels and the compiled models of the pinned commit."""
    from training.factory.paths import MODELS, OUT, pin
    from training.factory.split import sides
    commit = pin()
    from engine.program.catalog import Catalog
    side_of = sides()
    by_tab: dict[str, list[dict]] = defaultdict(list)
    digest = hashlib.sha256()
    for name in ROW_FILES[side]:
        if NEVER_READ in name:
            raise ValueError("the other-industry source is held out")
        with open(f"{OUT}{name}", "rb") as source:
            digest.update(source.read())
        with open(f"{OUT}{name}", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row.get("side") != side or (not side.startswith("pub_") and side_of.get(row["file"]) != side):
                    raise ValueError("a row whose workbook is not on this side")
                by_tab[f"{row['book']}|{row['tab']}"].append(row)
    labels: dict[str, dict] = {}
    with open(f"{OUT}{side}.schema_labels.jsonl", encoding="utf-8") as source:     # every column's schema.org property and values' class (training/factory/schema_labels.py)
        for line in source:
            record = json.loads(line)
            labels[record["key"]] = record["labels"]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    first_shard = len(list(out_dir.glob("shard_*.pkl.gz")))
    keys = sorted(by_tab)
    if limit_tabs:
        keys = sorted(keys, key=lambda key: hashlib.blake2b(key.encode("utf-8", "surrogatepass"), digest_size=8).digest())[:limit_tabs]
    keys.sort()
    shard, shards, count = [], 0, 0
    questions = gold = 0
    cached_book, cached_catalog = None, None
    for key in keys:
        rows = by_tab[key]
        book, tab = key.split("|", 1)
        if book != cached_book:
            with gzip.open(f"{MODELS}{book}.pkl.gz", "rb") as source:
                cached_catalog = Catalog.from_model(pickle.load(source))
            cached_book = book
        words = {str(link.get("target")).casefold() for row in rows for link in row.get("links", ()) if link.get("target")}
        tables = [(table.id, table.kind, thin(table.table, words, max_lines)) for table in cached_catalog.tables if table.tab == tab or (table.kind == "family" and any(line.tab == tab for line in table.lines))]
        shard.append(TabExample(key, rows[0]["family"], rows[0]["file"], tab, side, tables, rows, labels.get(key, {})))
        count += 1
        questions += len(rows)
        gold += sum(row.get("label") == "gold" for row in rows)
        if len(shard) == SHARD_TABS:
            write_shard(out_dir, first_shard + shards, shard)
            shard, shards = [], shards + 1
    if shard:
        write_shard(out_dir, first_shard + shards, shard)
        shards += 1
    manifest = {"side": side, "tabs": count, "rows": questions, "gold": gold, "shards": shards, "engine_commit": commit, "max_lines": max_lines, "rows_sha256": digest.hexdigest(), "limit_tabs": limit_tabs}
    if first_shard:                                 # a second side into one folder: the folder's manifest says all of them
        held = json.loads((out_dir / MANIFEST).read_text(encoding="utf-8"))
        if held["engine_commit"] != commit or held["max_lines"] != max_lines:
            raise ValueError("sides of one examples folder share an engine commit and a thinning")
        manifest = {**manifest, "side": f"{held['side']},{side}", "tabs": held["tabs"] + count, "rows": held["rows"] + questions, "gold": held["gold"] + gold, "shards": held["shards"] + shards, "rows_sha256": hashlib.sha256(f"{held['rows_sha256']}{digest.hexdigest()}".encode()).hexdigest()}
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return manifest


def write_shard(folder: str | Path, at: int, examples: list[TabExample]) -> None:
    path = Path(folder) / f"shard_{at:05d}.pkl.gz"
    with gzip.open(str(path) + ".tmp", "wb", compresslevel=6) as sink:
        pickle.dump(examples, sink, protocol=4)
    os.replace(str(path) + ".tmp", path)


def read_examples(folder: str | Path, side: str | None = None) -> list[TabExample]:
    """Every tab of an examples folder, in shard order."""
    out = []
    for path in sorted(Path(folder).glob("shard_*.pkl.gz")):
        with gzip.open(path, "rb") as source:
            out.extend(pickle.load(source))
    for example in out:
        if example.side not in ALLOWED_SIDES or (side is not None and example.side != side):
            raise ValueError(f"a {example.side} tab in a folder read as {side}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the reader's examples from the factory.")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--side", required=True, help="comma separated: " + ", ".join(sorted(ROW_FILES)))
    export.add_argument("--out", required=True)
    export.add_argument("--limit-tabs", type=int)
    export.add_argument("--max-lines", type=int, default=400)
    args = parser.parse_args()
    for side in args.side.split(","):
        print(json.dumps(tab_records(side, args.limit_tabs, args.max_lines, args.out), indent=0, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
