"""The factory's stages. Each reads the journals and the tabs, writes per-tab files under FACTORY/staged/<side>/, and can be run again: nothing is paid twice, nothing already written is redone.

  author-plan  --side S --tabs N       the requests for the next N tabs of the side's draw (rank 0 covers families first)
  execute      --side S                parse each reply, bind each program, run it with engine/program; drop what does not execute or returns nothing
  rewrite-plan --side S                the requests rewriting questions that share a word with a used column's header
  route-plan   --side S                the second route's requests: raw cells and the final questions only
  route-run    --side S                run the second route's programs in the sandbox, restricted
  assemble     --side S                gold, ambiguous, refusal; noise; quotas; links with named coordinates; FACTORY/out/<side>.jsonl

Gemini itself runs through llm_run.py, from the readers' worktree. Prints counts, never cells or questions.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import os
import pickle
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

from training.factory.paths import JOURNAL, MODELS, STAGED, TABS, pin

HERE = os.path.dirname(os.path.abspath(__file__))
STOP_WORDS = frozenset("a an and at by for from in is it of on or per the to with total no num number name".split())
MIN_NO_OVERLAP = 0.55
MAX_ROWS_KEPT = 50


def tab_key(record: dict) -> str:
    return f"{record['book']}|{record['tab']}"


def safe_name(key: str) -> str:
    return hashlib.blake2b(key.encode("utf-8"), digest_size=10).hexdigest()


def load_tabs(side: str) -> list[dict]:
    with open(f"{TABS}{side}.jsonl", encoding="utf-8") as source:
        return [json.loads(line) for line in source]


def draw_order(records: list[dict]) -> list[dict]:
    """Rank 0 of every family before rank 1; the first tab of each workbook before its second; a fixed shuffle inside that."""
    seen: dict[str, int] = defaultdict(int)
    keyed = []
    for record in sorted(records, key=lambda r: (r["file"], r["tab"])):
        at = seen[record["book"]]
        seen[record["book"]] += 1
        keyed.append(((record["rank"], at, hashlib.blake2b(tab_key(record).encode("utf-8"), digest_size=8).hexdigest()), record))
    return [record for _, record in sorted(keyed, key=lambda pair: pair[0])]


def usable(record: dict) -> bool:
    f = record["features"]
    return f["tables"] > 0 and f["lines"] >= 3 and f["columns"] >= 2


def read_journal(name: str) -> dict[str, dict]:
    path = f"{JOURNAL}{name}.jsonl"
    out: dict[str, dict] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as source:
            for line in source:
                record = json.loads(line)
                if record.get("reply") is not None:
                    out[record["key"]] = record
    return out


def stage_dir(side: str, name: str) -> str:
    path = f"{STAGED}{side}/{name}/"
    os.makedirs(path, exist_ok=True)
    return path


# ------------------------------------------------------------------------------------------------------- author plan
def author_plan(side: str, tabs: int, out_path: str) -> dict:
    from training.factory.prompts import AUTHOR_SYSTEM, author_max_output, author_prompt
    records = draw_order([r for r in load_tabs(side) if usable(r)])
    done = read_journal(f"author_{side}")
    fresh = [r for r in records if f"author|{tab_key(r)}" not in done][:tabs]
    with open(out_path, "w", encoding="utf-8", newline="\n") as sink:
        for record in fresh:
            prompt, slots = author_prompt(record)
            sink.write(json.dumps({"key": f"author|{tab_key(record)}", "system": AUTHOR_SYSTEM, "prompt": prompt, "max_output": author_max_output(slots)}, ensure_ascii=False) + "\n")
    return {"usable_tabs": len(records), "done": len(done), "planned": len(fresh)}


# ------------------------------------------------------------------------------------------------- parsing the author
_COL = re.compile(r"^COL\s+([tfn]\d+)\.(a?\d+)\s+(\S+)\s+([1-9])(?:\s+val\s+(\S+)\s+([1-9]))?\s*$")


def parse_author(reply: str) -> dict:
    """Lines to fields. A line that fits nothing is counted, not guessed at."""
    out = {"role": "", "columns": [], "questions": [], "refusals": [], "junk": 0}
    for raw in reply.splitlines():
        line = raw.strip().strip("`")
        if not line:
            continue
        if line.startswith("ROLE"):
            out["role"] = line[4:].strip()[:60]
        elif line.startswith("COL"):
            match = _COL.match(line)
            if match:
                out["columns"].append({"table": match[1], "at": match[2], "name": match[3], "conf": int(match[4]), "val": match[5], "val_conf": int(match[6]) if match[6] else None})
            else:
                out["junk"] += 1
        elif re.match(r"^Q\d+\|", line):
            parts = line.split("|")
            if len(parts) >= 4:
                out["questions"].append({"slot": int(parts[0][1:]), "question": parts[1].strip(), "program": "|".join(parts[2:-1]).strip(), "links": parts[-1].strip()})
            elif len(parts) == 3:
                out["questions"].append({"slot": int(parts[0][1:]), "question": parts[1].strip(), "program": parts[2].strip(), "links": ""})
            else:
                out["junk"] += 1
        elif re.match(r"^R\d+\|", line):
            parts = line.split("|")
            if len(parts) >= 2:
                out["refusals"].append({"question": parts[1].strip(), "why": parts[2].strip()[:120] if len(parts) > 2 else ""})
        else:
            out["junk"] += 1
    return out


def used_columns(program) -> list:
    from engine.program.language import Calc, Rank, Show, conditions_of, measure_columns
    if isinstance(program, Calc):
        return used_columns(program.left) + used_columns(program.right)
    out = []
    if isinstance(program, Show):
        out.extend(program.shown)
    else:
        if program.measure is not None:
            out.extend(measure_columns(program.measure))
        if isinstance(program, Rank):
            out.extend(program.shown)
            if program.key is not None:
                out.append(program.key)
    for condition in conditions_of(program):
        for attribute in ("column", "other"):
            column = getattr(condition, attribute, None)
            if column is not None:
                out.append(column)
    return out


def header_words(program, catalog) -> set[str]:
    """The words of the headers of the columns a program uses: a "Column F" names nothing; a peer rank's headers count too."""
    from engine.program.index import words_of
    from engine.program.language import Calc, Rank, SECTION
    words: set[str] = set()
    for column in used_columns(program):
        if column.position == SECTION:
            continue
        name = catalog.table(column.table).columns[column.position].name
        if not name.startswith("Column "):
            words.update(words_of(name))
    programs = [program]
    while programs:
        current = programs.pop()
        if isinstance(current, Calc):
            programs.extend((current.left, current.right))
        elif isinstance(current, Rank) and current.peers:
            table = catalog.table(current.table)
            for position in current.peers:
                words.update(words_of(table.columns[position].name))
    return {word for word in words if len(word) >= 2 and word not in STOP_WORDS and not word.isdigit()}


def overlap_words(question: str, words: set[str]) -> list[str]:
    from engine.program.index import words_of
    return sorted(set(words_of(question)) & words)


def _plain_cell(cell):
    if cell is None or isinstance(cell, (str, int, float, bool)):
        return cell
    return str(cell)


_NAMES_POSITION = re.compile(r"(?:col(?:umn)?|row)\.? ?(?:[A-Za-z]{1,2}|\d+)|cell [A-Z]{1,2}\d+")


def execute_tab(job: tuple) -> dict:
    """One workbook's tabs: bind and run each program. Returns counts; writes a file per tab."""
    pin()
    book, items, side = job
    from engine.program.catalog import Catalog
    from engine.program.execute import run
    from engine.program.language import answer_type_of, coordinates, form_of
    from engine.program.store import RequestTables
    from engine.program.verifier import shape_evidence
    from training.factory.grammar import Unbound, bind
    with gzip.open(f"{MODELS}{book}.pkl.gz", "rb") as source:
        model = pickle.load(source)
    catalog = Catalog.from_model(model)
    tables = RequestTables(catalog)
    counts: Counter = Counter()
    for tab_record, reply, path in items:
        parsed = parse_author(reply)
        counts["junk_lines"] += parsed["junk"]
        kept = []
        allowed = set(tab_record["tables"])
        for q in parsed["questions"]:
            counts["written"] += 1
            if not q["question"] or not q["program"]:
                counts["drop_blank"] += 1
                continue
            if _NAMES_POSITION.search(q["question"]):
                counts["drop_names_a_position"] += 1
                continue
            try:
                program = bind(q["program"], catalog, allowed)
            except Unbound as exc:
                counts[f"drop_bind_{exc}"] += 1
                continue
            try:
                result = run(program, tables)
            except Exception as exc:  # noqa: BLE001 - counted: a program that does not execute is dropped
                counts[f"drop_run_{type(exc).__name__}"] += 1
                continue
            rows = result.rows
            if not rows:
                counts["drop_empty"] += 1
                continue
            evidence = {item.name for item in shape_evidence(rows, program, None)}
            if "bare_zero" in evidence:
                counts["drop_bare_zero"] += 1
                continue
            if len(rows) > MAX_ROWS_KEPT:
                counts["drop_too_many_rows"] += 1
                continue
            if "row_dump" in evidence and form_of(program) != "show":
                counts["drop_row_dump"] += 1
                continue
            words = header_words(program, catalog)
            used = [{"table": column.table, "position": column.position, "letter": "band" if column.position < 0 else catalog.table(column.table).columns[column.position].letter, "name": "band" if column.position < 0 else catalog.table(column.table).columns[column.position].name} for column in dict.fromkeys(used_columns(program))]
            kept.append({**q, "form": form_of(program), "used": used, "listing": getattr(program, "listing", False), "repr": repr(program), "coordinates": list(coordinates(program, catalog.column_kind)), "answer_type": answer_type_of(program, catalog.column_kind),
                         "rows": [[_plain_cell(cell) for cell in row] for row in rows], "header_words": sorted(words), "overlap": overlap_words(q["question"], words)})
            counts["ran"] += 1
        counts["refusals_written"] += len(parsed["refusals"])
        out = {"key": tab_key(tab_record), "file": tab_record["file"], "book": book, "family": str(tab_record["family"]), "tab": tab_record["tab"], "role": parsed["role"], "labels": parsed["columns"], "questions": kept, "refusals": parsed["refusals"]}
        with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as sink:
            json.dump(out, sink, ensure_ascii=False)
        os.replace(path + ".tmp", path)
        counts["tabs"] += 1
    tables.connection.close()
    return dict(counts)


def execute(side: str, workers: int) -> dict:
    journal = read_journal(f"author_{side}")
    folder = stage_dir(side, "cand")
    by_book: dict[str, list] = defaultdict(list)
    for record in load_tabs(side):
        key = f"author|{tab_key(record)}"
        path = f"{folder}{safe_name(tab_key(record))}.json"
        if key in journal and not os.path.exists(path):
            slim = {k: record[k] for k in ("file", "book", "family", "tab", "tables")}
            by_book[record["book"]].append((slim, journal[key]["reply"], path))
    totals: Counter = Counter()
    if not by_book:
        return {}
    books = list(by_book.items())
    for first in range(0, len(books), 100):
        # A fresh pool per chunk gives memory back; max_tasks_per_child stops replacing workers on this Python.
        with ProcessPoolExecutor(workers) as pool:
            futures = [pool.submit(execute_tab, (book, items, side)) for book, items in books[first:first + 100]]
            for future in futures:
                try:
                    totals.update(future.result())
                except Exception as exc:  # noqa: BLE001 - a lost workbook is counted
                    totals[f"lost_book_{type(exc).__name__}"] += 1
        print("executed", first + 100, "of", len(books), flush=True)
    return dict(totals)


# --------------------------------------------------------------------------------------------------- rewriting
def rewrite_plan(side: str, out_path: str) -> dict:
    """Per tab: the questions over the overlap share get a rewrite, longest overlap first. Only those. The share is the script's."""
    from training.factory.prompts import REWRITE_SYSTEM
    done = read_journal(f"rewrite_{side}")
    counts: Counter = Counter()
    with open(out_path, "w", encoding="utf-8", newline="\n") as sink:
        for path in sorted(glob.glob(stage_dir(side, "cand") + "*.json")):
            with open(path, encoding="utf-8") as source:
                tab = json.load(source)
            questions = tab["questions"]
            if not questions:
                continue
            clean = sum(1 for q in questions if not q["overlap"])
            need = max(0, int(MIN_NO_OVERLAP * len(questions) + 0.999) - clean)
            counts["questions"] += len(questions)
            counts["clean"] += clean
            if not need:
                continue
            key = f"rewrite|{tab['key']}"
            if key in done:
                counts["done_tabs"] += 1
                continue
            offenders = sorted([at for at, q in enumerate(questions) if q["overlap"]], key=lambda at: -len(questions[at]["overlap"]))[:need + 1]
            counts["rewrites"] += len(offenders)
            lines = [f"{at}|{questions[at]['question']}|avoid: {', '.join(questions[at]['overlap'])}|old links: {questions[at]['links']}" for at in offenders]
            sink.write(json.dumps({"key": key, "system": REWRITE_SYSTEM, "prompt": f"The sheet's words for the tab {tab['tab']!r}. Rewrite:\n" + "\n".join(lines), "max_output": 500 + 80 * len(offenders)}, ensure_ascii=False) + "\n")
            counts["tabs"] += 1
    return dict(counts)


def final_tab(tab: dict, rewrites: dict[str, dict]) -> dict:
    """A tab's questions with the rewrites that pass the script: no header word left. One that still overlaps is left as first written."""
    from training.factory.overlap import overlap_of
    record = rewrites.get(f"rewrite|{tab['key']}")
    if record is None:
        return tab
    for raw in record["reply"].splitlines():
        parts = raw.strip().split("|")
        if len(parts) < 2 or not parts[0].strip().isdigit():
            continue
        at = int(parts[0])
        if at >= len(tab["questions"]):
            continue
        q = tab["questions"][at]
        new = parts[1].strip()
        if not new or overlap_of(new, set(q["header_words"])):
            q["rewrite_rejected"] = True
            continue
        q["first_written"] = q["question"]
        q["question"] = new
        q["overlap"] = []
        if len(parts) >= 3:
            q["links"] = parts[2].strip()
    return tab


# ---------------------------------------------------------------------------------------------- the second route
def route_plan(side: str, out_path: str) -> dict:
    from training.factory.prompts import ROUTE_SYSTEM
    done = read_journal(f"route_{side}")
    rewrites = read_journal(f"rewrite_{side}")
    records = {tab_key(r): r for r in load_tabs(side)}
    counts: Counter = Counter()
    with open(out_path, "w", encoding="utf-8", newline="\n") as sink:
        for path in sorted(glob.glob(stage_dir(side, "cand") + "*.json")):
            with open(path, encoding="utf-8") as source:
                tab = json.load(source)
            tab = final_tab(tab, rewrites)
            questions = [q["question"] for q in tab["questions"]] + [r["question"] for r in tab["refusals"]]
            if not questions:
                continue
            key = f"route|{tab['key']}"
            if key in done:
                counts["done"] += 1
                continue
            record = records[tab["key"]]
            siblings = ""
            if record["features"]["families"]:
                siblings = "\nOther tabs of this workbook hold the same layout; R(name) reads them: " + "; ".join(repr(r["tab"]) for r in records.values() if r["book"] == record["book"] and r["tab"] != record["tab"])[:600]
            prompt = record["rows_view"] + siblings + "\n\nQuestions:\n" + "\n".join(f"{at}|{text}" for at, text in enumerate(questions))
            sink.write(json.dumps({"key": key, "system": ROUTE_SYSTEM, "prompt": prompt, "max_output": 2000 + 170 * len(questions)}, ensure_ascii=False) + "\n")
            counts["tabs"] += 1
            counts["questions"] += len(questions)
    return dict(counts)


def _sandbox(book: str, tab_name: str, programs: dict[str, str], timeout: float = 90.0) -> dict:
    """Run programs against a workbook's cells in a separate restricted process, in the system temp folder, no imports from here."""
    if not programs:
        return {}
    with tempfile.TemporaryDirectory(prefix="factory_sb_") as folder:
        path = os.path.join(folder, "programs.json")
        with open(path, "w", encoding="utf-8") as sink:
            json.dump({"tab": tab_name, "programs": programs}, sink)
        try:
            done = subprocess.run([sys.executable, "-I", "-X", "utf8", os.path.join(HERE, "sandbox_run.py"), f"{MODELS}{book}.grids.json.gz", path], capture_output=True, text=True, encoding="utf-8", timeout=timeout, cwd=folder)
            return json.loads(done.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            if len(programs) == 1:
                return {key: {"error": "timeout"} for key in programs}
            out = {}
            for key, text in programs.items():
                out.update(_sandbox(book, tab_name, {key: text}, 20.0))
            return out
        except (ValueError, IndexError):
            return {key: {"error": "no output"} for key in programs}


def route_tab(job: tuple) -> dict:
    tab_path, reply, out_path = job
    with open(tab_path, encoding="utf-8") as source:
        tab = json.load(source)
    programs: dict[str, str] = {}
    asks: dict[str, str] = {}
    for raw in reply.splitlines():
        line = raw.strip().strip("`")
        match = re.match(r"^(\d+)\|(.*)$", line)
        if not match:
            continue
        body = match[2].strip()
        if body.upper().startswith("ASK"):
            asks[match[1]] = body[3:].strip()[:160]
        elif body:
            programs[match[1]] = body
    results = _sandbox(tab["book"], tab["tab"], programs)
    out = {"programs": programs, "asks": asks, "results": results}
    with open(out_path + ".tmp", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(out, sink, ensure_ascii=False)
    os.replace(out_path + ".tmp", out_path)
    return {"tabs": 1, "programs": len(programs), "asks": len(asks), "errors": sum(1 for r in results.values() if "error" in r)}


def route_run(side: str, workers: int) -> dict:
    journal = read_journal(f"route_{side}")
    cand, folder = stage_dir(side, "cand"), stage_dir(side, "route")
    jobs = []
    for path in sorted(glob.glob(cand + "*.json")):
        name = os.path.basename(path)
        target = folder + name
        if os.path.exists(target):
            continue
        with open(path, encoding="utf-8") as source:
            key = json.load(source)["key"]
        if f"route|{key}" in journal:
            jobs.append((path, journal[f"route|{key}"]["reply"], target))
    totals: Counter = Counter()
    if jobs:
        with ProcessPoolExecutor(workers) as pool:
            for result in pool.map(route_tab, jobs, chunksize=8):
                totals.update(result)
    return dict(totals)


# ----------------------------------------------------------------------------------------------------- command line
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--side", required=True)
    parser.add_argument("--tabs", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    pin()
    os.makedirs(STAGED + args.side, exist_ok=True)
    requests = f"{STAGED}{args.side}/{{}}.requests.jsonl"
    started = time.time()
    if args.command == "author-plan":
        result = author_plan(args.side, args.tabs, requests.format("author"))
    elif args.command == "execute":
        result = execute(args.side, args.workers)
    elif args.command == "rewrite-plan":
        result = rewrite_plan(args.side, requests.format("rewrite"))
    elif args.command == "route-plan":
        result = route_plan(args.side, requests.format("route"))
    elif args.command == "route-run":
        result = route_run(args.side, args.workers)
    else:
        sys.exit("unknown command")
    print(args.command, args.side, json.dumps(result, sort_keys=True), round(time.time() - started), "s", flush=True)


if __name__ == "__main__":
    main()

