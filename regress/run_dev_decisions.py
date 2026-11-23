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
compared with its set's measured outcome (REFERENCE_TAGS, the latest layer that measured it): the listed answers (answer
hashes, in order), their grades, the choice probabilities and the per-answer label (summarize.decided at level "answer").

The sets load exactly as the measurement's run_stock.py loaded them (its loaders, read from the experiment folders), and
each workbook's tabs are the wire-form tabs its loader compiles (header and typed rows for messy, WikiTableQuestions and
DataBench; the xlsx reader's tabs for the fresh dev twin). A workbook's questions share one request database
(engine/prepared.py scratch_database), as a conversation's turns share a prepared workbook. The stock reader keeps the vectors of the texts it read before,
and a batch's padding can move a vector in its last bits, so a run keeps the measurement's history by its order, at
--threads 4: --sets wtq and --sets messy,freshdev (as eor1 ran) and --sets freshdev,databench (as pgd1 ran), one process
each. Results: regress/results/dev_decisions/<tag>/<set>.jsonl and summary_<sets>.json.

--sets demo is the demo gate (tests/test_datasets.py: each dataset's prompt and its standalone eval.txt follow-ups) against
vtdemo, graded on rows as vtdemo was: each question is a served request's own-data selection, on its own request database
with the world layer's links registered (engine/knowledge_bridges.py link_request), so it reads the live knowledgebase
through the Cloud SQL proxy. --demo-maps reads each dataset instead on the request database production built for it on
2026-11-18 (build_maps.py's maps folder), the inputs demo25 read, with the links recorded there.

--csv is a new measurement, not parity: production receives each tab as CSV, so each tab is rendered to CSV text (the
cells as the loader read them) and read as an upload is (csv_table, canonical table names); its bodies differ from the
measurement's, so it needs --record DIR, which sends a body neither cache holds to the Decisions API (the deployment's
GOOGLE_API_KEY) and keeps the answer in DIR for a replay.

--retry DIR measures serving's retry (engine/retry.py, owner 2026-11-21): each question whose answer is most likely wrong
(closed.ERRED, which serving never asks back) is retried over its workbook's tables as TableQuery.serve retries it, and
the retry's rows are graded (right, wrong, the sheet cannot answer it, or the retry failed); the retry model's answers
are kept in DIR by their body's hash (a body DIR lacks is sent to the model, so a second run replays them).

usage: python -X utf8 -m regress.run_dev_decisions [--sets messy,freshdev] [--limit N] [--tag NAME] [--threads 4] [--resume]
                                                   [--csv --record DIR] [--sets demo [--demo-maps DIR]] [--retry DIR]
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import contextlib
import csv
import io

import torch

import engine.llm as llm
import engine.sheet
from engine.prepared import scratch_database
from engine.program.answer import ANSWERED, ERRED, WARNED
from engine.program.closed import answer_key
from engine.request_validation import canonical_table_name
from engine.tables import TableQuery, csv_table, normalize_tables

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = Path("C:/work/prereasoner-data/training/readers/data/experiments")
PROBE = EXPERIMENTS / "decisions-probe-20261118"
# Each set against its kept stack (decisions-probe-20261118 results/e2e_<tag>_stock_<set>.jsonl.gz), measured in layers:
# first the whole set, eor1 (the kept stack with the tab in view named and the said call) or, on DataBench, pgd1 (the
# kept stack before those two, since no workbook of it has two tabs and no question of it reads a word pair), then each
# kept change's rerun of the questions it touches (gs1, af1, wm1 and DataBench's wm1b; cf1 and cf1b, the cell as the sheet
# wrote it; fg1 and fg1b, a prose column's word counts; ol1 and ol1b, each cell on one line; to1 and to1b, tied lines in
# sheet order; fp1, every question again: a text column's figures read as numbers and lines taken by where they stand),
# a later measurement of a question replacing an earlier one
REFERENCE_TAGS = {"messy": ("eor1", "to1", "pl1b", "vt1", "ec2", "fp1"), "freshdev": ("eor1", "gs1", "wm1", "cf1", "fg1", "ol1", "to1", "vt1", "fp1"),
                  "wtq": ("eor1", "af1", "wm1", "cf1", "fg1", "ol1", "to1", "pl1b", "ec2", "fp1"),
                  "databench": ("pgd1", "wm1", "wm1b", "cf1", "cf1b", "fg1", "fg1b", "ol1", "ol1b", "to1", "to1b", "pl1", "pl1b", "vt1b", "ec2", "fp1")}
SETS = ("messy", "wtq", "databench", "freshdev", "demo")
# The demo set against vtdemo (results/e2e_vtdemo_con_stock.jsonl.gz): the stock arm on the connected catalog (place,
# entity, currency and key links with each key's identities, the kept closed questions, the tab in view named, the
# value targets' token bound), read on the request databases production built for the demo datasets on 2026-11-18 (the
# day their money columns with no date convert on); then fp1, every demo question again with a text column's figures read
# as numbers and lines taken by where they stand (five pools of eval-neartail-ecommerce change, no outcome).
DEMO_REFERENCE = ("e2e_vtdemo_con_stock.jsonl.gz", "e2e_fp1_stock_demo.jsonl.gz")
DEMO_AS_OF = "2026-11-18"
CENT = 0.01                 # run_connected.py: a scalar within one cent, a row's cells to the cent


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


class QueryReplay:
    """engine.llm's retry transport (QUERY_TRANSPORT) over a folder of recorded answers by body hash: a body the folder
    lacks is sent to the retry model and its answer kept. ``tokens`` sums the input and output tokens sent anew."""

    def __init__(self, folder: Path):
        self.folder, self.send = folder, llm.QUERY_TRANSPORT
        self.tokens = [0, 0]

    def __call__(self, payload: bytes) -> dict:
        digest = llm.decision_hash(payload)
        held = self.folder / digest[:2] / f"{digest}.json"
        if not held.exists():
            answer = self.send(payload)
            usage = answer.get("usage") or {}
            self.tokens[0] += int(usage.get("promptTokenCount") or 0)
            self.tokens[1] += int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
            held.parent.mkdir(parents=True, exist_ok=True)
            held.write_bytes((json.dumps(answer) + "\n").encode("utf-8"))
        return json.loads(held.read_text(encoding="utf-8"))


def retry_row(planner, item, selection) -> dict:
    """Serving's retry of an erred question (engine/retry.py over the workbook its selection read, as
    TableQuery.serve retries it), graded: right, wrong, unanswerable (the model says the sheet cannot answer it) or
    failed (no query ran)."""
    from engine.retry import retry, workbook_tables

    retried = retry(item.question, workbook_tables(selection.catalog), planner.guard)
    outcome = ("right" if graded(item, retried.rows) else "wrong") if retried.answered else (
        "unanswerable" if not retried.answerable else "failed")
    return {"outcome": outcome, "sql": retried.sql, "reason": retried.reason, "error": retried.error,
            "rows": [list(row) for row in retried.rows[:5]]}


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
    return ERRED if selection.selected is None else WARNED if selection.warned else ANSWERED


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
    """The measurement's per-answer label (summarize.decided at level "answer"), whose name for an answer most likely
    wrong (closed.ERRED, retried in serving) is "asked"."""
    if kind == ERRED:
        return "asked"
    return ("flagged_" if kind == WARNED else "") + ("right" if right else "wrong")


def reference(name: str, probe: Path = PROBE) -> dict:
    """Each question's measured outcome: the last of its set's layers (REFERENCE_TAGS, in order) that measured it."""
    targets = list(DEMO_REFERENCE) if name == "demo" else [f"e2e_{tag}_stock_{name}.jsonl.gz" for tag in REFERENCE_TAGS[name]]
    held = {}
    for target in targets:
        with gzip.open(probe / "results" / target, "rt", encoding="utf-8") as source:
            held.update({row["id"]: row for row in map(json.loads, source) if row})
    return held


@dataclass(frozen=True)
class DemoItem:
    """One question of the demo gate (tests/test_datasets.py: each dataset's prompt and its standalone eval.txt
    follow-ups, chat lines left out), graded on rows as vtdemo was (run_connected.py grade)."""
    id: str
    question: str
    book: str
    expected: object
    fx: bool
    folder: Path
    alternate = None

    def grade(self, rows) -> bool:
        return demo_grade(self.expected, self.fx, rows)


def demo_items(limit=None) -> list[DemoItem]:
    import tests.test_datasets as gate
    items = []
    for name in sorted(folder.name for folder in gate.DATASET_DIR.iterdir() if folder.is_dir() and folder.name in gate.EXPECTED):
        folder = gate.DATASET_DIR / name
        kind, want = gate.EXPECTED[name]
        questions = [((folder / "prompt.txt").read_text(encoding="utf-8").strip(), want, kind == "world+fx")]
        questions += [(question, expected, fx) for question, expected, fx, chat_only in (gate._eval_cases(folder) or []) if not chat_only]
        items.extend(DemoItem(f"{name}#{at}", question, name, expected, fx, folder) for at, (question, expected, fx) in enumerate(questions))
    return items[:limit]


def _same(actual, expected, tolerance) -> bool:
    from engine.program.semantics import fold
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(float(actual) - float(expected)) <= tolerance
        except (TypeError, ValueError):
            return False
    return actual is not None and fold(str(actual)) == fold(str(expected))


def demo_grade(expected, fx, rows) -> bool:
    """A scalar is a 1 x 1 numeric answer within one cent (within the gate's FX tolerance for a conversion), a row set
    the expected rows in order (extra columns to the right ignored); an expected clarification is never an answer."""
    from tests.test_datasets import FX_TOLERANCE
    if expected is None or not rows:
        return False
    if isinstance(expected, (dict, list)):
        want = expected["rows"] if isinstance(expected, dict) else expected
        return len(rows) == len(want) and all(len(row) >= len(wanted) and all(_same(a, b, CENT) for a, b in zip(row, wanted))
                                              for row, wanted in zip(rows, want))
    if len(rows) != 1 or len(rows[0]) != 1:
        return False
    try:
        actual = float(rows[0][0])
    except (TypeError, ValueError):
        return False
    return math.isfinite(actual) and abs(actual - expected) <= (abs(expected) * FX_TOLERANCE if fx else CENT)


def maps_links(record: dict):
    """A dataset's links as build_maps.py recorded what production connected for it (maps/<dataset>.json): its routes
    and their bridges, entity columns, keys and money columns (engine/prepared.py RequestLinks)."""
    from engine.prepared import EntityLink, KeyLink, MoneyLink, PlaceLink, RequestLinks
    places = tuple(PlaceLink(r["upload"], r["column"], r["column_index"], r["world_type"], r["bridge"], r["bridged"], r["cells"], r["cells_matched"])
                   for r in record.get("routes", []))
    entities = tuple(EntityLink(e["upload"], e["column"], e["column_index"], e.get("label") or "", e.get("entity_table", ""), e.get("bridge", ""),
                                e["routed_as_place"], e["bridged"], e["cells_matched"], e["has_country"]) for e in record.get("entity_columns", []))
    normalized = {table["name"]: table for table in record.get("normalized", [])}
    keys = []
    for fk in record.get("links_between_uploads", []):
        source, target = normalized.get(fk.get("from_table")), normalized.get(fk.get("to_table"))
        if (source and target and isinstance(fk.get("from_col"), str) and isinstance(fk.get("to_col"), str)
                and fk["from_col"] in source["columns"] and fk["to_col"] in target["columns"]):
            keys.append(KeyLink(source["upload"], fk["from_col"], source["columns"].index(fk["from_col"]),
                                target["upload"], fk["to_col"], target["columns"].index(fk["to_col"])))
    money = tuple(MoneyLink(c["upload"], c["currency_index"], c["date_index"], tuple(m["column_index"] for m in c["measures"]),
                            tuple(c["rate_columns"]), c["as_of"]) for c in record.get("conversions", []))
    return RequestLinks({upload["name"]: tuple(upload["columns"]) for upload in record["uploads"]}, places, entities, tuple(keys), money)


class DemoRequests:
    """Each demo question as a served request reads it: its own request database (engine/prepared.py
    request_database, the knowledgebase's base image attached), the world layer's links registered on it
    (engine/knowledge_bridges.py link_request, as engine/knowledge.py registers them), then the one own-data selection
    over the dataset's tables as the gate sends them. The convert question's currency is recorded per question.

    With ``maps`` (the request databases production built for the demo on 2026-11-18, which demo25 read: build_maps.py's
    maps/<dataset>.json with its .main.sqlite bridges and .kb.sqlite knowledgebase part), each question's database
    holds that snapshot and its links are the recorded ones: the same selection, on the measurement's own inputs, so the
    knowledgebase's changes since (a day's exchange rate published after the snapshot) do not enter."""

    def __init__(self, maps: Path | None = None):
        import engine.program.answer as answer_module
        self.maps = maps
        if maps is None:
            from engine.knowledge_query import KnowledgeQuery
            self.world = KnowledgeQuery()
        else:
            self.world = TableQuery()
        self.tables = {}
        self.converted = None
        held = answer_module.convert_target

        def recorded(*args, **kwargs):
            self.converted = held(*args, **kwargs)
            return self.converted
        answer_module.convert_target = recorded

    def select(self, item: DemoItem):
        import tests.test_datasets as gate
        from engine.prepared import request_database
        from engine.relations import request_memo
        from engine.request_state import request_scope
        if item.book not in self.tables:
            self.tables[item.book] = gate._tables(item.folder)
        self.converted = None
        if self.maps is not None:
            record = json.loads((self.maps / f"{item.book}.json").read_text(encoding="utf-8"))
            with scratch_database() as database:
                database.connection.deserialize((self.maps / f"{item.book}.main.sqlite").read_bytes())
                database.connection.deserialize((self.maps / f"{item.book}.kb.sqlite").read_bytes(), name="knowledgebase")
                database.link_with(lambda _tables: maps_links(record))
                return self.world.select_query(item.question, normalize_tables(self.tables[item.book]))
        with request_scope(), request_memo(), request_database():
            self.world.begin_request()
            try:
                self.world.link_request(DEMO_AS_OF)
                return self.world.select_query(item.question, normalize_tables(self.tables[item.book]))
            finally:
                self.world.end_request()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sets", default=",".join(SETS))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--tag", default="parity")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--resume", action="store_true", help="skip questions already recorded (the stock reader's history then differs)")
    parser.add_argument("--csv", action="store_true", help="each tab rendered to CSV and read as an upload is (a new measurement, not parity)")
    parser.add_argument("--record", type=Path, help="a cache folder: a body neither cache holds is sent to the Decisions API and kept here")
    parser.add_argument("--demo-maps", type=Path, help="the demo set on the request databases demo25 read (build_maps.py's maps folder)")
    parser.add_argument("--retry", type=Path, help="retry each erred question as serving does; the retry model's answers are kept here")
    args = parser.parse_args()
    names = args.sets.split(",")
    if any(name not in SETS for name in names):
        raise SystemExit(f"sets are {SETS}")
    torch.set_num_threads(args.threads)
    out = ROOT / "regress" / "results" / "dev_decisions" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    replay = Replay(PROBE / "cache", out / "misses", args.record)
    llm.DECISIONS_TRANSPORT = replay
    query_replay = None
    if args.retry is not None:
        query_replay = QueryReplay(args.retry)
        llm.QUERY_TRANSPORT = query_replay
    planner = TableQuery()
    run_e2e, summarize = experiment_modules()
    totals = {}
    demo = DemoRequests(args.demo_maps) if "demo" in names else None
    for name in names:
        items = demo_items(args.limit) if name == "demo" else items_of(name, run_e2e, args.limit)
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
                    if name == "demo":
                        selection = demo.select(item)
                        row["convert_to"] = demo.converted
                    else:
                        if item.book != book:
                            book, tables = None, None
                            request.close()
                            tables = tables_of(item, args.csv)
                            request.enter_context(scratch_database())
                            book = item.book
                        selection = planner.select_query(item.question, tables)
                    shown = selection.selected if selection.selected is not None else selection.erred
                    rows = selection.rows[shown] if shown is not None else ()
                    kind = kind_of(selection)
                    listed_right = [graded(item, listed) for listed in selection.rows]
                    row.update(kind=kind, label=label_of(kind, graded(item, rows)), mass=selection.confidence, listed=len(selection.rows),
                               listed_answer=[answer_key(listed) for listed in selection.rows], listed_right=listed_right,
                               choose_p=selection.probabilities, read_as=selection.pool[shown].evidence[0] if shown is not None else "")
                    if query_replay is not None and kind == ERRED and name != "demo":
                        row["retry"] = retry_row(planner, item, selection)
                except ReplayMiss as miss:
                    book, tables = None, None
                    request.close()
                    row.update(kind=ERRED, label="asked", miss=str(miss), listed_answer=None, choose_p=None, listed_right=None)
                except Exception as error:     # the measurement recorded a failed question as asked, with its error
                    book, tables = None, None
                    request.close()
                    row.update(kind=ERRED, label="asked", error=f"{type(error).__name__}: {str(error)[:160]}", listed_answer=[], choose_p=None, listed_right=[])
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
                        if name == "demo" and row.get("convert_to") != theirs.get("convert_to"):
                            differs.append(f"currency {row.get('convert_to')} vs {theirs.get('convert_to')}")
                row["differs"] = differs
                counts["n"] += 1
                counts["misses"] += row["miss"] is not None
                counts["errors"] += row["error"] is not None
                counts["mismatches"] += bool(differs) and row["miss"] is None
                counts["labels"][row["label"]] = counts["labels"].get(row["label"], 0) + 1
                if "retry" in row:
                    retried = counts.setdefault("retry", {})
                    retried[row["retry"]["outcome"]] = retried.get(row["retry"]["outcome"], 0) + 1
                sink.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                sink.flush()
                if row["miss"] is not None:
                    print(f"MISS {name} {item.id}: {row['miss']} (body in {out / 'misses'})", flush=True)
                elif differs:
                    print(f"MISMATCH {name} {item.id}: {'; '.join(differs)}", flush=True)
                if (at + 1) % 25 == 0:
                    print(f"{name} {at + 1}/{len(items)} {round(time.time() - started)}s misses {counts['misses']} mismatches {counts['mismatches']}", flush=True)
        counts["seconds"] = round(time.time() - started, 1)
        if query_replay is not None:
            counts["retry_tokens_sent"] = list(query_replay.tokens)
        totals[name] = counts
        print(json.dumps({"set": name, **counts}), flush=True)
    (out / f"summary_{'_'.join(names)}.json").write_bytes((json.dumps(totals, indent=1) + "\n").encode("utf-8"))
    clean = all(counts["misses"] == 0 and counts["mismatches"] == 0 for counts in totals.values())
    print("parity" if clean else "NOT at parity", flush=True)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
