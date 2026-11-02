"""The Enron emails pilot: real sentences tied to their attachments on the frozen side, by mailbox, attachment name and cell match. Test only: the output sits under evals/question_set/emails/, hidden from engine work, and nothing here prints a sentence or a cell.

A candidate is new body text of an email in a frozen mailbox, attachment named like one of that mailbox's frozen workbooks: a question, or a statement holding a figure with at least four significant digits found in that workbook's cells. Sentences pass the screen first. Gemini sees the sentence, the attachment's name and its tab names, never a cell, and says whether the sentence asks or states something the attachment could answer. Stays under $1 of Gemini at the standard rate.

usage: python -I training/factory/isolated.py training.factory.emails_pilot candidates <extracted root>
       python -I training/factory/isolated.py training.factory.emails_pilot plan
       (llm_run.py FACTORY/staged/emails/triage.requests.jsonl emails_triage --workers 8)
       python -I training/factory/isolated.py training.factory.emails_pilot write
"""
from __future__ import annotations

import argparse
import email
import email.policy
import json
import os
import re
import warnings
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

from training.factory.paths import FILES, NEVER, QUESTION_SET, STAGED
from training.factory.screen import screen_text
from training.factory.split import FROZEN, mailbox, sides

OUT_DIR = QUESTION_SET + "emails/"
WORK = STAGED + "emails/"
MAX_CANDIDATES = 2000
USD_IN, USD_OUT = 0.75 / 1e6, 3.75 / 1e6
CAP_USD = 1.0
_QUOTED = re.compile(r"(?im)^(?:>.*|-+ ?original message ?-+.*|from: .*|sent: .*|to: .*|subject: .*|cc: .*)$")
_SENTENCE = re.compile(r"[^.!?\n]{8,300}[.!?]")
_FIGURE = re.compile(r"(?<![\w/-])\$?\d[\d,]*(?:\.\d+)?(?![\w/-])")


def _digits(text: str) -> float | None:
    try:
        return float(text.replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _significant(value: float) -> int:
    return len(f"{abs(value):.10f}".rstrip("0").rstrip(".").replace(".", "").lstrip("0").rstrip("0"))


def body_of(message) -> str:
    part = message.get_body(preferencelist=("plain",))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):
        return ""
    cut = re.search(r"(?im)^-+ ?(?:original message|forwarded by).*$", text)
    if cut:
        text = text[:cut.start()]
    return _QUOTED.sub("", text)


def stem_of(name: str) -> str:
    base = os.path.basename(name).rsplit(".", 1)[0].casefold()
    return re.sub(r"[^a-z0-9]+", "", base)


def frozen_index() -> dict[str, dict[str, list[str]]]:
    """Per frozen mailbox, attachment stem -> its frozen workbooks. Names only."""
    out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for name, side in sides().items():
        if side == FROZEN and not name.startswith(NEVER):
            parts = name.split("__", 2)
            if len(parts) == 3:
                out[parts[0]][stem_of(parts[2])].append(name)
    return {box: dict(stems) for box, stems in out.items()}


def workbook_numbers(name: str) -> tuple[set[float], list[str]]:
    """A frozen workbook's numbers and tab names, locally. Nothing leaves this function but set membership tests and tab names."""
    import openpyxl
    warnings.filterwarnings("ignore")
    numbers: set[float] = set()
    tabs: list[str] = []
    book = openpyxl.load_workbook(FILES + name, read_only=True, data_only=True)
    try:
        for sheet in book.worksheets:
            tabs.append(sheet.title)
            for row in sheet.iter_rows(values_only=True, max_row=6000):
                for value in row:
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        numbers.add(round(float(value), 4))
    finally:
        book.close()
    return numbers, tabs


def scan_mailbox(job: tuple) -> dict:
    """One mailbox's emails: {counts, candidates}."""
    box, root, stems = job
    counts: Counter = Counter()
    candidates = []
    cache: dict[str, tuple] = {}
    for folder, _, names in os.walk(os.path.join(root, box)):
        for file_name in names:
            counts["emails"] += 1
            try:
                with open(os.path.join(folder, file_name), "rb") as source:
                    message = email.message_from_binary_file(source, policy=email.policy.default)
            except (OSError, ValueError):
                counts["unreadable"] += 1
                continue
            tied = []
            for part in message.iter_attachments():
                attachment = part.get_filename() or ""
                if attachment.lower().rsplit(".", 1)[-1] in ("xls", "xlsx", "xlsm"):
                    counts["sheet_attachments"] += 1
                    tied.extend(stems.get(stem_of(attachment), ()))
            if not tied:
                continue
            counts["emails_tied_by_name"] += 1
            body = body_of(message)
            for workbook in sorted(set(tied))[:2]:
                if workbook not in cache:
                    try:
                        cache[workbook] = workbook_numbers(workbook)
                    except Exception:  # noqa: BLE001 - counted
                        cache[workbook] = (set(), [])
                        counts["workbook_unreadable"] += 1
                numbers, tabs = cache[workbook]
                for match in _SENTENCE.finditer(body):
                    sentence = " ".join(match.group(0).split())
                    kind = None
                    if sentence.endswith("?"):
                        kind = "question"
                    else:
                        figures = [value for value in map(_digits, _FIGURE.findall(sentence)) if value is not None and _significant(value) >= 4]
                        if any(round(value, 4) in numbers or round(-value, 4) in numbers for value in figures):
                            kind = "statement_with_cell_match"
                    if kind is None:
                        continue
                    counts[f"found_{kind}"] += 1
                    if screen_text(sentence):
                        counts["screened_sentences"] += 1
                        continue
                    candidates.append({"box": box, "email": file_name, "workbook": workbook, "tabs": tabs[:8], "kind": kind, "sentence": sentence})
                    counts[f"candidate_{kind}"] += 1
    return {"counts": dict(counts), "candidates": candidates}


def candidates(root: str, workers: int) -> dict:
    os.makedirs(WORK, exist_ok=True)
    index = frozen_index()
    jobs = [(box, root, stems) for box, stems in sorted(index.items()) if os.path.isdir(os.path.join(root, box))]
    totals: Counter = Counter({"frozen_mailboxes": len(index), "mailboxes_in_archive": len(jobs)})
    out = []
    with ProcessPoolExecutor(workers) as pool:
        for result in pool.map(scan_mailbox, jobs):
            totals.update(result["counts"])
            out.extend(result["candidates"])
    seen = set()
    with open(WORK + "candidates.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for row in out:
            key = (row["workbook"], row["sentence"].casefold())
            if key not in seen:
                seen.add(key)
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    totals["candidates_distinct"] = len(seen)
    return dict(totals)


TRIAGE_SYSTEM = """You sort sentences from business emails sent with a spreadsheet attached. For each numbered sentence you see the attachment's file name and tab names, never its cells. Reply with lines only: <n>|Y if the sentence asks or states a specific figure, name, date or fact that a spreadsheet with this name and these tabs could hold; <n>|N if it is about anything else (scheduling, a meeting, a request to do something, a personal matter, or the file itself)."""


def plan() -> dict:
    with open(WORK + "candidates.jsonl", encoding="utf-8") as source:
        rows = [json.loads(line) for line in source][:MAX_CANDIDATES]
    with open(WORK + "triage.requests.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for at in range(0, len(rows), 25):
            chunk = rows[at:at + 25]
            lines = [f"{at + i}|file {os.path.basename(row['workbook']).split('__', 2)[-1]}; tabs {'; '.join(row['tabs'])}|{row['sentence']}" for i, row in enumerate(chunk)]
            sink.write(json.dumps({"key": f"triage|{at}", "system": TRIAGE_SYSTEM, "prompt": "\n".join(lines), "max_output": 800}, ensure_ascii=False) + "\n")
    return {"sentences": len(rows), "calls": (len(rows) + 24) // 25}


def write() -> dict:
    from training.factory.factory import read_journal
    from training.factory.ledger import spent
    from training.factory.paths import JOURNAL
    with open(WORK + "candidates.jsonl", encoding="utf-8") as source:
        rows = [json.loads(line) for line in source][:MAX_CANDIDATES]
    yes = {}
    for entry in read_journal("emails_triage").values():
        for line in entry["reply"].splitlines():
            match = re.match(r"^(\d+)\|\s*([YN])", line.strip())
            if match:
                yes[int(match[1])] = match[2] == "Y"
    os.makedirs(OUT_DIR, exist_ok=True)
    kept = Counter()
    with open(OUT_DIR + "frozen_email_sentences.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for at, row in enumerate(rows):
            if yes.get(at):
                kept[row["kind"]] += 1
                sink.write(json.dumps({**row, "side": "frozen", "use": "test only", "license": "CC BY 4.0, figshare 1221767"}, ensure_ascii=False) + "\n")
    usage = spent(JOURNAL).get("emails_triage", {"prompt": 0, "output": 0, "thought": 0})
    usd = usage["prompt"] * USD_IN + (usage["output"] + usage["thought"]) * USD_OUT
    return {"triaged": len(yes), "kept": dict(kept), "mailboxes_tied": len({row["box"] for at, row in enumerate(rows) if yes.get(at)}), "workbooks_tied": len({row["workbook"] for at, row in enumerate(rows) if yes.get(at)}), "tokens": usage, "usd_at_standard_rate": round(usd, 3), "cap_usd": CAP_USD}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("candidates", "plan", "write"))
    parser.add_argument("root", nargs="?")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.command == "candidates":
        print(json.dumps(candidates(args.root, args.workers), sort_keys=True))
    elif args.command == "plan":
        print(json.dumps(plan()))
    else:
        print(json.dumps(write(), sort_keys=True))


__all__ = ["mailbox"]

if __name__ == "__main__":
    main()
