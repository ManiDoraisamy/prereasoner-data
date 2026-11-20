"""The program path's parity with the Decisions measurement it was ported from (decisions-probe-20261118: the stock arm
with the kept closed questions, tag pgd1, and the same with the tab in view named, tag tiv1), on the 896 dev questions:
messy dev, the WikiTableQuestions dev sample, DataBench dev and the fresh dev twin. No frozen set is read, and no Enron
sheet.

Every question runs through engine/program/answer.py (prepare, answer; no active sheet, as measured) with
engine.llm.DECISIONS_TRANSPORT replaced by a replay of the measurement's own cache (cache/<hash[:2]>/<hash>.json, keyed by
engine.llm.decision_hash): the API is never called, no key is read, and a body the cache does not hold raises. A miss
means the port built a request the measurement did not; its body is written to <out>/misses/<hash>.json for a diff. Each
question is then compared with its set's measured outcome (REFERENCE_TAGS): the listed answers (answer hashes, in
order), their grades, the choice probabilities and the per-answer label (summarize.decided at level "answer").

The sets load exactly as the measurement's run_stock.py loaded them (its loaders, read from the experiment folders), and
each workbook's payload is the one its loader compiles. The stock reader keeps the vectors of the texts it read before,
and a batch's padding can move a vector in its last bits, so a run keeps the measurement's history by its order, at
--threads 4: --sets messy,freshdev (as tiv1 ran), --sets wtq and --sets freshdev,databench (as pgd1 ran), one process
each. Results: regress/results/dev_decisions/<tag>/<set>.jsonl and summary_<sets>.json.

usage: python -X utf8 -m regress.run_dev_decisions [--sets messy,freshdev] [--limit N] [--tag NAME] [--threads 4] [--resume]
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

import torch

import engine.llm as llm
import engine.sheet
from engine.program.answer import ASKED, WARNED, answer, prepare

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = Path("C:/work/prereasoner-data/training/readers/data/experiments")
PROBE = EXPERIMENTS / "decisions-probe-20261118"
# Each set against its measured tag: pgd1, the kept stack; tiv1, the same with the tab in view named, on the sets holding
# workbooks of several tabs (WikiTableQuestions and DataBench hold none, so their records are pgd1's)
REFERENCE_TAGS = {"messy": "tiv1", "freshdev": "tiv1", "wtq": "pgd1", "databench": "pgd1"}
SETS = ("messy", "wtq", "databench", "freshdev")


class ReplayMiss(RuntimeError):
    """A request body the measurement's cache does not hold."""


class Replay:
    """engine.llm's transport over the measurement's cache: the recorded answer of a body, by its hash; never the API."""

    def __init__(self, cache: Path, misses: Path):
        self.cache, self.misses = cache, misses
        self.used: list[str] = []

    def __call__(self, payload: bytes) -> dict:
        digest = llm.decision_hash(payload)
        held = self.cache / digest[:2] / f"{digest}.json"
        if not held.exists():
            self.misses.mkdir(parents=True, exist_ok=True)
            (self.misses / f"{digest}.json").write_bytes(payload)
            raise ReplayMiss(digest)
        self.used.append(digest)
        recorded = json.loads(held.read_text(encoding="utf-8"))
        return {"answers": recorded["answers"], "tokens": recorded["tokens"]}


class _Payload(Exception):
    """Raised in place of a compile: the workbook payload a set's loader built."""

    def __init__(self, workbook):
        super().__init__("payload")
        self.workbook = workbook


def _capture(workbook, typed=None):
    raise _Payload(workbook)


def payload_of(item):
    """The workbook payload the item's loader compiles (each loader calls engine.sheet.compile_workbook)."""
    try:
        item.load()
    except _Payload as held:
        return held.workbook
    raise RuntimeError(f"{item.id}: the loader compiled nothing")


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
    source = (load_messy("dev") if name == "messy" else run_e2e.wtq_dev_sample() if name == "wtq" else run_e2e.fresh_dev_items() if name == "freshdev"
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
    args = parser.parse_args()
    names = args.sets.split(",")
    if any(name not in SETS for name in names):
        raise SystemExit(f"sets are {SETS}")
    torch.set_num_threads(args.threads)
    out = ROOT / "regress" / "results" / "dev_decisions" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    replay = Replay(PROBE / "cache", out / "misses")
    llm.DECISIONS_TRANSPORT = replay
    engine.sheet.compile_workbook = _capture       # loaders compile through it; the port compiles the captured payload
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
        book, prepared = None, None
        with open(target, "a" if args.resume else "w", encoding="utf-8", newline="\n") as sink:
            for at, item in enumerate(items):
                if item.id in done:
                    continue
                replay.used = []
                clock = time.perf_counter()
                row = {"id": item.id, "set": name, "miss": None, "error": None}
                try:
                    if item.book != book:
                        book, prepared = None, None
                        prepared = prepare(payload_of(item))
                        book = item.book
                    said = answer(item.question, prepared)
                    listed_right = [graded(item, rows) for rows in said.listed_rows]
                    row.update(kind=said.kind, label=label_of(said.kind, graded(item, said.rows)), mass=said.mass, listed=len(said.listed_rows),
                               listed_answer=list(said.listed_answers), listed_right=listed_right, choose_p=said.probabilities, read_as=said.read_as)
                except ReplayMiss as miss:
                    book, prepared = None, None
                    row.update(kind=ASKED, label="asked", miss=str(miss), listed_answer=None, choose_p=None, listed_right=None)
                except Exception as error:     # the measurement recorded a failed question as asked, with its error
                    book, prepared = None, None
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
