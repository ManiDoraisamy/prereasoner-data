"""The program path's parity with the Decisions measurement it was ported from (decisions-probe-20261118: the stock arm
with the kept closed questions, the tab in view named and the said call, tag eor1; DataBench, tag pgd1), on the 896 dev
questions:
messy dev, the WikiTableQuestions dev sample, DataBench dev and the fresh dev twin. No frozen set is read, and no Enron
sheet.

Every question runs through the one own-data selection, engine/tables.py TableQuery.select_query (no active sheet, as
measured), on the request tables of its workbook, each table's grid the tab its loader built; engine.llm.DECISIONS_TRANSPORT
is replaced by a replay of the measurement's own cache (cache/<hash[:2]>/<hash>.json, keyed by engine.llm.decision_hash):
the API is never called, no key is read, and a body the cache does not hold raises. A miss means the serving path built a
request the measurement did not; its body is written to <out>/misses/<hash>.json for a diff. Each question is then
compared with its set's measured outcome (REFERENCE_TAGS): the listed answers (answer hashes, in order), their grades,
the choice probabilities and the per-answer label (summarize.decided at level "answer").

The sets load exactly as the measurement's run_stock.py loaded them (its loaders, read from the experiment folders), and
each workbook's tabs are the wire-form tabs its loader compiles (header and typed rows for messy, WikiTableQuestions and
DataBench; the xlsx reader's tabs for the fresh dev twin). A workbook's questions share one request database
(engine/prepared.py scratch_database), as a conversation's turns share a prepared workbook. The stock reader keeps the vectors of the texts it read before,
and a batch's padding can move a vector in its last bits, so a run keeps the measurement's history by its order, at
--threads 4: --sets wtq and --sets messy,freshdev (as eor1 ran) and --sets freshdev,databench (as pgd1 ran), one process
each. Results: regress/results/dev_decisions/<tag>/<set>.jsonl and summary_<sets>.json.

--csv is a new measurement, not parity: production receives each tab as CSV, so each tab is rendered to CSV text (the
cells as the loader read them) and read as an upload is (csv_table, canonical table names); its bodies differ from the
measurement's, so it needs --record DIR, which sends a body neither cache holds to the Decisions API (the deployment's
GOOGLE_API_KEY) and keeps the answer in DIR for a replay.

usage: python -X utf8 -m regress.run_dev_decisions [--sets messy,freshdev] [--limit N] [--tag NAME] [--threads 4] [--resume]
                                                   [--csv --record DIR]
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import contextlib
import csv
import io

import torch

import engine.llm as llm
import engine.sheet
from engine.prepared import scratch_database
from engine.program.answer import ANSWERED, ASKED, WARNED
from engine.program.closed import answer_key
from engine.request_validation import canonical_table_name
from engine.tables import TableQuery, csv_table, normalize_tables

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = Path("C:/work/prereasoner-data/training/readers/data/experiments")
PROBE = EXPERIMENTS / "decisions-probe-20261118"
# Each set against its measured tag (decisions-probe-20261118 results/e2e_<tag>_stock_<set>.jsonl.gz): eor1, the kept
# stack with the tab in view named and the said call; DataBench against pgd1, the kept stack before those two, since no
# workbook of it has two tabs and no question of it reads a word pair (its records are pgd1's but for the said call)
REFERENCE_TAGS = {"messy": "eor1", "freshdev": "eor1", "wtq": "eor1", "databench": "pgd1"}
SETS = ("messy", "wtq", "databench", "freshdev")


class ReplayMiss(RuntimeError):
    """A request body the measurement's cache does not hold."""


class Replay:
    """engine.llm's transport over the measurement's cache: the recorded answer of a body, by its hash. Without
    ``record`` it never calls the API; with it, a body neither cache holds is sent through engine.llm's own transport
    and its answer kept in ``record`` (the same layout), so the run can be replayed."""

    def __init__(self, cache: Path, misses: Path, record: Path | None = None):
        self.cache, self.misses, self.record = cache, misses, record
        self.send = llm.DECISIONS_TRANSPORT
        self.used: list[str] = []

    def __call__(self, payload: bytes) -> dict:
        digest = llm.decision_hash(payload)
        held = self.cache / digest[:2] / f"{digest}.json"
        if not held.exists() and self.record is not None:
            held = self.record / digest[:2] / f"{digest}.json"
            if not held.exists():
                answer = self.send(payload)
                held.parent.mkdir(parents=True, exist_ok=True)
                held.write_bytes((json.dumps({"answers": answer["answers"], "tokens": answer["tokens"]}) + "\n").encode("utf-8"))
        if not held.exists():
            self.misses.mkdir(parents=True, exist_ok=True)
            (self.misses / f"{digest}.json").write_bytes(payload)
            raise ReplayMiss(digest)
        self.used.append(digest)
        recorded = json.loads(held.read_text(encoding="utf-8"))
        return {"answers": recorded["answers"], "tokens": recorded["tokens"]}


class _Payload(Exception):
    """Raised in place of reading a workbook: the wire form a set's loader built."""

    def __init__(self, raw):
        super().__init__("payload")
        self.raw = raw


def _capture(raw):
    raise _Payload(raw)


@contextlib.contextmanager
def capturing():
    """Every loader reads its workbook through engine.sheet.workbook_from_json before it compiles it: while this holds,
    that raises the wire form instead. Messy's loader takes the function when its set loads, the others when an item
    loads, so both run under it."""
    held = engine.sheet.workbook_from_json
    engine.sheet.workbook_from_json = _capture
    try:
        yield
    finally:
        engine.sheet.workbook_from_json = held


def csv_text(rows) -> str:
    """A tab's cells as CSV text, each written as the loader read it (engine/sheet/xlsx_reader.py: a number, a date as its
    serial number, a text), an integral number without its ".0"."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if cell is None else str(int(cell)) if isinstance(cell, float) and cell.is_integer() else str(cell)
                         for cell in row])
    return out.getvalue()


def tables_of(item, as_csv=False):
    """The request tables of the item's workbook: one per tab its loader built, each carrying that tab as its grid. With
    as_csv, each grid tab is instead rendered to CSV text (csv_text) and read as an upload is read: named by its canonical
    table name (engine/request_validation.py) and parsed by engine/tables.py csv_table, its grid the CSV's rows."""
    try:
        with capturing():
            item.load()
    except _Payload as held:
        tables = []
        for index, tab in enumerate(held.raw.get("tabs") or ()):
            rows = [list(row) for row in tab.get("rows") or ()]
            if as_csv:
                if rows and (tab.get("sheetType") or "grid") == "grid":
                    tables.append(csv_table(csv_text(rows), canonical_table_name(tab["name"], index)))
                continue
            tables.append({"name": tab["name"], "columns": [str(cell) for cell in rows[0]] if rows else [], "rows": rows[1:], "grid": tab})
        return normalize_tables(tables)
    raise RuntimeError(f"{item.id}: the loader read no workbook")


def kind_of(selection) -> str:
    return ASKED if selection.selected is None else WARNED if selection.warned else ANSWERED


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def experiment_modules():
    """The measurement's loaders (run_e2e: wtq_dev_sample, load_databench, fresh_dev_items) and its decision rule
    (summarize.decided). Its modules put NEXT_ENGINE on the path: it is this checkout, so every engine module is ours."""
    os.environ["NEXT_ENGINE"] = str(ROOT)
    for folder in (EXPERIMENTS / "clef-serve-20261109" / "local", EXPERIMENTS / "clef-reading-zs-20261109" / "work"):
        if str(folder) not in sys.path:
            sys.path.append(str(folder))
    run_e2e = _module("run_e2e", EXPERIMENTS / "clef-serve-20261109" / "local" / "run_e2e.py")
    summarize = _module("summarize", PROBE / "local" / "summarize.py")
    return run_e2e, summarize


def items_of(name, run_e2e, limit):
    from training.named.evaluate import load_messy
    with capturing():
        messy = load_messy("dev") if name == "messy" else None
    source = (messy if name == "messy" else run_e2e.wtq_dev_sample() if name == "wtq" else run_e2e.fresh_dev_items() if name == "freshdev"
              else run_e2e.load_databench("dev", max_rows=60000))
    return [item for item in source if item.label == "gold"][:limit]


def graded(item, rows) -> bool:
    result = list(rows)
    return bool(result) and bool(item.grade(result) or (item.alternate is not None and item.alternate(result)))


def label_of(kind: str, right: bool) -> str:
    if kind == ASKED:
        return "asked"
    return ("flagged_" if kind == WARNED else "") + ("right" if right else "wrong")


def reference(name: str) -> dict:
    with gzip.open(PROBE / "results" / f"e2e_{REFERENCE_TAGS[name]}_stock_{name}.jsonl.gz", "rt", encoding="utf-8") as source:
        return {row["id"]: row for row in map(json.loads, source) if row}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sets", default=",".join(SETS))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--tag", default="parity")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--resume", action="store_true", help="skip questions already recorded (the stock reader's history then differs)")
    parser.add_argument("--csv", action="store_true", help="each tab rendered to CSV and read as an upload is (a new measurement, not parity)")
    parser.add_argument("--record", type=Path, help="a cache folder: a body neither cache holds is sent to the Decisions API and kept here")
    args = parser.parse_args()
    names = args.sets.split(",")
    if any(name not in SETS for name in names):
        raise SystemExit(f"sets are {SETS}")
    torch.set_num_threads(args.threads)
    out = ROOT / "regress" / "results" / "dev_decisions" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    replay = Replay(PROBE / "cache", out / "misses", args.record)
    llm.DECISIONS_TRANSPORT = replay
    planner = TableQuery()
    run_e2e, summarize = experiment_modules()
    totals = {}
    for name in names:
        items = items_of(name, run_e2e, args.limit)
        held = reference(name)
        target = out / f"{name}.jsonl"
        done = set()
        if args.resume and target.exists():
            done = {json.loads(line)["id"] for line in target.read_text(encoding="utf-8").splitlines() if line.strip()}
        counts = {"n": 0, "misses": 0, "errors": 0, "mismatches": 0, "labels": {}, "reference_labels": {}}
        started = time.time()
        book, tables = None, None
        request = contextlib.ExitStack()
        with request, open(target, "a" if args.resume else "w", encoding="utf-8", newline="\n") as sink:
            for at, item in enumerate(items):
                if item.id in done:
                    continue
                replay.used = []
                clock = time.perf_counter()
                row = {"id": item.id, "set": name, "miss": None, "error": None}
                try:
                    if item.book != book:
                        book, tables = None, None
                        request.close()
                        tables = tables_of(item, args.csv)
                        request.enter_context(scratch_database())
                        book = item.book
                    selection = planner.select_query(item.question, tables)
                    shown = selection.selected if selection.selected is not None else selection.declined
                    rows = selection.rows[shown] if shown is not None else ()
                    kind = kind_of(selection)
                    listed_right = [graded(item, listed) for listed in selection.rows]
                    row.update(kind=kind, label=label_of(kind, graded(item, rows)), mass=selection.confidence, listed=len(selection.rows),
                               listed_answer=[answer_key(listed) for listed in selection.rows], listed_right=listed_right,
                               choose_p=selection.probabilities, read_as=selection.pool[shown].evidence[0] if shown is not None else "")
                except ReplayMiss as miss:
                    book, tables = None, None
                    request.close()
                    row.update(kind=ASKED, label="asked", miss=str(miss), listed_answer=None, choose_p=None, listed_right=None)
                except Exception as error:     # the measurement recorded a failed question as asked, with its error
                    book, tables = None, None
                    request.close()
                    row.update(kind=ASKED, label="asked", error=f"{type(error).__name__}: {str(error)[:160]}", listed_answer=[], choose_p=None, listed_right=[])
                row["calls"] = list(replay.used)
                row["seconds"] = round(time.perf_counter() - clock, 3)
                theirs = held.get(item.id)
                differs = []
                if theirs is None:
                    differs.append("no reference outcome")
                else:
                    reference_label = summarize.decided(theirs, "answer")[0]
                    row["reference_label"] = reference_label
                    counts["reference_labels"][reference_label] = counts["reference_labels"].get(reference_label, 0) + 1
                    if row["miss"] is None:
                        if row["listed_answer"] != (theirs.get("listed_answer") or []):
                            mine, recorded = row["listed_answer"], theirs.get("listed_answer") or []
                            first = next((i for i, (a, b) in enumerate(zip(mine, recorded)) if a != b), min(len(mine), len(recorded)))
                            differs.append(f"listed answers ({len(mine)} vs {len(recorded)}, first differs at {first})")
                        if row["choose_p"] != theirs.get("choose_p"):
                            differs.append("choice probabilities")
                        if row["listed_right"] != (theirs.get("listed_right") or []):
                            differs.append("listed grades")
                        if row["label"] != reference_label:
                            differs.append(f"label {row['label']} vs {reference_label}")
                        if bool(row["error"]) != bool(theirs.get("error")):
                            differs.append(f"error {row['error']} vs {theirs.get('error')}")
                row["differs"] = differs
                counts["n"] += 1
                counts["misses"] += row["miss"] is not None
                counts["errors"] += row["error"] is not None
                counts["mismatches"] += bool(differs) and row["miss"] is None
                counts["labels"][row["label"]] = counts["labels"].get(row["label"], 0) + 1
                sink.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                sink.flush()
                if row["miss"] is not None:
                    print(f"MISS {name} {item.id}: {row['miss']} (body in {out / 'misses'})", flush=True)
                elif differs:
                    print(f"MISMATCH {name} {item.id}: {'; '.join(differs)}", flush=True)
                if (at + 1) % 25 == 0:
                    print(f"{name} {at + 1}/{len(items)} {round(time.time() - started)}s misses {counts['misses']} mismatches {counts['mismatches']}", flush=True)
        counts["seconds"] = round(time.time() - started, 1)
        totals[name] = counts
        print(json.dumps({"set": name, **counts}), flush=True)
    (out / f"summary_{'_'.join(names)}.json").write_bytes((json.dumps(totals, indent=1) + "\n").encode("utf-8"))
    clean = all(counts["misses"] == 0 and counts["mismatches"] == 0 for counts in totals.values())
    print("parity" if clean else "NOT at parity", flush=True)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
