"""The frozen read: our frozen questions on public US government workbooks (regress/sets/fresh_frozen: the 212 its
review found right) through the served entry and the retry each mark owes, as regress/run_dev_decisions.py --served
measures the dev sets (ServedEntry: engine/knowledge.py KnowledgeReasoner.serve inside an analysis execution context
on its own request database, then KnowledgeReasoner.settled). A launch gate reads it once; it is never read for
development.

Totals only. No question, answer, reading or id is printed, and the report written to
regress/sets/frozen_reads/<date>-<tag>.json holds counts alone: how many answers are right before the retry (the
engine's own) and after it, what the person gets (an answer, a question back, a chat reply, an error), and the counts
by mark, by route, by answer form and by domain. A run's own progress file (regress/results/frozen/<tag>/progress.jsonl:
an id and its outcome per question, for --resume) stays on the machine that ran it and is not for reading.

Needs a live Postgres (KB_PG_*) and the deployment's GOOGLE_API_KEY: the bodies the recorded answers lack are sent to
the Decisions API and kept in regress/sets/calls, and the retry model's answers in regress/sets/retry_calls, so the
read can be replayed. The messy benchmark's frozen split is read by its own owner (python -m regress.run_messy
--split frozen), which also reports totals only.

usage: python -X utf8 -m regress.run_frozen --tag NAME [--threads 4] [--resume]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

import torch

import engine.llm as llm
import regress.run_dev_decisions as dev
from regress import eval_sets

READS = eval_sets.SETS / "frozen_reads"
SET = "freshfrozen"


def totals(rows: list[dict]) -> dict:
    """Counts alone: right before and after the retry, what the person gets, and each by mark, route, form and domain."""
    def tally(key: str, right: str) -> dict:
        groups: dict = {}
        for row in rows:
            of = groups.setdefault(str(row.get(key)), {"n": 0, "right": 0})
            of["n"] += 1
            of["right"] += bool(row[right])
        return dict(sorted(groups.items()))

    return {"n": len(rows), "first_right": sum(row["first_right"] for row in rows), "final_right": sum(row["final_right"] for row in rows),
            "first": tally("first", "first_right"), "final": tally("final", "final_right"), "level": tally("level", "first_right"),
            "route": tally("route", "first_right"), "retried": tally("retried", "final_right"),
            "form": tally("form", "final_right"), "domain": tally("domain", "final_right")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--resume", action="store_true", help="skip the questions this tag's progress file already holds")
    args = parser.parse_args()
    torch.set_num_threads(args.threads)
    out = dev.ROOT / "regress" / "results" / "frozen" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    batch = f"{datetime.date.today().isoformat()}-{args.tag}-{SET}-{os.getpid()}"
    replay = dev.Replay(eval_sets.CallStore(eval_sets.CALLS, batch), out / "misses", True)
    llm.DECISIONS_TRANSPORT = replay
    query_replay = dev.QueryReplay(eval_sets.CallStore(dev.RETRY_CALLS, batch))
    llm.QUERY_TRANSPORT = query_replay
    with dev.capturing():
        items = eval_sets.fresh_frozen()
    progress = out / "progress.jsonl"
    measured = []
    if args.resume and progress.exists():
        measured = [json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines() if line.strip()]
    done = {row["id"] for row in measured}
    entry = dev.ServedEntry()
    started = time.time()
    book, tables = None, None
    with open(progress, "a" if args.resume else "w", encoding="utf-8", newline="\n") as sink:
        for at, item in enumerate(items):
            if item.id in done:
                continue
            clock = time.perf_counter()
            try:
                if item.book != book:
                    book, tables = item.book, dev.tables_of(item)
                first, final = entry.ask(tables, item.question)
                row = dev.served_row(item, SET, first if isinstance(first, dict) else {}, final if isinstance(final, dict) else {})
            except Exception as error:     # a request that raises is an error the person sees
                row = {"id": item.id, "set": SET, "route": None, "level": None, "first": "error", "first_right": False, "final": "error",
                       "final_right": False, "retried": False, "why": type(error).__name__}
            row = {key: row.get(key) for key in ("id", "route", "level", "first", "first_right", "final", "final_right", "retried")}
            row.update(form=item.form, domain=item.family, seconds=round(time.perf_counter() - clock, 1))
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()
            measured.append(row)
            if (at + 1) % 20 == 0:     # progress without outcomes
                print(f"{at + 1}/{len(items)} {round(time.time() - started)}s", flush=True)
    report = {"set": "fresh_frozen", "questions": len(items), "tag": args.tag, "date": datetime.date.today().isoformat(),
              "commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
              "dirty": bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip()),
              "calls_batch": batch, "retry_tokens_sent": list(query_replay.tokens), "seconds": round(time.time() - started, 1),
              "totals": totals(measured)}
    READS.mkdir(parents=True, exist_ok=True)
    target = READS / f"{report['date']}-{args.tag}.json"
    target.write_bytes((json.dumps(report, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps(report["totals"]), flush=True)
    print(f"-> {target}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
