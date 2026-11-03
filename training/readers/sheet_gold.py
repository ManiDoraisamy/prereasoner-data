"""The readers' training rows from the clean-sheet factory (training/factory): its gold questions and its label-and-formula anchors, over the tables the sheet compiler makes, read as the readers read any upload (engine/named/proposals.py:SheetView).

gold      Each gold row's program (the factory's grammar, bound to the workbook's compiled model) runs and must give the factory's own answer (enron_grade); it lowers through engine/program to its typed query, rewritten onto the view, and engine/readers/readings.readings_of gives its readings; the readings must build (engine/readers/readings.build) back to the same program (proposals.program_of). Anchors become an aggregate of their bound column, kept when it gives the formula's stored answer; their question is the label beside the formula and the header over it. Train side only: Enron rows whose workbook training/factory/split.py puts on train, and the public train sources; validation, frozen, quarantined and the held-out other-industry source are never read. --exclude families (the named chooser's held families) are left out too.
examples  stage-2 rows as training/readers/examples.py builds them (gold pruning, the token budget, value labels), anchored and plain, from the records gold wrote.

Every record: {"source": "factory"|"anchor", "index", "db_id" (the workbook's book id), "family", "question", "status"} plus "readings" when status is "ok". Counts print; no cell and no question.

usage: python -m training.readers.sheet_gold gold --out <dir> --exclude <held_families.json> [--anchors 15000] [--workers 4] [--limit N]
       python -m training.readers.sheet_gold examples --gold <dir> --out <dir> [--tokenizer <dir>]
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
import pickle
import random
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

FACTORY_OUT = "C:/work/prereasoner-readers-artifacts/factory/out/"
FACTORY_MODELS = "C:/work/prereasoner-readers-artifacts/factory/models/"
GOLD_FILES = ("train.jsonl", "train.over_quota.jsonl", "pub_eia.jsonl", "pub_eia.over_quota.jsonl", "pub_sec.jsonl", "pub_sec.over_quota.jsonl")
ANCHORS = "anchors.jsonl"
NEVER_READ = ("val", "held_out_other_industry", "frozen")
ANCHOR_FUNCTIONS = ("sum", "avg", "min", "max", "count")
SEED = 20261103
RECORDS = "readings.jsonl"


def book_id(name: str) -> str:
    from training.factory.prepare import book_id as factory_book_id
    return factory_book_id(name)


def anchor_question(anchor: dict) -> str | None:
    """A label as a person wrote it beside the figure, with the header over it when it adds words: "Total Cost ENA Costs"."""
    label = " ".join(str(anchor.get("label") or "").split())
    header = " ".join(str(anchor.get("header_above") or anchor.get("reads_header") or "").split())
    if label and header and header.casefold() not in label.casefold():
        return f"{label} {header}"
    return label or header or None


def _close(a, b) -> bool:
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= max(1e-6 * abs(b), 1e-9)


def _rows(exclude: set[str], limit: int | None) -> list[dict]:
    """The factory's gold rows on the train side and the anchors, each tagged with its source; sorted by workbook."""
    from training.factory.split import TRAIN, sides
    side_of = sides()
    out = []
    for name in GOLD_FILES:
        if any(name.startswith(word) for word in NEVER_READ):
            raise ValueError(f"{name} is never read here")
        with open(f"{FACTORY_OUT}{name}", encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row.get("label") != "gold" or row["family"] in exclude:
                    continue
                if not row["side"].startswith("pub_") and (row["side"] != TRAIN or side_of.get(row["file"]) != TRAIN):
                    raise ValueError("a gold row whose workbook is not on the train side")
                out.append({"source": "factory", "key": row["id"], "book": row["book"], "family": row["family"], "question": row["question"], "program": row["program"], "answer": row.get("answer"), "form": row.get("form")})
    with open(f"{FACTORY_OUT}{ANCHORS}", encoding="utf-8") as source:
        for at, line in enumerate(source):
            anchor = json.loads(line)
            binding = anchor.get("binding")
            if binding is None or anchor["form"] != "agg" or anchor["function"] not in ANCHOR_FUNCTIONS or str(anchor["family"]) in exclude:
                continue
            if side_of.get(anchor["file"]) != TRAIN:
                raise ValueError("an anchor whose workbook is not on the train side")
            question = anchor_question(anchor)
            if question is None:
                continue
            out.append({"source": "anchor", "key": f"anchor{at}", "book": book_id(anchor["file"]), "family": str(anchor["family"]), "question": question, "function": anchor["function"], "binding": binding, "answer": anchor["answer"]})
    out.sort(key=lambda row: (row["book"], row["source"], row["key"]))
    if limit:
        keep = set(sorted({row["book"] for row in out}, key=lambda book: hashlib.blake2b(book.encode(), digest_size=8).digest())[:limit])
        out = [row for row in out if row["book"] in keep]
    return out


def _book(rows: list[dict]) -> list[dict]:
    """One workbook's rows read: each record's status and, when ok, its readings."""
    from engine.named.proposals import program_key, program_of, program_readings, sheet_view
    from engine.program.catalog import Catalog
    from engine.program.execute import run
    from engine.program.language import Agg, Col
    from engine.program.names import COUNT
    from engine.program.store import RequestTables
    from engine.readers.readings import NotReadable, build
    from training.factory.grammar import bind
    from training.named.evaluate import enron_grader, record_of
    grade = enron_grader()
    path = f"{FACTORY_MODELS}{rows[0]['book']}.pkl.gz"
    out = []
    if not os.path.exists(path):
        return [{**_head(row), "status": "no-model"} for row in rows]
    with gzip.open(path, "rb") as source:
        catalog = Catalog.from_model(pickle.load(source))
    view = sheet_view(catalog)
    tables = RequestTables(catalog)
    try:
        for row in rows:
            head = _head(row)
            try:
                if row["source"] == "factory":
                    program = bind(row["program"], catalog)
                else:
                    binding = row["binding"]
                    if binding["table"] not in catalog.by_id or binding["position"] >= len(catalog.table(binding["table"]).columns):
                        out.append({**head, "status": "unbound"})
                        continue
                    column = Col(binding["table"], int(binding["position"]))
                    program = Agg(COUNT, None, column.table) if row["function"] == "count" else Agg(row["function"], column, column.table)
            except Exception:                                       # noqa: BLE001 - a program the compiled model no longer holds
                out.append({**head, "status": "unbound"})
                continue
            try:
                rows_out = run(program, tables).rows
            except Exception:                                       # noqa: BLE001 - counted
                out.append({**head, "status": "run-failed"})
                continue
            if row["source"] == "factory":
                right = grade(record_of(row["answer"] or [], row.get("form")), list(rows_out))
            else:
                right = len(rows_out) == 1 and len(rows_out[0]) == 1 and _close(rows_out[0][0], row["answer"])
            if not right:
                out.append({**head, "status": "answer-differs"})
                continue
            try:
                readings = program_readings(program, view)
            except NotReadable as exc:
                out.append({**head, "status": f"not-readable:{exc}"})
                continue
            try:
                back = program_of(build(readings, view.graph), view)
            except Exception:                                       # noqa: BLE001 - the builder refuses it
                out.append({**head, "status": "build"})
                continue
            if back is None or program_key(back) != program_key(program):
                out.append({**head, "status": "round-trip-differs"})
                continue
            out.append({**head, "status": "ok", "readings": readings})
    finally:
        tables.connection.close()
    return out


def _head(row: dict) -> dict:
    return {"source": row["source"], "key": row["key"], "db_id": row["book"], "family": row["family"], "question": row["question"]}


def gold(args) -> None:
    exclude = set(json.loads(Path(args.exclude).read_text(encoding="utf-8"))) if args.exclude else set()
    rows = _rows(exclude, args.limit)
    by_book: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_book[row["book"]].append(row)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: collections.Counter = collections.Counter()
    records = []
    books = sorted(by_book)
    with ProcessPoolExecutor(args.workers) as pool:
        for done, result in enumerate(pool.map(_book, [by_book[book] for book in books], chunksize=8)):
            records.extend(result)
            if (done + 1) % 500 == 0:
                print(f"{done + 1} of {len(books)} workbooks", flush=True)
    anchors_ok = [record for record in records if record["source"] == "anchor" and record["status"] == "ok"]
    if args.anchors is not None and len(anchors_ok) > args.anchors:
        kept = set(sorted((record["key"] for record in anchors_ok), key=lambda key: hashlib.blake2b(f"{SEED}|{key}".encode(), digest_size=8).digest())[:args.anchors])
        for record in records:
            if record["source"] == "anchor" and record["status"] == "ok" and record["key"] not in kept:
                record["status"] = "anchor-over-cap"
                record.pop("readings", None)
    index = collections.Counter()
    with open(out_dir / RECORDS, "w", encoding="utf-8", newline="\n") as sink:
        for record in records:
            record["index"] = index[record["source"]]
            index[record["source"]] += 1
            counts[(record["source"], record["status"].split(":")[0] if not record["status"].startswith("not-readable") else record["status"])] += 1
            sink.write(json.dumps(record, ensure_ascii=False) + "\n")
    summary = {"rows": len(records), "books": len(books), "excluded_families": len(exclude), "exclude_sha256": hashlib.sha256(Path(args.exclude).read_bytes()).hexdigest() if args.exclude else None,
               "counts": {f"{source}/{status}": count for (source, status), count in sorted(counts.items())}, "anchors_cap": args.anchors,
               "records_sha256": hashlib.sha256((out_dir / RECORDS).read_bytes()).hexdigest()}
    (out_dir / "gold_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(summary, indent=1, sort_keys=True))


def _examples_of_book(task) -> list[tuple[str, str]]:
    """(anchored row, plain row) as JSON lines for one workbook's ok records."""
    records, tokenizer_dir = task
    from transformers import AutoTokenizer

    from engine.named.proposals import sheet_view
    from engine.program.catalog import Catalog
    from engine.readers.text import Schema
    from training.readers.examples import gold_pruning, reading_example, roles_of
    tokenizer = _tokenizer(AutoTokenizer, tokenizer_dir)
    with gzip.open(f"{FACTORY_MODELS}{records[0]['db_id']}.pkl.gz", "rb") as source:
        view = sheet_view(Catalog.from_model(pickle.load(source)))
    schema = Schema(view.graph)
    out = []
    for record in records:
        record = dict(record, split="train")
        roles, used = roles_of(record["readings"])
        rng = random.Random(f"{record['source']}:{record['index']}")
        tables, columns_of, droppable = gold_pruning(record, schema, roles, used, rng)
        rows = [json.dumps(reading_example(record, record["question"], schema, tables, columns_of, droppable, tokenizer, anchored)) for anchored in (True, False)]
        out.append((rows[0], rows[1]))
    return out


_TOKENIZERS: dict = {}


def _tokenizer(factory, directory):
    if directory not in _TOKENIZERS:
        _TOKENIZERS[directory] = factory.from_pretrained(directory)
    return _TOKENIZERS[directory]


def examples(args) -> None:
    from training.readers.provenance import assert_no_evaluation_sources
    records = [json.loads(line) for line in open(Path(args.gold) / RECORDS, encoding="utf-8")]
    records = [record for record in records if record["status"] == "ok"]
    assert_no_evaluation_sources(records, "sheet gold readings")
    by_book: dict[str, list[dict]] = collections.defaultdict(list)
    for record in records:
        by_book[record["db_id"]].append(record)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: collections.Counter = collections.Counter()
    books = sorted(by_book)
    with open(out_dir / "stage2_anchored.jsonl", "w", encoding="utf-8", newline="\n") as anchored, open(out_dir / "stage2_plain.jsonl", "w", encoding="utf-8", newline="\n") as plain, ProcessPoolExecutor(args.workers) as pool:
        for done, result in enumerate(pool.map(_examples_of_book, [(by_book[book], args.tokenizer) for book in books], chunksize=8)):
            for first, second in result:
                anchored.write(first + "\n")
                plain.write(second + "\n")
                row = json.loads(first)
                counts[row["source"]] += 1
                counts["values"] += len(row["values"]) + row["missing_values"]
                counts["values_found"] += len(row["values"])
            if (done + 1) % 500 == 0:
                print(f"{done + 1} of {len(books)} workbooks", flush=True)
    summary = {"rows": dict(counts), "anchored_sha256": hashlib.sha256((out_dir / "stage2_anchored.jsonl").read_bytes()).hexdigest(), "plain_sha256": hashlib.sha256((out_dir / "stage2_plain.jsonl").read_bytes()).hexdigest(), "gold_records_sha256": hashlib.sha256((Path(args.gold) / RECORDS).read_bytes()).hexdigest()}
    (out_dir / "examples_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(summary, indent=1, sort_keys=True))


def main() -> None:
    from training.readers.sources import TOKENIZER
    parser = argparse.ArgumentParser(description="The readers' rows from the clean-sheet factory's gold and anchors.")
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("gold")
    one.add_argument("--out", required=True)
    one.add_argument("--exclude", help="a JSON list of families never read (the named chooser's held families)")
    one.add_argument("--anchors", type=int, help="at most this many anchors, by seeded hash")
    one.add_argument("--workers", type=int, default=4)
    one.add_argument("--limit", type=int, help="only this many workbooks, by hash")
    two = commands.add_parser("examples")
    two.add_argument("--gold", required=True)
    two.add_argument("--out", required=True)
    two.add_argument("--tokenizer", default=TOKENIZER)
    two.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    gold(args) if args.command == "gold" else examples(args)


if __name__ == "__main__":
    main()
