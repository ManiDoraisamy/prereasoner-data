"""The whole engine, offline: sheet model, index, reader, decoder, verifier, chooser, answer or ask, on the 186 studied questions (dev) and on validation, graded by evals/enron/enron_grade.py. Reports right/asked/wrong, top-10 link recall, p50 and p99 question time, index build time, memory, and the survival tests (reorder, 20 added columns, renames, unreadable headers). Two modes on one model: named-only, and named-plus-open with similarity allowed to decide, for the comparison the owner was promised.

Writes a report with the code commit, dirty state, dataset identity, bundle hash and flags. Prints counts, never a cell or a question.

usage: python -m training.named.evaluate --set val --bundle <dir> [--limit N] [--survival 200] [--modes named,named_plus_open] [--folds 5] --out <report.json>
       python -m training.named.evaluate --set studied|messy --bundle <dir> --chooser <the validation report> --out <report.json>
       --words: the floor, the reading from words alone with no model
       --readers <dir>: today's readers propose beside the decoder (engine/named/proposals.py), over the readers the bundle names; --reader-cache <file.pkl.gz> keeps their proposals per question and sheet, so a second run reads none again
The funnel counts, of gold questions, a right program listed, ranked first by the chooser and served; and the same through the chooser over only the decoder's candidates and over only the readers'.
On validation, --folds decides each question by a chooser calibrated on the other folds (training/named/choose.py:cross_fit); the fit of all folds is written beside the report for the sets read after it.
Each question's misses are put in a stage: compile (the gold program itself no longer gives the gold answer), links (a gold link outside the top 10), decode (the gold links stand and no right program reached the top 50), verify (the gold program is listed and its result is not graded right), choose (a right candidate was listed and not answered with).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import re
import json
import os
import pickle
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import psutil
import torch

from engine.named.chooser import ANSWER, Chooser
from engine.named.links import named_matrix
from engine.named.pipeline import NamedEngine, Prepared, load_bundle
from engine.named.reader import MODE_NAMED, MODE_NAMED_PLUS_OPEN, NamedReading
from engine.named.units import T_BAND, T_COLUMN, T_LINE, T_VALUE, Target
from engine.program.catalog import Catalog
from engine.program.decoder import Decoder
from engine.program.names import EV_EMPTY, EV_SHAPE_AGREES
from engine.program.semantics import fold
from engine.program.store import RequestTables
from engine.program.verifier import verify
from engine.program.words import predict_answer_type, read_question
from training.named.augment import SURVIVAL_OPS, mutate_catalog
from training.named.choose import SUPPORT_ALL, SUPPORT_NO_LINKS, answer_key, calibrated, cross_fit, decision_of, fold_of, records_of

ENRON = "C:/work/prereasoner-readers-artifacts/evals/enron/"
DESIGN = ENRON + "design/"
FACTORY_OUT = "C:/work/prereasoner-readers-artifacts/factory/out/"
FACTORY_MODELS = "C:/work/prereasoner-readers-artifacts/factory/models/"
STUDIED_SHEET_COMMIT = "584f9cce27f163bbea61bb557f3711c03837ffe4"
MODE_WORDS = "words"
RIGHT = "right"
ASKED = "asked"
WRONG = "wrong"
GATE_A = {"right": 0.45, "wrong": 0.15, "link_top10": 0.90, "p50_ms": 300.0, "reorder": 0.99, "added_columns": 0.95, "renames": 0.90, "unreadable_headers": 0.70}
STOP_RIGHT = 0.35
STOP_WRONG = 0.15


@dataclass
class Item:
    """One question, however a set finds its workbook and grades it. label: gold, ambiguous or refusal; alternate grades the second reading of an ambiguous one."""
    id: str
    question: str
    book: str
    family: str
    form: str
    label: str
    load: Callable[[], Catalog]
    grade: Callable[[list], bool]
    alternate: Callable[[list], bool] | None
    links: list[dict]
    program: str = ""


def record_of(answer: list, form: str | None) -> dict:
    """A gold result as enron_grade reads a record: a figure, a name, or several names. A rank's figure is accepted beside its key, as "highest charge" may ask for the charge."""
    cells = [cell for row in answer for cell in row if cell not in (None, "")]
    if not answer or not cells:
        return {"expected": None, "alternates": {}, "accept_text": [], "tolerance": 0}
    if len(answer) > 1:
        first = [row[0] for row in answer if row and row[0] not in (None, "")]
        if all(isinstance(cell, (int, float)) for cell in first):
            return {"expected": first, "alternates": {}, "accept_text": [], "tolerance": 0}
        return {"expected": [str(cell) for cell in first], "alternates": {}, "accept_text": [], "tolerance": 0}
    alternates = {}
    if form == "rank" and len(answer[0]) > 1 and isinstance(answer[0][-1], (int, float)):
        alternates["figure"] = answer[0][-1]
    return {"expected": cells[0], "alternates": alternates, "accept_text": [], "tolerance": 0}


def enron_grader() -> Callable[[dict, list], bool]:
    """evals/enron/enron_grade.py, the grader of every Enron number."""
    if ENRON not in sys.path:
        sys.path.insert(0, ENRON)
    from enron_grade import right_answer
    return lambda record, rows: bool(right_answer(record, None, [list(row) for row in rows]))


def _book_loader(book: str) -> Callable[[], Catalog]:
    def load() -> Catalog:
        with gzip.open(f"{FACTORY_MODELS}{book}.pkl.gz", "rb") as source:
            return Catalog.from_model(pickle.load(source))
    return load


def load_factory_set(side: str, *, limit: int | None = None, families: Iterable[str] | None = None, grade: Callable | None = None, out_dir: str = FACTORY_OUT, over_quota: bool = False) -> list[Item]:
    """A factory side's questions, in book order, or several sides' by commas. A question id's hash draws a limit, so a small run is a fair sample."""
    grade = grade or enron_grader()
    wanted = set(families) if families is not None else None
    rows = []
    for one in side.split(","):
        for name in (f"{one}.jsonl", f"{one}.over_quota.jsonl") if over_quota else (f"{one}.jsonl",):
            if not os.path.exists(f"{out_dir}{name}"):
                continue
            with open(f"{out_dir}{name}", encoding="utf-8") as source:
                for line in source:
                    row = json.loads(line)
                    if row.get("side") != one:
                        raise ValueError("a row of another side")
                    if wanted is None or row["family"] in wanted:
                        rows.append(row)
    if limit:
        rows = sorted(rows, key=lambda row: hashlib.blake2b(row["id"].encode(), digest_size=8).digest())[:limit]
    rows.sort(key=lambda row: (row["book"], row["id"]))
    items = []
    for row in rows:
        record = record_of(row.get("answer") or [], row.get("form"))
        second = (row.get("second_reading") or {}).get("answer")
        alternate = None
        if row["label"] == "ambiguous" and second is not None:
            shaped = second if isinstance(second, list) and second and isinstance(second[0], list) else [[second]] if not isinstance(second, (list, dict)) else None
            if shaped is not None:
                other = record_of(shaped, row.get("form"))
                alternate = lambda rows, other=other: grade(other, rows)
        items.append(Item(row["id"], row["question"], row["book"], row["family"], row.get("form") or "", row["label"], _book_loader(row["book"]), lambda rows, record=record: grade(record, rows), alternate, row.get("links") or [], row.get("program") or ""))
    return items


def load_studied(design: str = DESIGN) -> list[Item]:
    """The 186 hand-graded questions: dev, the language was written from them. Read through design/phase2_common.py: its cached compiled models and its is_right, enron_grade over the author's gold."""
    os.environ.setdefault("SHEET_COMMIT", STUDIED_SHEET_COMMIT)
    if design not in sys.path:
        sys.path.insert(0, design)
    from phase2_common import GOLD, IDS, QUESTIONS, catalog_of, is_right
    items = [Item(i, QUESTIONS[i], str(GOLD[i]["workbook_n"]), str(GOLD[i]["file"]), "", "gold", lambda n=GOLD[i]["workbook_n"]: catalog_of(n), lambda rows, i=i: is_right(i, rows), None, []) for i in IDS]
    return sorted(items, key=lambda item: (int(item.book), item.id))


_NUMERIC = re.compile(r"-?\d+(?:\.\d+)?")


def _typed(cell: object) -> object:
    """A benchmark workbook writes every cell as text, as a text upload does: a plain number reads as a number; "$1,234.50" and "25%" stay as written, and the sheet's reader takes them from there."""
    if isinstance(cell, str) and _NUMERIC.fullmatch(cell.strip()):
        return float(cell)
    return cell


def load_messy(split: str = "dev") -> list[Item]:
    """regress/messy_cases.py's questions on its seeded workbooks, graded by its own grade. The frozen split is run later, once, and never from here."""
    if split != "dev":
        raise ValueError("the frozen split is not read here")
    from engine.sheet import compile_workbook, workbook_from_json
    from regress.messy_cases import cases, grade as messy_grade
    from regress.messy_workbooks import workbook

    def load(name: str) -> Catalog:
        tabs = [{"name": tab["name"], "rows": [list(tab["columns"]), *[[_typed(cell) for cell in row] for row in tab["rows"]]]} for tab in workbook(name)]
        return Catalog.from_model(compile_workbook(workbook_from_json({"name": name, "tabs": tabs})))

    def right(expected, rows) -> bool:
        return messy_grade(expected, {"result": {"columns": [], "rows": rows}})[0] == "right"

    return [Item(f"messy{at}", case.question, case.workbook, case.workbook, case.family, "gold", lambda name=case.workbook: load(name), lambda rows, expected=case.expected: right(expected, rows), None, []) for at, case in enumerate(cases()) if case.split == "dev"]


def words_outcome(item: Item, prepared: Prepared, k: int = 50) -> tuple[str, tuple, float]:
    """The floor: words alone, the first program passing the shape check, or ask."""
    started = time.perf_counter()
    reading = read_question(item.question, prepared.catalog, prepared.decoder)
    decoded = prepared.decoder.decode(reading, k=k)
    checked = verify(decoded.programs, prepared.tables, predict_answer_type(item.question), top=k)
    seconds = time.perf_counter() - started
    for candidate in checked:
        if candidate.result is not None and candidate.result.rows and candidate.held(EV_SHAPE_AGREES) and not candidate.held(EV_EMPTY):
            return ANSWER, candidate.result.rows, seconds
    return "ask", (), seconds


def graded(item: Item, kind: str, rows: tuple) -> str:
    """right, asked or wrong. A gold question answered must match; an ambiguous one is right to ask and right to answer either reading; asking is right of a refusal and answering it is wrong."""
    if item.label == "refusal":
        return RIGHT if kind != ANSWER else WRONG
    if kind != ANSWER:
        return RIGHT if item.label == "ambiguous" else ASKED
    if item.grade(list(rows)) or (item.alternate is not None and item.alternate(list(rows))):
        return RIGHT
    return WRONG


def graded_choice(item: Item, kind: str, right: bool) -> str:
    """The same reading as graded, from a record whose answer was graded when it was read."""
    if item.label == "refusal":
        return RIGHT if kind != ANSWER else WRONG
    if kind != ANSWER:
        return RIGHT if item.label == "ambiguous" else ASKED
    return RIGHT if right else WRONG


def stage_of(item: Item, outcome, prepared: Prepared, ranks: list[int], right_listed: bool) -> str:
    """Where a gold question's right answer was lost. Reads the gold program only here, never for a decision."""
    if right_listed:
        return "choose"
    if not item.program:
        return "unknown"
    from training.factory.grammar import Unbound, bind
    from engine.program.execute import run
    try:
        gold = bind(item.program, prepared.catalog)
        if not item.grade(list(run(gold, prepared.tables).rows)):
            return "compile"
    except (Unbound, ValueError, KeyError, RuntimeError):
        return "compile"
    if any(rank >= 10 for rank in ranks):
        return "links"
    if any(candidate.verified.scored.program == gold for candidate in outcome.candidates):
        return "verify"
    return "decode"


def percentile(values: list[float], share: float) -> float | None:
    return round(float(np.percentile(values, share)), 2) if values else None


def link_rank(named: NamedReading, engine: NamedEngine, prepared: Prepared, link: dict) -> int | None:
    """Where a gold link's target ranks among every target of its kind on its table, by link(span, target). None when the target is not one a word links to."""
    view = prepared.index.views.get(link.get("table"))
    span = [at for at in link.get("span", ()) if at < len(named.read.words)]
    kind = {"column": T_COLUMN, "value": T_VALUE, "line": T_LINE, "band": T_BAND}.get(link.get("kind"))
    if view is None or not span or kind is None:
        return None
    if kind == T_COLUMN:
        gold = (link.get("table"), T_COLUMN, link.get("position"))
    elif kind == T_VALUE:
        if link.get("target") is None:
            return None
        gold = (link.get("table"), T_VALUE, link.get("position"), fold(link["target"]))
    else:
        if link.get("target") is None:
            return None
        gold = (link.get("table"), kind, -1, fold(link["target"]))
    rows = [at for at, target in enumerate(view.targets) if target.kind == kind]
    if not rows:
        return None
    question = named.read.coordinates[span].mean(axis=0)[None]
    scores = named_matrix(question, view.ids[rows], view.values[rows], engine.weights.coordinates)[0]
    at_gold = None
    totals = []
    for row, score in zip(rows, scores.tolist()):
        target: Target = view.targets[row]
        evidence = named.book.get(target.coordinates()[0])
        total = score + (sum(hit.value for hit in evidence.rules) if evidence is not None else 0.0)
        if named.mode == MODE_NAMED_PLUS_OPEN and evidence is not None:
            total += engine.weights.open_weight * max(evidence.open, 0.0)
        totals.append(round(total, 4))
        key = target.key[:3] if kind == T_COLUMN else target.key
        if key == gold:
            at_gold = len(totals) - 1
    if at_gold is None:
        return None
    return sum(1 for total in totals if total > totals[at_gold])


class Runner:
    """One workbook prepared at a time: the machine has little room to spare, and tables load on first use."""

    def __init__(self, engine: NamedEngine | None):
        self.engine = engine
        self.book = None
        self.prepared: Prepared | None = None
        self.index_seconds: list[float] = []
        self.index_bytes: list[int] = []

    def prepare(self, item: Item, mutate: Callable | None = None) -> Prepared:
        """One workbook at a time, as it is or through one op: a Runner holds one op for its whole life."""
        if self.book == item.book and self.prepared is not None:
            return self.prepared
        self.close()
        catalog = item.load()
        if mutate is not None:
            catalog = mutate(catalog)
        if self.engine is None:
            self.prepared = Prepared(catalog, Decoder(catalog), RequestTables(catalog), None)
        else:
            self.prepared = self.engine.prepare(catalog)
            if self.prepared.index.built:
                self.index_seconds.append(self.prepared.index_seconds)
                self.index_bytes.append(sum(len(tab.to_bytes()) for tab in self.prepared.index.tabs))
        self.book = item.book
        return self.prepared

    def close(self) -> None:
        if self.prepared is not None:
            self.prepared.tables.connection.close()
        self.prepared = None
        self.book = None


def peak_gb() -> float:
    info = psutil.Process().memory_info()
    return round(getattr(info, "peak_wset", info.rss) / 1024 ** 3, 3)


FUNNEL_POOLS = {"all": ("decoder", "reader", "both"), "decoder_only": ("decoder", "both"), "reader_only": ("reader", "both")}


def funnel(items: list[Item], held: list[list], chooser_of: Callable[[Item], Chooser]) -> dict:
    """Of gold questions: a right program listed, ranked first by the chooser, served right, served wrong; over the whole pool and over each proposer's own candidates, each question decided by the chooser it is decided by."""
    out = {}
    for name, sources in FUNNEL_POOLS.items():
        row = {"n": 0, "listed": 0, "first": 0, "served": 0, "served_wrong": 0}
        for item, group in zip(items, held):
            if item.label != "gold":
                continue
            kept = [record for record in group if record.proposed in sources]
            row["n"] += 1
            row["listed"] += any(record.right for record in kept)
            ranked = sorted(((round(chooser_of(item).logit(record.features)[0], 4), record) for record in kept if record.ran), key=lambda pair: (-pair[0], pair[1].rank))
            row["first"] += bool(ranked) and ranked[0][1].right
            kind, best, _ = decision_of(kept, chooser_of(item))
            row["served"] += kind == ANSWER and best is not None and best.right
            row["served_wrong"] += kind == ANSWER and best is not None and not best.right
        out[name] = row
    return out


def run_mode(items: list[Item], engine: NamedEngine | None, mode: str, *, progress: bool = False, folds: int = 0, keep_candidates: str | None = None) -> dict:
    """Every item through one mode. Counts, by label and by form, the times, the links; per question only its id and result. With folds, each question is decided by a chooser calibrated on the other folds."""
    runner = Runner(engine)
    records = []
    times: list[float] = []
    stages: dict[str, list[float]] = {}
    link_ranks: dict[str, list[int]] = {}
    held: list[list] = []
    lost: dict[str, str] = {}
    listed_at: list[int] = []
    digest = hashlib.sha256()
    for at, item in enumerate(items):
        prepared = runner.prepare(item)
        if mode == MODE_WORDS:
            kind, rows, seconds = words_outcome(item, prepared)
            reasons = ()
            support = SUPPORT_NO_LINKS
        else:
            outcome = engine.ask(item.question, prepared, mode=mode)
            kind, rows, seconds, reasons = outcome.kind, outcome.rows, outcome.seconds["total"], outcome.decision.reasons
            for stage, value in outcome.seconds.items():
                stages.setdefault(stage, []).append(value * 1000.0)
            ranks = []
            for link in item.links:
                rank = link_rank(outcome.named, engine, prepared, link)
                if rank is not None:
                    link_ranks.setdefault(link["kind"], []).append(rank)
                    ranks.append(rank)
            kept = records_of(outcome.candidates, item, outcome.named, prepared.catalog)
            held.append(kept)
            right_at = [row.rank for row in kept if row.right]
            if item.label == "gold":
                listed_at.append(min(right_at) if right_at else -1)
                if not right_at:
                    lost[item.id] = stage_of(item, outcome, prepared, ranks, False)
            support = next((row.support for row in kept if outcome.decision.candidate is not None and row.rank == outcome.decision.candidate.rank), SUPPORT_NO_LINKS)
        digest.update(f"{item.id}|{kind}|{answer_key(rows)}\n".encode("utf-8", "surrogatepass"))
        times.append(seconds * 1000.0)
        result = graded(item, kind, rows)
        records.append({"id": item.id, "label": item.label, "form": item.form, "family": item.family, "kind": kind, "result": result, "reasons": list(reasons), "support": support})
        if progress and (at + 1) % 50 == 0:
            print(f"{mode} {at + 1} of {len(items)}", flush=True)
    runner.close()
    choosers = None
    if folds and mode != MODE_WORDS:
        gold = {item.id for item in items if item.label == "gold"}
        rows_of_gold = [row for group in held for row in group if row.question in gold]
        choosers = cross_fit(engine.chooser, rows_of_gold, folds)
        for item, record, group in zip(items, records, held):
            kind, best, reasons = decision_of(group, choosers[fold_of(item.family, folds)])
            record.update(kind=kind, result=graded_choice(item, kind, bool(best is not None and best.right)), reasons=list(reasons), support=best.support if best is not None else SUPPORT_NO_LINKS)
    if keep_candidates and held:
        with gzip.open(keep_candidates, "wb") as sink:
            pickle.dump(held, sink, protocol=4)
    if held:
        report_funnel = funnel(items, held, (lambda item: choosers[fold_of(item.family, folds)]) if choosers is not None else (lambda item: engine.chooser))
    for record, group in zip(records, held):
        if record["label"] == "gold" and record["result"] != RIGHT:
            record["stage"] = lost.get(record["id"], "choose")
    all_ranks = [rank for ranks in link_ranks.values() for rank in ranks]
    report = {"mode": mode, "by_label": {}, "by_form": {}, "p50_ms": percentile(times, 50), "p99_ms": percentile(times, 99), "stages_p50_ms": {stage: percentile(values, 50) for stage, values in stages.items()}, "index_seconds_p50": percentile(runner.index_seconds, 50), "index_seconds_p99": percentile(runner.index_seconds, 99), "index_kb_p50": round(percentile(runner.index_bytes, 50) / 1024, 1) if runner.index_bytes else None, "workbooks_indexed": len(runner.index_seconds), "link_top10": round(sum(rank < 10 for rank in all_ranks) / len(all_ranks), 4) if all_ranks else None, "links": len(all_ranks), "link_top10_by_kind": {kind: {"n": len(ranks), "top10": round(sum(rank < 10 for rank in ranks) / len(ranks), 4)} for kind, ranks in sorted(link_ranks.items())}, "peak_gb": peak_gb(), "records": records, "digest": digest.hexdigest()}
    if held:
        report["funnel"] = report_funnel
    if listed_at:
        report["right_listed"] = {"n": len(listed_at), **{f"top{cut}": sum(0 <= at < cut for at in listed_at) for cut in (1, 3, 5, 10, 20, 50)}}
    answered = [record for record in records if record["kind"] == ANSWER]
    report["answers"] = {"n": len(answered), "named_support": sum(record["support"] == SUPPORT_ALL for record in answered), "no_links_to_support": sum(record["support"] == SUPPORT_NO_LINKS for record in answered), "not_all_named": sum(record["support"] not in (SUPPORT_ALL, SUPPORT_NO_LINKS) for record in answered)}
    stage_counts: dict[str, dict] = {}
    for record in records:
        if "stage" in record:
            row = stage_counts.setdefault(record["stage"], {ASKED: 0, WRONG: 0})
            row[record["result"]] = row.get(record["result"], 0) + 1
    report["stages_of_misses"] = stage_counts
    if choosers is not None:
        report["_choosers"] = choosers
        report["chooser_all"] = calibrated(engine.chooser, rows_of_gold).record()
    for key, field in (("by_label", "label"), ("by_form", "form")):
        groups: dict[str, dict] = {}
        for record in records:
            group = groups.setdefault(record[field] or "all", {RIGHT: 0, ASKED: 0, WRONG: 0, "n": 0})
            group[record["result"]] += 1
            group["n"] += 1
        report[key] = groups
    return report


def run_survival(items: list[Item], base: dict, engine: NamedEngine | None, mode: str, count: int, choosers: dict | None = None, folds: int = 0) -> dict:
    """Of the questions right on the sheet as it is, how many stay right when its columns are moved, added to, renamed, or stripped of their headers. A fair draw by id, each workbook re-read whole. Each question keeps the chooser it was decided by."""
    right = {record["id"] for record in base["records"] if record["result"] == RIGHT and record["label"] == "gold"}
    chosen = sorted((item for item in items if item.id in right), key=lambda item: hashlib.blake2b(item.id.encode(), digest_size=8).digest())[:count]
    chosen.sort(key=lambda item: item.book)
    out = {}
    for name, operation in SURVIVAL_OPS.items():
        runner = Runner(engine)
        kept = 0
        for item in chosen:
            prepared = runner.prepare(item, mutate=lambda catalog, op=operation: mutate_catalog(catalog, op))
            if mode == MODE_WORDS:
                kind, rows, _ = words_outcome(item, prepared)
            else:
                if choosers:
                    engine.chooser = choosers[fold_of(item.family, folds)]
                outcome = engine.ask(item.question, prepared, mode=mode)
                kind, rows = outcome.kind, outcome.rows
            kept += graded(item, kind, rows) == RIGHT
        runner.close()
        out[name] = {"n": len(chosen), "stayed_right": kept, "share": round(kept / len(chosen), 4) if chosen else None}
    return out


def gate_a(report: dict, survival: dict | None) -> dict:
    """Gate A of the runbook, on gold questions: each bar against what was measured, and the stop line under it."""
    gold = report["by_label"].get("gold") or {RIGHT: 0, ASKED: 0, WRONG: 0, "n": 0}
    n = max(1, gold["n"])
    right, wrong = gold[RIGHT] / n, gold[WRONG] / n
    bars = {"right": (round(right, 4), right >= GATE_A["right"]), "wrong": (round(wrong, 4), wrong <= GATE_A["wrong"]), "link_top10": (report.get("link_top10"), report.get("link_top10") is not None and report["link_top10"] >= GATE_A["link_top10"]), "p50_ms": (report.get("p50_ms"), report.get("p50_ms") is not None and report["p50_ms"] <= GATE_A["p50_ms"])}
    for name, row in (survival or {}).items():
        bars[name] = (row["share"], row["share"] is not None and row["share"] >= GATE_A[name])
    complete = set(bars) == set(GATE_A) and all(value is not None for value, _ in bars.values())
    return {"bars": {name: {"measured": value, "bar": GATE_A[name], "passes": passes} for name, (value, passes) in bars.items()}, "all_measured": complete, "passes": complete and all(passes for _, passes in bars.values()), "stop_line": right < STOP_RIGHT and wrong <= STOP_WRONG}


def provenance(args: argparse.Namespace, items: list[Item]) -> dict:
    root = Path(__file__).resolve().parents[2]
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "-C", str(root), "status", "--short", "--", "engine/named", "training/named", "engine/program", "engine/sheet"], text=True).strip())
    dataset = hashlib.sha256("\n".join(sorted(item.id for item in items)).encode()).hexdigest()
    out = {"commit": commit, "dirty": dirty, "set": args.set, "questions": len(items), "question_ids_sha256": dataset, "flags": {key: value for key, value in vars(args).items() if key != "out"}, "threads": os.environ.get("OMP_NUM_THREADS")}
    if args.bundle:
        digest = hashlib.sha256()
        for name in ("reader.pt", "named_bundle.json"):
            digest.update((Path(args.bundle) / name).read_bytes())
        out["bundle_sha256"] = digest.hexdigest()
    if getattr(args, "readers", None):
        out["readers"] = readers_identity(args.readers)
    if args.set == "val":
        with open(f"{FACTORY_OUT}val.jsonl", "rb") as source:
            out["val_jsonl_sha256"] = hashlib.sha256(source.read()).hexdigest()
    return out


def readers_identity(folder: str) -> dict:
    """A readers folder's identity: each model file's sha256 and each reader's anchoring."""
    out = {"folder": str(folder)}
    for path in sorted(Path(folder).glob("*/model.pt")):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for block in iter(lambda: source.read(1 << 24), b""):
                digest.update(block)
        out[path.parent.name] = digest.hexdigest()
        reader = path.parent / "reader.json"
        if reader.exists():
            out[f"{path.parent.name}_reader"] = json.loads(reader.read_text(encoding="utf-8"))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="The whole engine offline, graded by enron_grade.")
    parser.add_argument("--set", required=True, choices=("val", "studied", "messy"))
    parser.add_argument("--bundle")
    parser.add_argument("--words", action="store_true", help="the reading from words alone, no model")
    parser.add_argument("--modes", default=f"{MODE_NAMED},{MODE_NAMED_PLUS_OPEN}")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--survival", type=int, default=0, help="how many right questions each survival test re-reads; 0 for none")
    parser.add_argument("--folds", type=int, default=0, help="decide each question by a chooser calibrated on the other folds")
    parser.add_argument("--chooser", help="a validation report, whose fit of all folds decides, per mode, over the bundle's chooser")
    parser.add_argument("--keep-candidates", action="store_true", help="write every candidate's evidence beside the report, to fit again without reading")
    parser.add_argument("--readers", help="a folder of today's readers to propose beside the decoder (default: the bundle's)")
    parser.add_argument("--reader-cache", help="a .pkl.gz of the readers' proposals, read first and written back")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    items = load_factory_set("val", limit=args.limit) if args.set == "val" else load_messy() if args.set == "messy" else load_studied()
    if args.limit and args.set != "val":
        items = items[:args.limit]
    report = {"provenance": provenance(args, items), "not_a_result_unless_candidate_passed_contract": True, "modes": {}}
    engine = None
    modes = [MODE_WORDS] if args.words else args.modes.split(",")
    if not args.words:
        if not args.bundle:
            parser.error("--bundle, or --words")
        engine = load_bundle(args.bundle, readers=args.readers)
        if engine.proposer is not None and not args.readers:
            report["provenance"]["readers"] = readers_identity(engine.proposer.directory)
        if engine.proposer is not None and args.reader_cache and os.path.exists(args.reader_cache):
            with gzip.open(args.reader_cache, "rb") as source:
                engine.proposer.cache.update(pickle.load(source))
        report["provenance"]["torch_threads"] = torch.get_num_threads()
        report["provenance"]["machine"] = {"logical_cores": psutil.cpu_count(), "memory_gb": round(psutil.virtual_memory().total / 1024 ** 3, 1), "cpu_percent_at_start": psutil.cpu_percent(interval=1.0), "note": "the development machine, shared with other work, not Cloud Run"}
    bundle_chooser = engine.chooser if engine is not None else None
    for mode in modes:
        started = time.time()
        if engine is not None:
            engine.chooser = Chooser.from_record(json.loads(Path(f"{args.chooser}.chooser_{mode}.json").read_text(encoding="utf-8"))) if args.chooser else bundle_chooser
        row = run_mode(items, engine, mode, progress=True, folds=args.folds, keep_candidates=f"{args.out}.candidates_{mode}.pkl.gz" if args.keep_candidates else None)
        row["wall_seconds"] = round(time.time() - started, 1)
        choosers = row.pop("_choosers", None)
        if row.get("chooser_all") is not None:
            Path(f"{args.out}.chooser_{mode}.json").write_text(json.dumps(row["chooser_all"], indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        survival = run_survival(items, row, engine, mode, args.survival, choosers, args.folds) if args.survival and mode in (MODE_NAMED, MODE_WORDS) else None     # the gate's mode: the other is a measurement of the cost
        row["survival"] = survival
        row["gate_a"] = gate_a(row, survival)
        report["modes"][mode] = row
        summary = {key: row.get(key) for key in ("mode", "by_label", "p50_ms", "p99_ms", "stages_p50_ms", "index_seconds_p50", "index_seconds_p99", "index_kb_p50", "link_top10", "link_top10_by_kind", "peak_gb", "survival", "gate_a", "right_listed", "funnel", "answers", "stages_of_misses", "digest")}
        print(json.dumps(summary, sort_keys=True), flush=True)
        if engine is not None and engine.proposer is not None and args.reader_cache:
            with gzip.open(args.reader_cache + ".tmp", "wb") as sink:
                pickle.dump(engine.proposer.cache, sink, protocol=4)
            os.replace(args.reader_cache + ".tmp", args.reader_cache)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=0, sort_keys=True, default=str) + "\n", encoding="utf-8", newline="\n")


__all__ = ["GATE_A", "Item", "gate_a", "graded", "graded_choice", "load_factory_set", "load_messy", "load_studied", "record_of", "run_mode", "run_survival", "stage_of"]

if __name__ == "__main__":
    main()
