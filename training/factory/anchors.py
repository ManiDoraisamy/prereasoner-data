"""Free real truth: a text label beside a one-function formula whose stored answer the plain forms reproduce, on train families only. A person typed the label, wrote the formula and Excel stored the answer: real words, a real lookup or sum, no model. Used as extra link and lookup examples, not as questions.

Each anchor: label, the column header it reads, function, answer, and where it binds in the compiled model when the workbook has been compiled. A label and a header over one figure are also a two-axis lookup in a person's own words (lookup_words). Workbooks are screened as the factory's are; a family's identical anchors count once.

usage: python -m training.factory.anchors [--workers 4]     Writes FACTORY/out/anchors.jsonl and anchors.counts.json. Prints counts only.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import pickle
import re
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor

from training.factory.paths import FILES, MODELS, NEVER, OUT, pin
from training.factory.prepare import book_id

WORD = re.compile(r"[A-Za-z]{2,}")
CALL = re.compile(r"^=?\s*([A-Z][A-Z0-9.]*)\((.*)\)\s*$", re.S)
RANGE = re.compile(r"^(?:(?:'((?:[^']|'')+)'|([A-Za-z0-9_. ]+))!)?\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?$")
PLAIN = {"SUM": "sum", "AVERAGE": "avg", "MAX": "max", "MIN": "min", "COUNT": "count", "VLOOKUP": "lookup", "SUMIF": "sumif", "COUNTIF": "countif"}


def _col(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def _range(text: str):
    """(first row, first column, last row, last column, tab or None)."""
    match = RANGE.match(text.strip())
    if not match:
        return None
    c1, r1 = _col(match[3]), int(match[4])
    c2, r2 = (_col(match[5]), int(match[6])) if match[5] else (c1, r1)
    tab = match[1].replace("''", "'") if match[1] else match[2]
    return min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2), tab


def _args(text: str) -> list[str]:
    out, depth, quote, current = [], 0, False, []
    for ch in text:
        if ch == '"':
            quote = not quote
        if not quote:
            depth += ch in "(["
            depth -= ch in ")]"
            if ch == "," and depth == 0:
                out.append("".join(current).strip())
                current = []
                continue
        current.append(ch)
    out.append("".join(current).strip())
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(abs(b) * 1e-6, 1e-6)


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _replay(function: str, args: list[str], tabs: dict[str, list[list]], here: str, cached) -> bool | None:
    """Re-execute over the stored cells of its own tab, or another tab it names."""
    def rows_of(rng) -> list[list]:
        return tabs.get(rng[4] or here, [])

    def cell(rows, r, c):
        return rows[r - 1][c - 1] if 0 < r <= len(rows) and 0 < c <= len(rows[r - 1]) else ""

    if function in ("SUM", "AVERAGE", "MAX", "MIN", "COUNT"):
        numbers = []
        for arg in args:
            rng = _range(arg)
            if rng is None or (rng[2] - rng[0] + 1) * (rng[3] - rng[1] + 1) > 60000 or (rng[4] or here) not in tabs:
                return None
            for r in range(rng[0], rng[2] + 1):
                for c in range(rng[1], rng[3] + 1):
                    value = _number(cell(rows_of(rng), r, c))
                    if value is not None:
                        numbers.append(value)
        want = _number(cached)
        if want is None or not numbers:
            return None
        got = {"SUM": sum(numbers), "AVERAGE": sum(numbers) / len(numbers), "MAX": max(numbers), "MIN": min(numbers), "COUNT": float(len(numbers))}[function]
        return _close(got, want)
    if function == "VLOOKUP" and len(args) >= 4 and args[3].strip().upper() in ("FALSE", "0"):
        key, table = _range(args[0]), _range(args[1])
        try:
            index = int(args[2])
        except ValueError:
            return None
        if not key or not table or key[0] != key[2] or key[1] != key[3] or (table[4] or here) not in tabs or (key[4] or here) not in tabs:
            return None
        wanted = cell(rows_of(key), key[0], key[1])
        source = rows_of(table)
        for r in range(table[0], table[2] + 1):
            if str(cell(source, r, table[1])).strip().casefold() == str(wanted).strip().casefold() and wanted != "":
                got = cell(source, r, table[1] + index - 1)
                x, y = _number(got), _number(cached)
                return _close(x, y) if x is not None and y is not None else str(got).strip() == str(cached).strip()
        return None
    return None


def _is_word(value) -> bool:
    return isinstance(value, str) and WORD.search(value) is not None and not value.strip().startswith("#")


def _label_left(rows, r, c):
    for at in range(c - 1, max(0, c - 13), -1):
        value = rows[r - 1][at - 1] if at <= len(rows[r - 1]) else ""
        if _is_word(value):
            return " ".join(value.split())
    return None


def _header_above(rows, r, c, reach=30):
    for at in range(r - 1, max(0, r - reach - 1), -1):
        row = rows[at - 1]
        value = row[c - 1] if c <= len(row) else ""
        if _is_word(value):
            return " ".join(value.split())
    return None


def _bind(anchors: list[dict], book: str) -> None:
    """Where each anchor sits in the workbook's compiled model, if one was made: its table, column and line. A lookup a reader can be trained on names these."""
    path = f"{MODELS}{book}.pkl.gz"
    if not os.path.exists(path):
        return
    from engine.program.catalog import Catalog
    with gzip.open(path, "rb") as source:
        catalog = Catalog.from_model(pickle.load(source))
    places: dict[tuple, tuple] = {}
    for table in catalog.tables:
        if table.kind != "table":
            continue
        for line in table.lines:
            for position, column in enumerate(table.columns):
                if position in line.cells:
                    places[(table.tab, line.row, column.col)] = (table.id, position, column.name, line.role, " ".join(line.label.split())[:80])
    for anchor in anchors:
        place = places.get((anchor["tab"], anchor["address"][0], anchor["address"][1]))
        if place:
            anchor["binding"] = dict(zip(("table", "position", "column", "line_role", "line_label"), place))


def scan(pick: dict) -> list[dict]:
    name = pick["file"]
    if name.startswith(NEVER):
        return [{"status": "never"}]
    pin()
    from engine.sheet.xlsx_reader import read_workbook
    from training.factory.screen import screen_workbook
    try:
        raw = read_workbook(FILES + name)
    except Exception:  # noqa: BLE001 - counted
        return [{"status": "unreadable"}]
    if screen_workbook(raw):
        return [{"status": "screened"}]
    out = []
    tabs = {tab["name"]: tab.get("rows") or [] for tab in raw["tabs"]}
    for tab in raw["tabs"]:
        rows = tab.get("rows") or []
        for r0, c0, text in tab.get("formulas") or ():
            match = CALL.match(text or "")
            if not match or match[1] not in PLAIN:
                continue
            function = match[1]
            r, c = r0 + 1, c0 + 1
            cached = rows[r0][c0] if r0 < len(rows) and c0 < len(rows[r0]) else ""
            if cached == "" or (isinstance(cached, str) and cached.startswith("#")):
                continue
            args = _args(match[2])
            if _replay(function, args, tabs, tab["name"], cached) is not True:
                continue
            label = _label_left(rows, r, c)
            head = _header_above(rows, r, c)
            if not label and not head:
                continue
            reads = None
            for arg in reversed(args):
                rng = _range(arg)
                if rng and rng[1] == rng[3] and rng[2] > rng[0]:
                    reads = rng
                    break
            read_header = None
            if reads:
                read_header = _header_above(tabs.get(reads[4] or tab["name"], []), reads[0], reads[1], 8)
            out.append({"status": "anchor", "file": name, "family": str(pick["family"]), "tab": tab["name"], "address": [r, c], "label": label, "header_above": head, "reads_header": read_header, "function": PLAIN[function], "formula": text[:200], "answer": cached,
                        "form": "lookup" if function == "VLOOKUP" else "agg", "lookup_words": bool(label and head), "other_tab": any((_range(arg) or (0, 0, 0, 0, None))[4] for arg in args)})
    if out:
        pin()
        _bind(out, book_id(name))
    return out or [{"status": "no_anchor"}]


def main() -> None:
    from training.factory.split import all_train_workbooks
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    pin()
    books = all_train_workbooks()
    os.makedirs(OUT, exist_ok=True)
    counts: Counter = Counter()
    seen: set[tuple] = set()
    started = time.time()
    with open(f"{OUT}anchors.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for first in range(0, len(books), 200):
            with ProcessPoolExecutor(args.workers) as pool:
                for rows in pool.map(scan, books[first:first + 200], chunksize=4):
                    counts["books"] += 1
                    for row in rows:
                        status = row.pop("status")
                        counts[status] += 1
                        if status != "anchor":
                            continue
                        key = (row["family"], (row["label"] or "").casefold(), (row["header_above"] or "").casefold(), row["function"], str(row["answer"]))
                        if key in seen:
                            counts["family_repeat"] += 1
                            continue
                        seen.add(key)
                        counts["kept"] += 1
                        counts[f"form_{row['form']}"] += 1
                        counts[f"fn_{row['function']}"] += 1
                        counts["two_axis_words"] += row["lookup_words"]
                        counts["bound_to_model"] += "binding" in row
                        counts["other_tab"] += row["other_tab"]
                        counts["no_shared_word_label_header"] += bool(row["label"] and row["reads_header"] and not set(WORD.findall(row["label"].lower())) & set(WORD.findall(row["reads_header"].lower())))
                        sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(counts["books"], "of", len(books), dict(kept=counts["kept"]), round(time.time() - started), "s", flush=True)
    with open(f"{OUT}anchors.counts.json", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(dict(counts), sink, indent=1, sort_keys=True)
    print(json.dumps(dict(counts), sort_keys=True))


if __name__ == "__main__":
    main()
