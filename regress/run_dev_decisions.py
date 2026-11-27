"""The dev replay: the Decisions path through the one own-data selection on the 896 dev questions (messy dev, the
WikiTableQuestions dev sample, DataBench dev and our fresh dev set; regress/eval_sets.py loads them) and the demo gate's
91 questions, each held to its measured reference. No frozen set is read.

Every question runs through engine/tables.py TableQuery.select_query (no active sheet) on the request tables of its
workbook, each table's grid the tab its loader built; engine.llm.DECISIONS_TRANSPORT is replaced by a replay of the
recorded answers (regress/sets/calls, keyed by engine.llm.decision_hash): the API is never called, no key is read, and a
body the store does not hold is a miss, its body written to <out>/misses/<hash>.json for a diff. Each question is then
compared with its set's reference (regress/sets/dev_decisions/reference/<set>.jsonl.gz, the last kept measurement of
every question): the listed answers (answer hashes, in order), their grades, the choice probabilities, the served
label, any error and, on the demo set, the currency the convert question chose.

A workbook's questions share one request database (engine/prepared.py scratch_database), as a conversation's turns
share a prepared workbook. The stock reader keeps the vectors of the texts it read before, and a batch's padding can
move a vector in its last bits, so a run keeps the reference's history by its order, at --threads 4: --sets wtq and
--sets messy,freshdev and --sets databench, one process each (and --sets demo). Results:
regress/results/dev_decisions/<tag>/<set>.jsonl and summary_<sets>.json.

A kept change is measured with --record (a body the store lacks is sent to the Decisions API, with the deployment's
GOOGLE_API_KEY, and kept in regress/sets/calls/<date>-<tag>-<sets>-<pid>.jsonl, one batch per process) and --keep (the run's records become the sets'
references); then python -m regress.eval_sets pack compresses the new calls, and the references and calls are
committed with the change.

--sets demo is the demo gate (tests/test_datasets.py: each dataset's prompt and its standalone eval.txt follow-ups),
graded on rows: each question is a served request's own-data selection, on its own request database with the world
layer's links registered (engine/knowledge_bridges.py link_request), so it reads the live knowledgebase. --demo-maps
reads each dataset instead on the request database production built for it on 2026-11-18 (regress/sets/demo_maps),
with the links recorded there: the reference's inputs, so the knowledgebase's changes since do not enter.

--csv is a new measurement, not parity: each tab is rendered to CSV text and read as an upload is (csv_table,
canonical table names); its bodies differ, so it needs --record.

The second judge (engine/llm.py judge, JUDGE_TRANSPORT) is replayed the same way from regress/sets/judge_calls: an
answer the choice gives unmarked is labelled as its two judges leave it (``doubt``: why the second one warned it).
With --record a judge's call that fails is sent again (three attempts, each as served), since the reference keeps the
judge's answer and not one run's timeout; a set with a miss is not kept and is run again, its recorded bodies replayed.
--unjudged asks no second judge (a change measured for what it builds and picks, not for its marks): an answer is
labelled as the choice alone leaves it (``judged`` false), and compared with the reference's label as the choice alone
left that one.

--retry measures serving's retry (engine/retry.py): each question whose answer is most likely wrong (closed.ERRED) is
retried over its workbook's tables as TableQuery.serve retries it, and its rows are graded (right, wrong, the sheet
cannot answer it, or the retry failed); the retry model's answers are kept by body hash in regress/sets/retry_calls.

usage: python -X utf8 -m regress.run_dev_decisions [--sets messy,freshdev] [--limit N] [--tag NAME] [--threads 4] [--resume]
                                                   [--record] [--keep] [--csv] [--sets demo [--demo-maps [DIR]]] [--retry]
"""
from __future__ import annotations

import argparse
import datetime
import gzip
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
from engine.program.closed import DOUBT_UNREAD, answer_key
from engine.request_validation import canonical_table_name
from engine.tables import TableQuery, csv_table, normalize_tables
from regress import eval_sets

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = eval_sets.SETS / "dev_decisions" / "reference"
DEMO_MAPS = eval_sets.SETS / "demo_maps"
RETRY_CALLS = eval_sets.SETS / "retry_calls"
JUDGE_CALLS = eval_sets.SETS / "judge_calls"
SETS = ("messy", "wtq", "databench", "freshdev", "demo")
# The fields of a record that the reference keeps (its timing and comparison are the run's own)
KEPT = ("id", "set", "kind", "label", "mass", "listed", "listed_answer", "listed_right", "choose_p", "read_as", "error", "miss", "convert_to", "calls", "doubt", "judged", "judge_call")
DEMO_AS_OF = "2026-11-18"
CENT = 0.01                 # run_connected.py: a scalar within one cent, a row's cells to the cent


class ReplayMiss(RuntimeError):
    """A request body the recorded answers do not hold."""


class Replay:
    """engine.llm's Decisions transport over the recorded answers (eval_sets.CallStore), by body hash. Without ``record``
    it never calls the API; with it, a body the store lacks is sent through engine.llm's own transport and its answer
    kept in this run's batch, so the run can be replayed."""

    def __init__(self, store: eval_sets.CallStore, misses: Path, record: bool = False):
        self.store, self.misses, self.record = store, misses, record
        self.send = llm.DECISIONS_TRANSPORT
        self.used: list[str] = []
        self.judge_call: str | None = None     # the judge's body hash for the question in hand, from its choice call

    def __call__(self, payload: bytes) -> dict:
        digest = llm.decision_hash(payload)
        self.judge_call = eval_sets.judge_call_of(payload) or self.judge_call
        held = self.store.get(digest)
        if held is None and self.record:
            answer = self.send(payload)
            held = {"answers": answer["answers"], "tokens": answer["tokens"]}
            self.store.keep(digest, held)
        if held is None:
            self.misses.mkdir(parents=True, exist_ok=True)
            (self.misses / f"{digest}.json").write_bytes(payload)
            raise ReplayMiss(digest)
        self.used.append(digest)
        return {"answers": held["answers"], "tokens": held["tokens"]}


class JudgeReplay:
    """engine.llm's judge transport (JUDGE_TRANSPORT) over the recorded judge answers by body hash, as Replay is the
    Decisions transport's: without ``record`` a body the store lacks is a miss; with it, the body is sent through
    engine.llm's own transport and its answer kept. Serving asks the judge once, within its seconds, and warns the
    answer when that call fails; a recording wants the judge's answer, so a call that fails is sent again (ATTEMPTS in
    all, each one as served), and only a body that never answers is the question's miss. The selection asks the judge
    beside its choice for every question and reads its answer only for an answer the choice gives unmarked, so a miss
    reaches the question (ReplayMiss) only there. Every record with a listing names its judge's call
    (``judge_call``, eval_sets.judge_call_of over the choice's own body), read or not, so the judges' answers on
    warned and erred answers can be read from the store without asking again."""

    ATTEMPTS = 3

    def __init__(self, store: eval_sets.CallStore, record: bool = False):
        self.store, self.record, self.send = store, record, llm.JUDGE_TRANSPORT

    def __call__(self, payload: bytes) -> dict:
        digest = llm.decision_hash(payload)
        held = self.store.get(digest)
        if held is None and self.record:
            held = self._asked(payload)
            self.store.keep(digest, held)
        if held is None:
            raise ReplayMiss(digest)
        return held

    def _asked(self, payload: bytes) -> dict:
        for left in range(self.ATTEMPTS - 1, -1, -1):
            try:
                return self.send(payload)
            except llm.LLMUnavailable:
                if not left:
                    raise


class QueryReplay:
    """engine.llm's retry transport (QUERY_TRANSPORT) over the recorded retry answers by body hash: a body the store
    lacks is sent to the retry model and its answer kept. ``tokens`` sums the input and output tokens sent anew."""

    def __init__(self, store: eval_sets.CallStore):
        self.store, self.send = store, llm.QUERY_TRANSPORT
        self.tokens = [0, 0]

    def __call__(self, payload: bytes) -> dict:
        digest = llm.decision_hash(payload)
        held = self.store.get(digest)
        if held is None:
            held = self.send(payload)
            usage = held.get("usage") or {}
            self.tokens[0] += int(usage.get("promptTokenCount") or 0)
            self.tokens[1] += int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)
            self.store.keep(digest, held)
        return held


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


def items_of(name, limit):
    """A dev set's questions (regress/eval_sets.py), the reference's order."""
    with capturing():
        source = (eval_sets.messy() if name == "messy" else eval_sets.wtq_dev_sample() if name == "wtq"
                  else eval_sets.fresh_dev() if name == "freshdev" else eval_sets.databench("dev", max_rows=60000))
    return [item for item in source if item.label == "gold"][:limit]


def graded(item, rows) -> bool:
    result = list(rows)
    return bool(result) and bool(item.grade(result) or (item.alternate is not None and item.alternate(result)))


def pick_right(row: dict) -> bool:
    """The always-answering pick: the answer the choice holds likeliest (its probability summed over the listed answers
    that return it, the earlier listing first among equals), right."""
    answers, right, p = row.get("listed_answer") or [], row.get("listed_right") or [], row.get("choose_p") or {}
    mass = {}
    for at, answer in enumerate(answers):
        mass[answer] = mass.get(answer, 0.0) + p.get(f"r{at:02d}", 0.0)
    if not mass:
        return False
    best = min(mass, key=lambda answer: (-mass[answer], answers.index(answer)))
    return bool(right[answers.index(best)])


def gate(rows: list[dict], held: dict) -> dict:
    """A run against its reference on the same questions: built (some listed answer right), the always-answering pick,
    its wins and losses, and the unwarned wrong answers, each before and after."""
    counts = {"built": 0, "reference_built": 0, "pick": 0, "reference_pick": 0, "wins": 0, "losses": 0, "unwarned_wrong": 0, "reference_unwarned_wrong": 0}
    for row in rows:
        theirs = held.get(row["id"])
        if theirs is None:
            continue
        mine_pick, their_pick = pick_right(row), pick_right(theirs)
        counts["built"] += any(row.get("listed_right") or [])
        counts["reference_built"] += any(theirs.get("listed_right") or [])
        counts["pick"] += mine_pick
        counts["reference_pick"] += their_pick
        counts["wins"] += mine_pick and not their_pick
        counts["losses"] += their_pick and not mine_pick
        counts["unwarned_wrong"] += row.get("label") == "wrong"
        counts["reference_unwarned_wrong"] += theirs.get("label") == "wrong"
    return counts


def label_of(kind: str, right: bool) -> str:
    """The per-answer label; an answer most likely wrong (closed.ERRED, retried in serving) is "asked"."""
    if kind == ERRED:
        return "asked"
    return ("flagged_" if kind == WARNED else "") + ("right" if right else "wrong")


def choice_label(row: dict) -> str:
    """A record's label as the Decisions choice alone leaves it: an answer only the second judge warned (``doubt``) is
    right or wrong unflagged."""
    label = row["label"]
    return label.removeprefix("flagged_") if row.get("doubt") else label


def reference(name: str, folder: Path = REFERENCE) -> dict:
    """Each question's reference: its record in the set's last kept measurement."""
    with gzip.open(folder / f"{name}.jsonl.gz", "rt", encoding="utf-8") as source:
        return {row["id"]: row for row in map(json.loads, source) if row}


def keep_reference(name: str, rows: list[dict], folder: Path = REFERENCE) -> None:
    """A kept change's records as the set's reference: the fields KEPT, one line each, no timestamp in the archive."""
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{name}.jsonl.gz", "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as sink:
        for row in rows:
            sink.write((json.dumps({key: row.get(key) for key in KEPT if key in row}, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


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
                return self.world.select_query(item.question, normalize_tables(self.tables[item.book]), judged=True)
        with request_scope(), request_memo(), request_database():
            self.world.begin_request()
            try:
                self.world.link_request(DEMO_AS_OF)
                return self.world.select_query(item.question, normalize_tables(self.tables[item.book]), judged=True)
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
    parser.add_argument("--record", action="store_true", help="a body the recorded answers lack is sent to the Decisions API and kept in regress/sets/calls")
    parser.add_argument("--keep", action="store_true", help="the run's records become each measured set's reference (a kept change)")
    parser.add_argument("--demo-maps", type=Path, nargs="?", const=DEMO_MAPS, help="the demo set on the request databases built for it on 2026-11-18 (default regress/sets/demo_maps)")
    parser.add_argument("--retry", action="store_true", help="retry each erred question as serving does; the retry model's answers are kept in regress/sets/retry_calls")
    parser.add_argument("--unjudged", action="store_true", help="ask no second judge: each answer labelled as the choice alone leaves it")
    args = parser.parse_args()
    names = args.sets.split(",")
    if any(name not in SETS for name in names):
        raise SystemExit(f"sets are {SETS}")
    if args.keep and (args.limit or args.resume):
        raise SystemExit("a reference is a whole set measured in one run: --keep takes neither --limit nor --resume")
    torch.set_num_threads(args.threads)
    out = ROOT / "regress" / "results" / "dev_decisions" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    # one batch per process: the sets of one tag run in parallel processes, and two appending to one file interleave
    batch = f"{datetime.date.today().isoformat()}-{args.tag}-{'_'.join(args.sets.split(','))}-{os.getpid()}"
    replay = Replay(eval_sets.CallStore(eval_sets.CALLS, batch), out / "misses", args.record)
    llm.DECISIONS_TRANSPORT = replay
    judge_replay = None if args.unjudged else JudgeReplay(eval_sets.CallStore(JUDGE_CALLS, batch), args.record)
    llm.JUDGE_TRANSPORT = eval_sets.judge_unasked if args.unjudged else judge_replay
    query_replay = None
    if args.retry:
        query_replay = QueryReplay(eval_sets.CallStore(RETRY_CALLS, batch))
        llm.QUERY_TRANSPORT = query_replay
    planner = TableQuery()
    totals = {}
    demo = DemoRequests(args.demo_maps) if "demo" in names else None
    for name in names:
        items = demo_items(args.limit) if name == "demo" else items_of(name, args.limit)
        held = reference(name)
        target = out / f"{name}.jsonl"
        done = set()
        if args.resume and target.exists():
            done = {json.loads(line)["id"] for line in target.read_text(encoding="utf-8").splitlines() if line.strip()}
        counts = {"n": 0, "misses": 0, "errors": 0, "mismatches": 0, "labels": {}, "reference_labels": {}}
        measured = []
        started = time.time()
        book, tables = None, None
        request = contextlib.ExitStack()
        with request, open(target, "a" if args.resume else "w", encoding="utf-8", newline="\n") as sink:
            for at, item in enumerate(items):
                if item.id in done:
                    continue
                replay.used, replay.judge_call = [], None
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
                        selection = planner.select_query(item.question, tables, judged=True)
                    shown = selection.selected if selection.selected is not None else selection.erred
                    rows = selection.answer(shown) if shown is not None else ()
                    kind = kind_of(selection)
                    if selection.doubt == DOUBT_UNREAD:     # no second judge answered: unasked, or its call failed
                        if not args.unjudged:
                            raise ReplayMiss("the second judge's call failed")
                        kind = ANSWERED
                    listed_right = [graded(item, selection.answer(at)) for at in range(len(selection.rows))]
                    row.update(kind=kind, label=label_of(kind, graded(item, rows)), mass=selection.confidence, listed=len(selection.rows),
                               listed_answer=[answer_key(listed) for listed in selection.rows], listed_right=listed_right,
                               choose_p=selection.probabilities, read_as=selection.pool[shown].evidence[0] if shown is not None else "",
                               doubt=None if args.unjudged else selection.doubt, judged=not args.unjudged)
                    if judge_replay is not None and replay.judge_call is not None:
                        row["judge_call"] = replay.judge_call
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
                row["calls"] = sorted(replay.used)     # the calls beside the reading call finish in any order
                row["seconds"] = round(time.perf_counter() - clock, 3)
                theirs = held.get(item.id)
                differs = []
                if theirs is None:
                    differs.append("no reference outcome")
                else:
                    reference_label = theirs["label"]
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
                        # the labels as served where both runs asked the second judge, else as the choice alone leaves them
                        both = bool(row.get("judged")) and bool(theirs.get("judged"))
                        mine, recorded = (row["label"], reference_label) if both else (choice_label(row), choice_label(theirs))
                        if mine != recorded:
                            differs.append(f"label {mine} vs {recorded}")
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
                measured.append(row)
                if row["miss"] is not None:
                    print(f"MISS {name} {item.id}: {row['miss']} (body in {out / 'misses'})", flush=True)
                elif differs:
                    print(f"MISMATCH {name} {item.id}: {'; '.join(differs)}", flush=True)
                if (at + 1) % 25 == 0:
                    print(f"{name} {at + 1}/{len(items)} {round(time.time() - started)}s misses {counts['misses']} mismatches {counts['mismatches']}", flush=True)
        counts["seconds"] = round(time.time() - started, 1)
        counts["gate"] = gate(measured, held)
        if query_replay is not None:
            counts["retry_tokens_sent"] = list(query_replay.tokens)
        totals[name] = counts
        print(json.dumps({"set": name, **counts}), flush=True)
        if args.keep and counts["misses"]:     # the sets after it are still measured; this one is run again
            print(f"NOT kept {name}'s reference: a reference is a whole set measured in one run with no miss ({counts['misses']} here)", flush=True)
        elif args.keep:
            keep_reference(name, measured)
            print(f"kept {name}'s reference ({len(measured)} questions)", flush=True)
    (out / f"summary_{'_'.join(names)}.json").write_bytes((json.dumps(totals, indent=1) + "\n").encode("utf-8"))
    clean = all(counts["misses"] == 0 and counts["mismatches"] == 0 for counts in totals.values())
    print("parity" if clean else "NOT at parity", flush=True)
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
