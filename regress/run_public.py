"""The public benchmark (owner, 2026-11-22): standard table-QA sets, the engine against frontier models, and the training
questions read for diagnosis, through the one own-data selection.

Sets (regress/eval_sets.py loads them; a sample is a committed list of ids, regress/sets/public/samples/<set>.json):
  databench-test   DataBench (SemEval-2025 Task 8) test, datasets 066-080, all 522 questions, graded by databench_eval's
                   own default_compare. Two datasets (068, 079: 72 questions) exceed the product's upload limit
                   (engine/request_validation.py), which every client enforces, so the engine refuses those sheets;
                   the frontier models get them.
  wtq-test         the benchmark's 1,000 of WikiTableQuestions' 4,344 test questions (seed 20261122)
  wtq-fresh        a fresh 1,000 of the 3,344 that sample left out (seed 20261123): the confirmation of 2026-11-23
  wtq-<name>       any further sample made with --new-sample (its seed and the samples it leaves out are recorded)
  wtq-train        seeded WikiTableQuestions training questions SQUALL annotates, with SQUALL's human-written query (the
                   find set, read for diagnosis; --skip and --size choose which); never a gate
  wtq-train-open   seeded training questions SQUALL does not annotate (a fifth of training, the questions its SQL did not
                   write; test questions are not screened so): read for diagnosis, never a gate
  databench-train  DataBench train 001-049 (at most 60,000 rows), 400 seeded questions; never a gate
Systems:
  engine       the Decisions path alone (engine/tables.py TableQuery.select_query): the erred answer's likeliest pick
               is its answer, no model writes a query. Its records hold every listed answer's grade (built right).
  prereasoner  the same, served: an erred answer retried by Gemini 4 Neon's written query (engine/retry.py); a failed
               retry serves the erred pick.
  neon, opus   Gemini 4 Neon at its default thinking and Claude Opus 5.5 at its default effort write the query for
               every question: the retry's own prompt (columns with their types, the row count, the first rows, the
               values of short text columns) over the question's table as the dataset ships it, one SQL table
               (raw_tables), plus one line stating the answer format. The query runs through the same guard,
               read-only, with the same one repair, on the full table, graded as the engine's rows are.

Every API answer is kept by its request body's hash: the Decisions API's in regress/sets/calls, each written model's in
regress/sets/public/calls/<system>/, so a rerun replays it and a clone reproduces the records. New answers go to this
run's own batch; python -m regress.eval_sets pack compresses them before they are committed. Spend is metered per run
(regress/results/public/<run>/spend/), and no new call is sent once it reaches --cap dollars.
Records: regress/results/public/<run>/<set>/<system>.jsonl; --keep packs them into regress/sets/public/records/<run>/.

usage: python -X utf8 -m regress.run_public --run NAME --set SET --system SYSTEM [--limit N] [--threads N] [--cap 30] [--skip N --size N]
       python -X utf8 -m regress.run_public --new-sample NAME --size 1000 --seed S      (a fresh WikiTableQuestions test sample)
       python -X utf8 -m regress.run_public --compare RUN_A RUN_B --set SET [--system engine]
       python -X utf8 -m regress.run_public --summary RUN
       python -X utf8 -m regress.run_public --census RUN          (wtq-train: the misses by SQUALL query family)
       python -X utf8 -m regress.run_public --oracle RUN [--boost 4]   (wtq-train: what a reading naming SQUALL's columns builds)
       python -X utf8 -m regress.run_public --run NAME --set wtq-train --size 2100 --squall "max .* - min"   (one SQUALL family)
       python -X utf8 -m regress.run_public --run NAME --set wtq-train-open --size 2251 --match "PATTERN"   (one family by its text)
       python -X utf8 -m regress.run_public --run NAME --set wtq-train --size 9070 --books FILE   (the questions on the tables a reading change touches)
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import gzip
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import engine.llm as llm
from engine.request_validation import MAX_UPLOAD_CELLS, MAX_UPLOAD_COLUMNS, MAX_UPLOAD_ROWS
from regress import eval_sets

PUBLIC = eval_sets.SETS / "public"
SAMPLES = PUBLIC / "samples"
WORK = eval_sets.REPO / "regress" / "results" / "public"
SYSTEMS = ("engine", "prereasoner", "neon", "opus")
NEON_MODEL, OPUS_MODEL = "gemini-4-neon", "claude-opus-5-5"
# Dollars per million tokens (input; output with thinking) on 2026-11-22: OpenRouter's model list for Gemini 4 Neon, the
# Claude API's price table for Opus 5.5, Google's Decisions API page for gemini-decision-1 (no output charge).
PRICES = {"decisions": (0.10, 0.0), "retry": (3.0, 15.0), "neon": (3.0, 15.0), "opus": (4.0, 20.0)}
FORMAT = ("Put the answer itself in the first output column: one row holding the value for a single answer, one row "
          "per item for a list; supporting columns may follow it.")
DATABENCH_TRAIN, DATABENCH_TRAIN_SEED = 400, 20261122


class BudgetStop(RuntimeError):
    """The cap is reached: no new call is sent."""


class Ledger:
    """Spend of one run, one file per process (parallel runs never write one file); the cap is checked on the sum."""

    def __init__(self, run: str, cap: float):
        self.cap, self.lock, self.stopped = cap, threading.Lock(), False
        self.folder = WORK / run / "spend"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / f"{os.getpid()}.json"
        self.held: dict[str, dict] = {}

    def total(self) -> float:
        usd = 0.0
        for path in self.folder.glob("*.json"):
            for _ in range(5):
                try:
                    usd += sum(entry["usd"] for entry in json.loads(path.read_text(encoding="utf-8")).values())
                    break
                except (OSError, ValueError):
                    time.sleep(0.05)
        return usd

    def check(self) -> None:
        if self.stopped or self.total() >= self.cap:
            self.stopped = True
            raise BudgetStop(f"API spend reached the ${self.cap:g} cap")

    def add(self, system: str, tokens_in: int, tokens_out: int) -> None:
        price = PRICES[system]
        with self.lock:
            entry = self.held.setdefault(system, {"calls": 0, "input": 0, "output": 0, "usd": 0.0})
            entry["calls"] += 1
            entry["input"] += int(tokens_in)
            entry["output"] += int(tokens_out)
            entry["usd"] = round((entry["input"] * price[0] + entry["output"] * price[1]) / 1e6, 4)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.held), encoding="utf-8")
            for attempt in range(20):     # Windows refuses the replace while another process reads the file
                try:
                    os.replace(temporary, self.path)
                    break
                except PermissionError:
                    time.sleep(0.05 * (attempt + 1))


class Decisions:
    """engine.llm's Decisions transport over the recorded answers: a body they hold is replayed, a new one sent (after
    the ledger's check) and kept in this run's batch."""

    def __init__(self, ledger: Ledger, batch: str):
        self.ledger, self.send = ledger, llm.DECISIONS_TRANSPORT
        self.store = eval_sets.CallStore(eval_sets.CALLS, batch)

    def __call__(self, payload: bytes) -> dict:
        digest = llm.decision_hash(payload)
        held = self.store.get(digest)
        if held is None:
            self.ledger.check()
            answer = self.send(payload)
            self.ledger.add("decisions", int(answer.get("tokens") or 0), 0)
            held = {"answers": answer["answers"], "tokens": answer["tokens"]}
            self.store.keep(digest, held)
        return {"answers": held["answers"], "tokens": held["tokens"]}


class Recorded:
    """engine.llm's query transport over one written model's recorded answers (its own folder, since each system
    rewrites the same body its own way): a body held is replayed; a new one sent (after the ledger's check) and kept."""

    def __init__(self, name: str, ledger: Ledger, send, batch: str):
        self.name, self.ledger, self.send = name, ledger, send
        self.store = eval_sets.CallStore(PUBLIC / "calls" / name, batch)

    def __call__(self, payload: bytes) -> dict:
        digest = llm.decision_hash(payload)
        held = self.store.get(digest)
        if held is not None:
            return held
        self.ledger.check()
        answer = self.send(payload)
        usage = answer.get("usage") or {}
        self.ledger.add(self.name, int(usage.get("promptTokenCount") or 0),
                        int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0))
        self.store.keep(digest, answer)
        return answer


def _query_parts(payload: bytes) -> tuple[str, list[tuple[str, str]]]:
    body = json.loads(payload.decode("utf-8"))
    system = "".join(part.get("text", "") for part in body["systemInstruction"]["parts"]) + " " + FORMAT
    turns = [(content.get("role") or "user", "".join(part.get("text", "") for part in content["parts"])) for content in body["contents"]]
    return system, turns


def neon_send(payload: bytes) -> dict:
    """Gemini 4 Neon at its default thinking (no thinkingConfig), the format line added, room for its thinking."""
    body = json.loads(payload.decode("utf-8"))
    body.pop("model", None)
    system, _ = _query_parts(payload)
    body["systemInstruction"] = {"parts": [{"text": system}]}
    body["generationConfig"].pop("thinkingConfig", None)
    body["generationConfig"]["maxOutputTokens"] = 65536
    result = llm._post_developer(llm.QUERY_ENDPOINT.format(model=NEON_MODEL), json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 600.0, 4, "Benchmark query")
    try:
        parts = result["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        raise llm.LLMUnavailable(f"Neon gave no answer ({(result.get('candidates') or [{}])[0].get('finishReason')})") from None
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    return {"text": text, "usage": result.get("usageMetadata") or {}}


_ANTHROPIC = None
_ANTHROPIC_LOCK = threading.Lock()


def opus_send(payload: bytes) -> dict:
    """Claude Opus 5.5 at its default effort (medium; thinking is always on), the answer held to the retry's JSON schema
    by structured outputs, the format line added. No refusal fallback: a fallback model's answer is not Opus 5.5's."""
    import anthropic
    global _ANTHROPIC
    with _ANTHROPIC_LOCK:
        if _ANTHROPIC is None:
            # An organization key names its workspace on each request (ANTHROPIC_WORKSPACE_ID).
            workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
            _ANTHROPIC = anthropic.Anthropic(max_retries=6, timeout=600.0,
                                             default_headers={"anthropic-workspace-id": workspace} if workspace else None)
    system, turns = _query_parts(payload)
    schema = dict(llm.QUERY_SCHEMA, additionalProperties=False)
    try:
        response = _ANTHROPIC.messages.create(
            model=OPUS_MODEL, max_tokens=16000, system=system,
            messages=[{"role": "assistant" if role == "model" else "user", "content": text} for role, text in turns],
            output_config={"format": {"type": "json_schema", "schema": schema}})
    except anthropic.APIError as error:
        raise llm.LLMUnavailable(f"Opus call failed ({type(error).__name__}: {str(error)[:120]})") from None
    if response.stop_reason == "refusal":
        raise llm.LLMUnavailable("Opus declined")
    text = "".join(block.text for block in response.content if block.type == "text")
    return {"text": text, "stop_reason": response.stop_reason,
            "usage": {"promptTokenCount": response.usage.input_tokens, "candidatesTokenCount": response.usage.output_tokens}}


# ---------------------------------------------------------------------------------------------------------- the sets
def sample(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def new_sample(name: str, size: int, seed: int) -> dict:
    """A fresh seeded sample of WikiTableQuestions test ids, every committed sample's ids left out."""
    target = SAMPLES / f"{name}.json"
    if target.exists():
        raise SystemExit(f"{target} exists: a sample is drawn once")
    held = sorted(path.stem for path in SAMPLES.glob("wtq-*.json"))
    exclude = {key for other in held for key in sample(other)["ids"]}
    ids = eval_sets.wtq_test_sample(size, seed, exclude)
    record = {"set": name, "seed": seed, "from": len(eval_sets.wtq_test_ids()) - len(exclude), "exclude": held, "ids": ids}
    target.write_text(json.dumps(record, indent=0) + "\n", encoding="utf-8", newline="\n")
    return record


def items_of(name: str, size: int | None, skip: int) -> tuple[list, dict, dict]:
    """(items, the books the product refuses with their size, each item's SQUALL query) of a set."""
    if name == "databench-test":
        items = eval_sets.databench("test")
        limits = {}
        for book in sorted({item.book for item in items}):
            rows, columns = eval_sets.databench_book_size(next(eval_sets._databench_root().glob(f"{book}")))
            if rows > MAX_UPLOAD_ROWS or columns > MAX_UPLOAD_COLUMNS or rows * columns > MAX_UPLOAD_CELLS:
                limits[book] = f"{rows:,} rows x {columns} columns"
        return items, limits, {}
    if name == "databench-train":
        import random
        items = sorted((item for item in eval_sets.databench("train", max_rows=60000) if item.label == "gold"), key=lambda item: item.id)
        random.Random(DATABENCH_TRAIN_SEED).shuffle(items)
        return sorted(items[skip:skip + (size or DATABENCH_TRAIN)], key=lambda item: (item.book, item.id)), {}, {}
    if name in ("wtq-train", "wtq-train-open"):
        items, sql = eval_sets.wtq_train(size or 600, skip, annotated=name == "wtq-train")
        return items, {}, sql
    if name.startswith("wtq-"):
        return eval_sets.wtq_test(sample(name)["ids"]), {}, {}
    raise SystemExit(f"unknown set {name}")


def raw_tables(item) -> list[dict]:
    """The question's table as its dataset ships it, one SQL table: a DataBench dataset's all.parquet named by its tab,
    or a WikiTableQuestions csv named "table"; a blank header is "column N", a repeated one "name (2)"."""
    import csv
    if item.id.startswith("db/"):
        import pandas as pd
        frame = pd.read_parquet(eval_sets._databench_root() / item.book / "all.parquet")
        name = re.sub(r"^\d+_", "", item.book).replace("_", " ")
        header = [str(column) for column in frame.columns]
        rows = [[eval_sets._databench_cell(value) for value in record] for record in frame.itertuples(index=False, name=None)]
    else:
        with open(eval_sets._wtq_root() / item.book, encoding="utf-8") as source:
            lines = [[eval_sets._wtq_typed(cell) for cell in row] for row in csv.reader(source, escapechar="\\", doublequote=False)]
        name, header, rows = "table", [str(cell) for cell in lines[0]], lines[1:]
    columns, seen = [], set()
    for at, column in enumerate(header):
        base = column.strip() or f"column {at + 1}"
        label, n = base, 2
        while label.lower() in seen:
            label, n = f"{base} ({n})", n + 1
        seen.add(label.lower())
        columns.append(label)
    return [{"name": name, "columns": columns, "rows": rows}]


def _infra(error) -> bool:
    """A provider's failure (an HTTP error or a timeout after its retries), not the model's answer: the row is kept but
    not counted as done, so the next run asks again."""
    return bool(error) and " call failed" in str(error)


def _first(rows, at: int = 3) -> list:
    return [[str(cell)[:60] for cell in row[:4]] for row in list(rows)[:at]]


def _stage_fields(selection, shown, listed_right) -> dict:
    """What the stage map (--stages) reads of a selection beyond its grade: the choice's probability of the shown
    listing and of the likeliest right one, that listing's read-as line, both programs' coordinates
    (engine/program/language.py coordinates), and each compiled table's lines and columns (name, kind, source column)."""
    from engine.program.language import coordinates
    probabilities = selection.probabilities or {}

    def chance(at):
        return round(float(probabilities.get(f"r{at:02d}", 0.0)), 4)

    right = [at for at, flag in enumerate(listed_right) if flag]
    best = max(right, key=lambda at: (chance(at), -at)) if right else None
    out = {"none_p": round(float(probabilities.get("none", 0.0)), 4)}
    if shown is not None:
        out.update(shown_at=shown, shown_p=chance(shown), shown_coordinates=list(coordinates(selection.programs[shown])))
    if best is not None:
        out.update(right_at=best, right_p=chance(best), right_read_as=selection.pool[best].evidence[0],
                   right_coordinates=list(coordinates(selection.programs[best])))
    if selection.catalog is not None:
        out["tables"] = [{"id": table.id, "lines": sum(table.is_data), "printed": len(table.lines) - sum(table.is_data),
                          "columns": [[column.name[:40], column.kind, column.col] for column in table.columns]}
                         for table in selection.catalog.tables]
    return out


# ------------------------------------------------------------------------------------------------------------- runs
def run_engine(system: str, items, limits, sql, sink, done, ledger: Ledger, batch: str, threads: int) -> None:
    import torch
    import regress.run_dev_decisions as dev
    from engine.prepared import scratch_database
    from engine.program.answer import ERRED
    from engine.tables import TableQuery

    torch.set_num_threads(threads)
    llm.DECISIONS_TRANSPORT = Decisions(ledger, batch)
    if system == "prereasoner":
        llm.QUERY_TRANSPORT = Recorded("retry", ledger, llm.QUERY_TRANSPORT, batch)
    planner = TableQuery()
    book, tables, request = None, None, contextlib.ExitStack()
    started, count = time.time(), 0
    with request:
        for item in items:
            if item.id in done or ledger.stopped:
                continue
            clock = time.perf_counter()
            row = {"id": item.id, "book": item.book, "question": item.question}
            if item.id in sql:
                row["squall"] = sql[item.id]
            if item.book in limits:
                row.update(kind="refused", refused=f"over the product's upload limit ({limits[item.book]})", engine=False, served=False, listed=0, listed_right=[])
            else:
                try:
                    if item.book != book:
                        book, tables = None, None
                        request.close()
                        tables = dev.tables_of(item)
                        request.enter_context(scratch_database())
                        book = item.book
                    selection = planner.select_query(item.question, tables)
                    shown = selection.selected if selection.selected is not None else selection.erred
                    rows = selection.answer(shown) if shown is not None else ()
                    kind = dev.kind_of(selection)
                    right = dev.graded(item, rows)
                    row.update(kind=kind, engine=right, served=right, confidence=selection.confidence, answer=_first(rows),
                               listed=len(selection.rows), listed_right=[bool(dev.graded(item, selection.answer(at))) for at in range(len(selection.rows))],
                               read_as=selection.pool[shown].evidence[0] if shown is not None else "")
                    row.update(_stage_fields(selection, shown, row["listed_right"]))
                    if system == "prereasoner" and kind == ERRED:
                        retried = dev.retry_row(planner, item, selection)
                        row["retry"] = retried
                        row["served"] = retried["outcome"] == "right" or (retried["outcome"] == "failed" and right)
                        row["infra"] = _infra(retried.get("error"))
                except BudgetStop:
                    break
                except Exception as error:
                    book, tables = None, None
                    request.close()
                    row.update(kind="error", engine=False, served=False, listed=0, listed_right=[],
                               error=f"{type(error).__name__}: {str(error)[:200]}", infra=_infra(error))
            row["seconds"] = round(time.perf_counter() - clock, 2)
            sink.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            sink.flush()
            count += 1
            if count % 50 == 0:
                print(f"{system} {count} done, {round(time.time() - started)}s, spend ${ledger.total():.2f}", flush=True)


def run_frontier(system: str, items, sink, done, ledger: Ledger, batch: str, threads: int) -> None:
    import regress.run_dev_decisions as dev
    from engine.retry import retry
    from engine.tables import TableQuery

    llm.QUERY_TRANSPORT = Recorded(system, ledger, {"neon": neon_send, "opus": opus_send}[system], batch)
    guard = TableQuery().guard
    started, count, lock = time.time(), [0], threading.Lock()

    def one(item, tables):
        clock = time.perf_counter()
        retried = retry(item.question, tables, guard)
        outcome = ("right" if dev.graded(item, retried.rows) else "wrong") if retried.answered else (
            "unanswerable" if not retried.answerable else "failed")
        return {"id": item.id, "book": item.book, "question": item.question, "outcome": outcome, "right": outcome == "right",
                "sql": retried.sql, "reason": retried.reason, "error": retried.error, "answer": _first(retried.rows),
                "infra": _infra(retried.error), "seconds": round(time.perf_counter() - clock, 2)}

    def keep(item, future) -> None:
        try:
            row = future.result()
        except BudgetStop:
            return
        except Exception as error:
            row = {"id": item.id, "book": item.book, "question": item.question, "outcome": "failed", "right": False,
                   "error": f"{type(error).__name__}: {str(error)[:200]}"}
        if ledger.stopped and row.get("outcome") == "failed":
            return
        with lock:
            sink.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            sink.flush()
            count[0] += 1
            if count[0] % 25 == 0:
                print(f"{system} {count[0]} done, {round(time.time() - started)}s, spend ${ledger.total():.2f}", flush=True)

    groups: dict[str, list] = {}
    for item in items:
        if item.id not in done:
            groups.setdefault(item.book, []).append(item)
    pending = []
    with ThreadPoolExecutor(threads) as pool:
        for group in groups.values():
            if ledger.stopped:
                break
            tables = raw_tables(group[0])
            pending.extend((item, pool.submit(one, item, tables)) for item in group)
            while len(pending) > threads * 3:
                keep(*pending.pop(0))
        while pending:
            keep(*pending.pop(0))


# ---------------------------------------------------------------------------------------------------------- reading
def records(run: str, name: str, system: str) -> dict:
    """A run's records of a set and system: the working copy, else the committed one."""
    for path, opener in ((WORK / run / name / f"{system}.jsonl", open), (PUBLIC / "records" / run / name / f"{system}.jsonl.gz", gzip.open)):
        if path.exists():
            with opener(path, "rt", encoding="utf-8") as source:
                return {row["id"]: row for row in map(json.loads, filter(str.strip, source))}
    raise SystemExit(f"no records of {run}/{name}/{system}")


def verdict(row: dict, system: str) -> bool:
    return bool(row["right"] if system in ("neon", "opus") else row["served"] if system == "prereasoner" else row["engine"])


def wilson(right: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = right / n
    centre, half = (p + z * z / (2 * n)) / (1 + z * z / n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(100 * (centre - half), 1), round(100 * (centre + half), 1)


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact binomial p of wins against losses (ties left out)."""
    n, k = wins + losses, min(wins, losses)
    return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def compare(run_a: str, run_b: str, name: str, system: str) -> dict:
    """Right and built (some listed answer right) in each run, wins and losses of b against a, the marks of each."""
    a, b = records(run_a, name, system), records(run_b, name, system)
    ids = sorted(set(a) & set(b))
    out = {"questions": len(ids)}
    for label, held in ((run_a, a), (run_b, b)):
        marks = collections.Counter((held[i].get("kind"), verdict(held[i], system)) for i in ids)
        out[label] = {"right": sum(verdict(held[i], system) for i in ids), "built": sum(any(held[i].get("listed_right") or []) for i in ids),
                      "unmarked right": marks[("answered", True)], "unmarked wrong": marks[("answered", False)],
                      "warned right": marks[("warned", True)], "warned wrong": marks[("warned", False)],
                      "erred": marks[("erred", True)] + marks[("erred", False)], "refused": marks[("refused", False)],
                      "errors": marks[("error", False)]}
    wins = sum(verdict(b[i], system) and not verdict(a[i], system) for i in ids)
    losses = sum(verdict(a[i], system) and not verdict(b[i], system) for i in ids)
    out.update(wins=wins, losses=losses, sign_test_p=round(sign_test(wins, losses), 4))
    return out


def summary(run: str) -> dict:
    """Per set: each system's right with its Wilson interval, over all questions and within the upload limit."""
    out = {}
    for folder in sorted({path.parent.name for base in (WORK / run, PUBLIC / "records" / run) if base.exists() for path in base.glob("*/*.jsonl*")}):
        verdicts, refused = {}, set()
        for system in SYSTEMS:
            try:
                held = records(run, folder, system)
            except SystemExit:
                continue
            verdicts[system] = {i: verdict(r, system) for i, r in held.items()}
            refused |= {i for i, r in held.items() if r.get("kind") == "refused"}
        if not verdicts:
            continue
        common = set.intersection(*(set(v) for v in verdicts.values()))
        block = {"questions": len(common)}
        for scope, ids in (("all", common), ("within the upload limit", common - refused)):
            if ids and (scope == "all" or refused):
                block[scope] = {system: {"right": sum(v[i] for i in ids), "of": len(ids), "percent": round(100 * sum(v[i] for i in ids) / len(ids), 1),
                                         "ci95": wilson(sum(v[i] for i in ids), len(ids))} for system, v in verdicts.items()}
        out[folder] = block
    return out


PART = re.compile(r"\bc\d+_(\w+)")


def families(sql: str) -> list[str]:
    """The constructs a SQUALL query uses: the cell parts it reads (c4_number, c2_year) and its forms."""
    low = " ".join(sql.lower().split())
    out = []
    parts = sorted(set(PART.findall(low)))
    if parts:
        out.append("part:" + "+".join(parts))
    for name, pattern in (("nested", None), ("line position (id)", r"\border by\s+id\b|\bid\s*[<>=]"),
                          ("nth", r"\blimit\s+\d+\s*,|\boffset\b"), ("having", r"\bgroup by\b.*\bhaving\b"),
                          ("group by", r"\bgroup by\b"), ("arithmetic", r"(?<![<>!=(])\s[-+*/]\s(?!\))"), ("abs", r"\babs\s*\("),
                          ("set operation", r"\b(union|intersect|except)\b"), ("in list", r"\bnot in\b|\bin \("), ("count", r"\bcount\s*\(")):
        if (low.count("select") > 1) if pattern is None else re.search(pattern, low):
            out.append(name)
    return out or ["plain"]


def census(run: str, show: str | None = None) -> None:
    rows = records(run, "wtq-train", "engine")
    built = {key for key, row in rows.items() if any(row.get("listed_right") or [])}
    right = sum(bool(row["engine"]) for row in rows.values())
    print(f"wtq-train {run}: {len(rows)} questions, right {right}, built {len(built)}, not built {len(rows) - len(built)}, built not picked {len(built) - right}")
    every, missed = collections.Counter(), collections.Counter()
    for key, row in rows.items():
        for family in families(row.get("squall") or ""):
            every[family] += 1
            missed[family] += key not in built
    print(f"{'family of the SQUALL query':38} questions  not built")
    for family, n in every.most_common():
        if missed[family]:
            print(f"  {family:36} {n:6}  {missed[family]:5} ({100 * missed[family] / n:.1f}%)")
    if show:
        for key, row in rows.items():
            if key not in built and show in families(row.get("squall") or ""):
                print(" ", key, "|", row["question"][:90], "|", (row.get("squall") or "")[:110], "| read:", row.get("read_as", "")[:80])


SQUALL_COLUMN = re.compile(r"\bc(\d+)(?:_(\w+))?\b")
SQUALL_LITERAL = re.compile(r"'((?:[^']|'')*)'")
SQUALL_OPERATIONS = ((r"\bcount \(", "aggregate", "count"), (r"\bsum \(", "aggregate", "sum"), (r"\bavg \(", "aggregate", "avg"),
                     (r"\bmax \(", "aggregate", "max"), (r"\bmin \(", "aggregate", "min"), (r"\bdesc\b", "direction", "highest"),
                     (r"order by [^)]*?(?<!desc)\blimit\b", "direction", "lowest"), (r"\) - \(| - ", "calc_op", "-"),
                     (r"\bcount \( distinct\b", "aggregate", "countd"),
                     (r"\border by id( asc)? limit\b", "order", "first"), (r"\border by id desc limit\b", "order", "last"),
                     (r"\bid = \( select id\b.*\) \+ 1\b", "neighbour", "after"), (r"\bid = \( select id\b.*\) - 1\b", "neighbour", "before"))


def oracle_reading(sql: str, table) -> list[str]:
    """The coordinates SQUALL's query names on a compiled WikiTableQuestions table: each column it reads (c<k> is the
    csv's k-th column; a part of a text column, c<k>_number, its figure columns too) in the measure, shown, key and
    filter slots, each string it compares with as that column's value, and its operations."""
    from engine.program.names import coordinate
    from engine.program.semantics import fold
    low = " ".join(sql.lower().split())
    columns = table.table.columns
    sources = [column for column in columns if column.col >= 0]
    named = set()
    for k, part in SQUALL_COLUMN.findall(low):
        at = int(k) - 1
        if not 0 <= at < len(sources):
            continue
        source = sources[at]
        named.add(source.position)
        if part:
            named.update(column.position for column in columns if column.col < 0 and column.path[:-1] == (source.path or (source.name,)))
    names = [f"{table.id}.{position}" for position in sorted(named)]
    out = [coordinate(slot, name) for name in names for slot in ("measure", "shown", "key", "filter")]
    for literal in SQUALL_LITERAL.findall(low):
        text = literal.replace("''", "'")
        if "%" in text:     # a written query's like pattern: the partial match, anchored as the pattern is
            core = text.strip("%")
            anchor = ("anywhere" if text.startswith("%") and text.endswith("%") else "starts" if text.endswith("%") else "ends")
            if core and "%" not in core:
                out.extend((coordinate("contains", name, fold(core)) if anchor == "anywhere" else coordinate("contains", name, fold(core), anchor))
                           for name in names)
            continue
        out.extend(coordinate("value", name, fold(text)) for name in names)
    from engine.program.semantics import number_text
    for number in re.findall(r"(?:=|!=) (\d+(?:\.\d+)?)(?![\w.'])", low):     # a number a cell is: Equals on a number column
        out.extend(coordinate("value", name, fold(number_text(float(number)))) for name in names)
    out.extend(coordinate(slot, value) for pattern, slot, value in SQUALL_OPERATIONS if re.search(pattern, low))
    top = low.split(" from ", 1)[0]
    form = ("calc" if re.search(r"\) [-+] \(", low) else "rank" if re.search(r"\border by\b.*\blimit\b", low)
            else "agg" if re.search(r"\b(count|sum|avg|max|min) \(", top) else "show")
    out.append(coordinate("form", form))
    if form == "rank":
        out.append(coordinate("entity", "group" if " group by " in low else "line"))
    for operator in (">=", "<=", "!=", ">", "<"):
        if re.search(rf"\s{re.escape(operator)}\s", low):
            out.append(coordinate("comparison", operator))
    for number in re.findall(r"(?<![\w.'])(\d+(?:\.\d+)?)(?![\w.'])", low.split(" where ", 1)[1] if " where " in low else ""):
        from engine.program.semantics import number_text
        out.append(coordinate("number", number_text(float(number))))
    return out


WRITTEN_IDENTIFIER = re.compile(r'"((?:[^"]|"")+)"')
WRITTEN_PARSE = re.compile(r"\b(substr|substring|instr|replace|trim|ltrim|rtrim|length|strftime|julianday|date|cast)\s*\(", re.I)


def squall_form(sql: str, header: list[str]) -> str:
    """A query a model wrote over the raw table (raw_tables: one table, its csv header as column names) in SQUALL's
    form, so oracle_reading, families and the stage map read it as they read SQUALL's: each quoted column name the
    csv's k-th column, c<k>; c<k>_number where the query casts it, c<k>_parsed where it reads it through a function
    that takes a cell apart (a substring, a replace, a date function); every parenthesis spaced, as SQUALL writes them."""
    positions = {name.lower(): at + 1 for at, name in enumerate(header)}
    out, depth_parsing = [], []      # each open parsing function: [its name, the parentheses opened inside it]
    text = re.sub(r"\s+", " ", sql)
    at = 0
    while at < len(text):
        match = WRITTEN_PARSE.match(text, at)
        if match and (at == 0 or not (text[at - 1].isalnum() or text[at - 1] == "_")):
            depth_parsing.append([match.group(1).lower(), 0])
            out.append(match.group(1).lower() + " ( ")
            at = match.end()
            continue
        char = text[at]
        if char == '"':
            end = at + 1
            while end < len(text) and not (text[end] == '"' and text[end + 1:end + 2] != '"'):
                end += 2 if text[end] == '"' else 1
            name = text[at + 1:end].replace('""', '"')
            k = positions.get(name.lower())
            part = "" if not depth_parsing else "_number" if all(name == "cast" for name, _ in depth_parsing) else "_parsed"
            out.append(f"c{k}{part}" if k else "w")
            at = end + 1
            continue
        if char == "'":
            end = at + 1
            while end < len(text) and not (text[end] == "'" and text[end + 1:end + 2] != "'"):
                end += 2 if text[end] == "'" else 1
            out.append(text[at:end + 1])
            at = end + 1
            continue
        if char == "(":
            if depth_parsing:
                depth_parsing[-1][1] += 1
            out.append(" ( ")
        elif char == ")":
            if depth_parsing:
                if depth_parsing[-1][1] == 0:
                    depth_parsing.pop()
                else:
                    depth_parsing[-1][1] -= 1
            out.append(" ) ")
        else:
            out.append(char.lower())
        at += 1
    return " ".join("".join(out).split())


def written_queries(set_name: str, teacher: str | None) -> tuple[list, dict[str, str]]:
    """(the set's items, each question's query in SQUALL's form): SQUALL's own for wtq-train; for a test sample, the
    query the teacher run's model wrote (neon, recorded with its grade) where it was graded right, in SQUALL's form
    (squall_form over the csv's header as raw_tables names it). A question the teacher got wrong has none."""
    if set_name == "wtq-train":
        items, _, sql = items_of("wtq-train", 100000, 0)
        return items, sql
    items, _, _ = items_of(set_name, None, 0)
    if not teacher:
        return items, {}
    taught = records(teacher, set_name, "neon")
    by_id = {item.id: item for item in items}
    out = {}
    for key, row in taught.items():
        if row.get("outcome") == "right" and row.get("sql") and key in by_id:
            out[key] = squall_form(row["sql"], raw_tables(by_id[key])[0]["columns"])
    return items, out


def oracle(run: str, boost: float, limit: int | None, set_name: str = "wtq-train", teacher: str | None = None) -> None:
    """How many of a run's unbuilt questions a reading naming a human-written query's columns, values and operations
    builds (SQUALL's for wtq-train; on a test sample, the teacher run's Neon query where it was right): each one is read
    again with those coordinates raised by ``boost``, decoded and verified, and stopped before the choice (no new API
    call: the reading's calls are recorded). An upper bound on what a better reading reaches; the rest needs language
    the engine lacks, or a compiled table the query's columns do not map onto."""
    import regress.run_dev_decisions as dev
    import engine.program.answer as answer_module
    from engine.prepared import scratch_database
    from engine.program.decoder import Decoder
    from engine.program.scoring import Reading
    from engine.tables import TableQuery

    class Stop(Exception):
        pass

    held = records(run, set_name, "engine")
    targets = {key for key, row in held.items() if not any(row.get("listed_right") or [])}
    items, sql = written_queries(set_name, teacher)       # every question with a query; the run's unbuilt are kept below
    items = [item for item in items if item.id in targets and item.id in sql][:limit]
    llm.DECISIONS_TRANSPORT = dev.Replay(eval_sets.CallStore(), WORK / "oracle_misses")
    state: dict = {}
    real_decode, real_verify = Decoder.decode, answer_module.verify

    def decode(self, reading, *args, **kwargs):
        raised = {name: reading.scores.get(name, 0.0) + boost for name in state["boost"]}
        return real_decode(self, Reading({**reading.scores, **raised}), *args, **kwargs)

    def verify(programs, tables, expected, top):
        state["verified"] = real_verify(programs, tables, expected, top=top)
        raise Stop

    Decoder.decode, answer_module.verify = decode, verify
    planner, reached, mapped, outcome = TableQuery(), 0, 0, collections.Counter()
    sink_path = WORK / run / set_name / "oracle.jsonl"
    sink_path.parent.mkdir(parents=True, exist_ok=True)
    done = {row["id"] for row in map(json.loads, filter(str.strip, sink_path.read_text(encoding="utf-8").splitlines()))} if sink_path.exists() else set()
    try:
        with open(sink_path, "a", encoding="utf-8") as sink:
            for item in items:
                if item.id in done:
                    continue
                tables = dev.tables_of(item)
                catalog = item.load()
                state.update(boost=oracle_reading(sql[item.id], catalog.tables[0]), verified=None)
                mapped += bool(state["boost"])
                with scratch_database():
                    try:
                        planner.select_query(item.question, tables)
                    except Stop:
                        pass
                    except Exception as error:       # a reading call the store lacks: the question is not measured
                        outcome[type(error).__name__] += 1
                        sink.write(json.dumps({"id": item.id, "oracle": "unmeasured", "error": type(error).__name__}) + "\n")
                        continue
                right = any(candidate.result.rows and dev.graded(item, candidate.result.rows) for candidate in state["verified"] or ())
                reached += right
                outcome["built" if right else "still not built"] += 1
                sink.write(json.dumps({"id": item.id, "oracle": "built" if right else "not built", "mapped": bool(state["boost"])}) + "\n")
                sink.flush()
    finally:
        Decoder.decode, answer_module.verify = real_decode, real_verify
    print(f"{run}: {len(items)} unbuilt training questions, {mapped} with SQUALL columns mapped; with SQUALL's reading raised by {boost}: {dict(outcome)}")


STAGES = ("right", "choice", "table", "reading", "cell part", "operation", "teacher wrong", "unmeasured")


def _choice_difference(shown: list[str], right: list[str]) -> str:
    """How the shown listing's reading differs from the likeliest right one, by the first coordinate kind they differ
    in (engine/program/names.py): its form, table, a line pinned by its label, the measure or shown column, a
    condition, a direction, aggregate or calc operator, else something else."""
    a, b = set(shown), set(right)
    differ = {name.split(":", 1)[0] for name in a ^ b}
    for kind, slots in (("form", {"form"}), ("table", {"table"}), ("line pin", {"line"}), ("column read", {"measure", "shown", "key", "peers"}),
                        ("condition", {"filter", "value", "comparison", "number", "period", "conditions", "said"}),
                        ("operation", {"aggregate", "direction", "calc_op", "entity", "size", "row_op"})):
        if differ & slots:
            return kind
    return "other"


def _wtq_shape(book: str) -> tuple[int, int]:
    """(data rows, columns) of a WikiTableQuestions csv as the dataset ships it, its header row aside."""
    import csv
    with open(eval_sets._wtq_root() / book, encoding="utf-8") as source:
        rows = [row for row in csv.reader(source, escapechar="\\", doublequote=False)]
    return max(0, len(rows) - 1), max((len(row) for row in rows), default=0)


def stages(run: str, set_name: str = "wtq-train", teacher: str | None = None) -> dict:
    """The stage map of a wtq-train run (owner's plan, 2026-11-25): each question at the first stage that loses it.
    right; choice (a listed answer was right, another was picked); and of the unbuilt: table (the compiled table's data
    lines or columns differ from the csv SQUALL reads: a line or column gained or lost), reading (SQUALL's reading
    builds it: --oracle RUN first), cell part (SQUALL reads a part of a cell, c4_number, c2_year, and the compiled
    table holds that source column as text with no figure of its own), operation (the rest: language the engine lacks,
    or what the decoder does not compose), unmeasured (the oracle could not replay it). On a test sample the query is
    the teacher run's (written_queries); an unbuilt question the teacher got wrong too is "teacher wrong" (a hard
    question, or its gold in doubt). Writes regress/results/public/<run>/<set>/stages.jsonl and returns the counts by
    stage and, within each, by family."""
    rows = records(run, set_name, "engine")
    items, written = written_queries(set_name, teacher)
    taught_rows = records(teacher, set_name, "neon") if teacher else {}
    path = WORK / run / set_name / "oracle.jsonl"
    oracle_of = {row["id"]: row for row in map(json.loads, filter(str.strip, path.read_text(encoding="utf-8").splitlines()))} if path.exists() else {}
    shapes: dict[str, tuple[int, int]] = {}
    loaders = {item.book: item.load for item in items}
    catalogs: dict = {}

    def unread_parts(book: str, parts: set[tuple[int, str]]) -> set[str]:
        """The parts SQUALL reads of source columns the compiled table holds as plain text: no figure column of its own
        (c4_number over "3-1" read as "Score #1"), not a number column, not a date column for a date's part."""
        if book not in catalogs:
            catalogs[book] = loaders[book]()
        table = catalogs[book].tables[0]
        columns = table.table.columns
        sources = [column for column in columns if column.col >= 0]
        out = set()
        for k, part in parts:
            if not 0 < k <= len(sources):
                continue
            source = sources[k - 1]
            figures = [column for column in columns if column.col < 0 and column.path[:-1] == (source.path or (source.name,))]
            dated = table.is_date_column(source.position)
            if figures or (source.kind == "number" and "number" in part) or (dated and part in ("year", "month", "day", "parsed")):
                continue
            out.add(part)
        return out
    out, counts, within = [], collections.Counter(), collections.defaultdict(collections.Counter)
    for key, row in sorted(rows.items()):
        sql = written.get(key) or row.get("squall") or ""
        low = " ".join(sql.lower().split())
        built = any(row.get("listed_right") or [])
        tables = row.get("tables") or []
        detail = ""
        if row.get("kind") in ("error", "refused") and not built:
            stage = "unmeasured"
        elif row.get("engine"):
            stage = "right"
        elif built:
            stage, detail = "choice", _choice_difference(row.get("shown_coordinates") or [], row.get("right_coordinates") or [])
        else:
            if row["book"] not in shapes:
                shapes[row["book"]] = _wtq_shape(row["book"])
            data_rows, width = shapes[row["book"]]
            main = tables[0] if tables else {"lines": 0, "printed": 0, "columns": []}
            sources = [column for column in main["columns"] if column[2] is not None and column[2] >= 0]
            used = {int(k) for k, _part in SQUALL_COLUMN.findall(low)}
            oracle_row = oracle_of.get(key, {})
            parts = {(int(k), part) for k, part in SQUALL_COLUMN.findall(low) if part}
            if set_name != "wtq-train" and not sql:
                stage = "teacher wrong"
                theirs = (taught_rows.get(key) or {}).get("answer") or []
                ours = row.get("answer") or []
                detail = ("the same answer as the engine's (the gold in doubt)" if theirs and ours and
                          str(theirs[0][0]).strip().lower().rstrip("0").rstrip(".") == str(ours[0][0]).strip().lower().rstrip("0").rstrip(".")
                          else "another answer")
            elif len(tables) != 1 or main["lines"] + main["printed"] != data_rows or len(sources) != width or (used and max(used) > len(sources)):
                stage, detail = "table", ("tables" if len(tables) != 1 else "lines" if main["lines"] + main["printed"] != data_rows else "columns")
            elif oracle_row.get("oracle") == "built":
                stage = "reading"
            elif oracle_row.get("oracle") in (None, "unmeasured"):
                stage = "unmeasured"
            elif unread := unread_parts(row["book"], parts):
                stage, detail = "cell part", "+".join(sorted(unread))
            else:
                stage = "operation"
        if not detail and stage not in ("right", "unmeasured"):
            detail = ", ".join(families(sql))
        counts[stage] += 1
        within[stage][detail or "-"] += 1
        out.append({"id": key, "stage": stage, "detail": detail, "question": row["question"], "squall": sql,
                    "read_as": row.get("read_as", ""), "right_read_as": row.get("right_read_as", "")})
    with open(WORK / run / set_name / "stages.jsonl", "w", encoding="utf-8") as sink:
        for row in out:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    total = sum(counts.values())
    print(f"{run}: {total} training questions")
    for stage in STAGES:
        if counts[stage]:
            print(f"  {stage:12} {counts[stage]:5} ({100 * counts[stage] / total:.1f}%)")
            for detail, n in within[stage].most_common(12 if stage != "right" else 0):
                print(f"      {n:5}  {detail[:110]}")
    return {stage: counts[stage] for stage in STAGES}


def keep(run: str) -> None:
    """The run's working records packed into regress/sets/public/records/<run>/ (no timestamp in the archives)."""
    for path in sorted((WORK / run).glob("*/*.jsonl")):
        target = PUBLIC / "records" / run / path.parent.name / (path.name + ".gz")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as sink:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    sink.write((line + "\n").encode("utf-8"))
        print("kept", target, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run")
    parser.add_argument("--set")
    parser.add_argument("--system", choices=SYSTEMS, default="engine")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--size", type=int)
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--cap", type=float, default=30.0)
    parser.add_argument("--redo-errors", action="store_true", help="rerun the rows recorded as errors or provider failures")
    parser.add_argument("--squall", help="wtq-train: only the questions whose SQUALL query matches this pattern (one family, for diagnosis)")
    parser.add_argument("--match", help="training sets: only the questions whose text matches this pattern (one family, for diagnosis; never a gate)")
    parser.add_argument("--books", type=Path, help="training sets: only the questions on the workbooks this file lists, one per line (the tables a reading change touches, for diagnosis; never a gate)")
    parser.add_argument("--new-sample")
    parser.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"))
    parser.add_argument("--summary")
    parser.add_argument("--census")
    parser.add_argument("--show")
    parser.add_argument("--keep", help="pack a run's records into regress/sets/public/records")
    parser.add_argument("--oracle", help="wtq-train: how many of a run's unbuilt questions SQUALL's reading builds")
    parser.add_argument("--stages", help="wtq-train, or a test sample with --teacher: each question of a run at the first stage that loses it (after --oracle RUN)")
    parser.add_argument("--teacher", help="with --oracle or --stages on a test sample: the run whose neon records hold the written queries")
    parser.add_argument("--merge", nargs="+", metavar="RUN", help="with --run and --set: the shards' records of a set gathered into one run")
    parser.add_argument("--boost", type=float, default=4.0)
    args = parser.parse_args()
    if args.new_sample:
        record = new_sample(args.new_sample, args.size or 1000, args.seed)
        print(f"{args.new_sample}: {len(record['ids'])} of {record['from']} test questions, seed {record['seed']}, leaving out {record['exclude']}")
        return 0
    if args.compare:
        print(json.dumps(compare(*args.compare, args.set, args.system), indent=1))
        return 0
    if args.summary:
        print(json.dumps(summary(args.summary), indent=1))
        return 0
    if args.census:
        census(args.census, args.show)
        return 0
    if args.keep:
        keep(args.keep)
        return 0
    if args.stages:
        stages(args.stages, args.set or "wtq-train", args.teacher)
        return 0
    if args.merge:
        target = WORK / args.run / args.set / f"{args.system}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        gathered = {}
        for shard in args.merge:
            gathered.update(records(shard, args.set, args.system))
        with open(target, "w", encoding="utf-8") as sink:
            for key in sorted(gathered):
                sink.write(json.dumps(gathered[key], ensure_ascii=False, default=str) + "\n")
        print(f"{args.run}: {len(gathered)} records of {args.set} / {args.system} from {', '.join(args.merge)}")
        return 0
    if args.oracle:
        oracle(args.oracle, args.boost, args.limit, args.set or "wtq-train", args.teacher)
        return 0
    if not (args.run and args.set):
        parser.error("--run and --set are required")
    ledger = Ledger(args.run, args.cap)
    batch = f"{args.run}-{args.set}-{args.system}-{os.getpid()}"     # run names carry their date
    items, limits, sql = items_of(args.set, args.size, args.skip)
    if args.squall:
        items = [item for item in items if re.search(args.squall, " ".join(sql.get(item.id, "").lower().split()))]
    if args.match:
        if not args.set.startswith(("wtq-train", "databench-train")):
            parser.error("--match reads training questions only")
        items = [item for item in items if re.search(args.match, item.question)]
    if args.books:
        if not args.set.startswith(("wtq-train", "databench-train")):
            parser.error("--books reads training questions only")
        books = {line.strip() for line in args.books.read_text(encoding="utf-8").splitlines() if line.strip()}
        items = [item for item in items if item.book in books]
    items = items[:args.limit]
    target = WORK / args.run / args.set / f"{args.system}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    held = {}
    if target.exists():
        held = {row["id"]: row for row in map(json.loads, filter(str.strip, target.read_text(encoding="utf-8").splitlines()))}
    done = {key for key, row in held.items() if not row.get("infra") and not (args.redo_errors and row.get("kind") == "error")}
    if len(done) < len(held):     # rows to ask again are dropped, so the file holds each question once
        target.write_text("".join(json.dumps(held[key], ensure_ascii=False, default=str) + "\n" for key in held if key in done), encoding="utf-8", newline="\n")
    print(f"{args.run} {args.set} / {args.system}: {len(items)} questions, {len(done)} already done, refused: {limits or 'none'}; "
          f"spend so far ${ledger.total():.2f} of ${args.cap:g}", flush=True)
    with open(target, "a", encoding="utf-8", newline="\n") as sink:
        if args.system in ("engine", "prereasoner"):
            run_engine(args.system, items, limits, sql, sink, done, ledger, batch, args.threads)
        else:
            run_frontier(args.system, items, sink, done, ledger, batch, args.threads)
    print(("STOPPED at the cap; " if ledger.stopped else "") + f"done {args.run} {args.set} {args.system}, spend ${ledger.total():.2f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
