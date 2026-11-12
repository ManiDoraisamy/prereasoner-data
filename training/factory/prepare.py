"""Stage 1, no Gemini: read each chosen workbook, screen it, compile it at the pinned commit, and write its model, its plain grid and one record per tab with what Gemini may be shown.

usage (any cwd): python -m training.factory.prepare --side train|val [--workers 4] [--limit N]
Output: FACTORY/models/<book>.pkl.gz, FACTORY/models/<book>.grids.json.gz, FACTORY/tabs/<side>.jsonl. A restart skips what is done. Prints counts only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import pickle
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from training.factory.paths import FILES, MIN_FREE_BYTES, MODELS, NEVER, TABS, pin


CHUNK = 120


def book_id(name: str) -> str:
    return hashlib.blake2b(name.encode("utf-8"), digest_size=8).hexdigest()


def tab_tables(catalog, tab_name: str) -> list:
    """The catalog tables a question on this tab may read: the tab's own tables and notes, and a family it belongs to."""
    return [table for table in catalog.tables if table.tab == tab_name or (table.kind == "family" and _in_family(catalog, table, tab_name))]


def _in_family(catalog, table, tab_name: str) -> bool:
    return any(line.tab == tab_name for line in table.lines)


def prepare_book(pick: dict) -> dict:
    """One workbook: {file, status, rules?, tabs: [...]}."""
    name = pick["file"]
    if name.startswith(NEVER):
        return {"file": name, "status": "never"}
    commit = pin()
    from engine.program.catalog import Catalog
    from engine.sheet import compile_workbook, workbook_from_json
    from engine.sheet.xlsx_reader import read_workbook
    from training.factory.screen import SCREEN_VERSION, screen_workbook
    from training.factory.views import grid_rows, rows_view, slots_view
    bid = book_id(name)
    done_path = f"{MODELS}{bid}.tabs.json"
    if os.path.exists(done_path):
        with open(done_path, encoding="utf-8") as source:
            done = json.load(source)
        if done.get("screen_version") == SCREEN_VERSION and done.get("commit") == commit:
            return done
    out = {"file": name, "book": bid, "family": pick["family"], "rank": pick.get("rank", 0), "status": "ok", "tabs": [], "screen_version": SCREEN_VERSION, "commit": commit}
    try:
        raw = read_workbook(pick.get("path") or FILES + name)
    except Exception as exc:  # noqa: BLE001 - a workbook the reader cannot read is counted and left out
        out.update(status="unreadable", why=type(exc).__name__)
        return _finish(done_path, out)
    if pick.get("screen_by_tab"):
        # A public company's machine-rendered filing: words such as compensation or employee sit in its statements as accounts, not people. Only tabs that pass whole are kept; the others are never compiled, shown or sent.
        kept = [tab for tab in raw["tabs"] if not screen_workbook({"tabs": [tab]})]
        out["tabs_screened"] = len(raw["tabs"]) - len(kept)
        raw["tabs"] = kept
        fired = [] if kept else ["every_tab"]
    else:
        fired = screen_workbook(raw)
    if fired:
        out.update(status="screened", rules=fired)
        return _finish(done_path, out)
    try:
        model = compile_workbook(workbook_from_json(raw))
        catalog = Catalog.from_model(model)
    except Exception as exc:  # noqa: BLE001 - counted: Phase 1's bar is no crash, and a crash here is reported to its owner
        out.update(status="compile_failed", why=type(exc).__name__)
        return _finish(done_path, out)
    with gzip.open(f"{MODELS}{bid}.pkl.gz.tmp", "wb", compresslevel=3) as sink:
        pickle.dump(model, sink, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(f"{MODELS}{bid}.pkl.gz.tmp", f"{MODELS}{bid}.pkl.gz")
    grids = {tab.name: grid_rows(tab) for tab in model.tabs if tab.sheet_type == "grid"}
    with gzip.open(f"{MODELS}{bid}.grids.json.gz.tmp", "wt", encoding="utf-8", compresslevel=3) as sink:
        json.dump([{"name": tab, "rows": rows} for tab, rows in grids.items()], sink, ensure_ascii=False)
    os.replace(f"{MODELS}{bid}.grids.json.gz.tmp", f"{MODELS}{bid}.grids.json.gz")
    wanted = set(pick["tabs"]) if pick["tabs"] is not None else None
    taken = 0
    for tab in model.tabs:
        if tab.sheet_type != "grid" or tab.name not in grids or (wanted is not None and tab.name not in wanted):
            continue
        if wanted is None:          # a public workbook: its first filled tabs, up to its cap
            if taken >= pick.get("max_tabs", 4) or tab.hidden or sum(1 for row in grids[tab.name] if any(v != "" for v in row)) < 5:
                continue
            taken += 1
        tables = [t for t in tab_tables(catalog, tab.name) if t.lines]
        features = {"tables": len(tables), "columns": sum(len(t.columns) for t in tables), "lines": sum(len(t.lines) for t in tables),
                    "peers": sum(1 for t in tables if t.table.axes), "bands": sum(1 for t in tables if any(t.section_texts)), "printed": sum(1 for t in tables if not all(t.is_data)),
                    "dates": sum(1 for t in tables for c in t.columns if c.kind in ("date", "period")), "numbers": sum(1 for t in tables for c in t.columns if c.kind == "number"),
                    "texts": sum(1 for t in tables for c in t.columns if c.kind == "text"), "notes": sum(1 for t in tables if t.kind == "notes"), "families": sum(1 for t in tables if t.kind == "family")}
        out["tabs"].append({"tab": tab.name, "features": features, "rows_view": rows_view(tab.name, grids[tab.name]), "slots_view": slots_view(tables),
                            "tables": [t.id for t in tables], "columns": {t.id: [[c.position, c.name, c.kind, c.role, c.letter] for c in t.columns] for t in tables}})
    return _finish(done_path, out)


def _finish(path: str, out: dict) -> dict:
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(out, sink, ensure_ascii=False)
    os.replace(path + ".tmp", path)
    return out


def run(picks: list[dict], side: str, workers: int) -> dict:
    os.makedirs(MODELS, exist_ok=True)
    os.makedirs(TABS, exist_ok=True)
    counts: dict[str, int] = {}
    rules: dict[str, int] = {}
    path = f"{TABS}{side}.jsonl"
    started = time.time()
    at = 0
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as sink:
        # A fresh pool per chunk gives memory back: max_tasks_per_child stops replacing workers on this Python (200 tasks at 4 workers x 50, then a hang).
        for first in range(0, len(picks), CHUNK):
            if shutil.disk_usage(MODELS).free < MIN_FREE_BYTES:
                print("under the disk floor: stopping", flush=True)
                break
            with ProcessPoolExecutor(workers) as pool:
                futures = [pool.submit(prepare_book, pick) for pick in picks[first:first + CHUNK]]
                for future in as_completed(futures):
                    at += 1
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001 - a worker lost to memory counts as one lost book
                        counts[f"lost_{type(exc).__name__}"] = counts.get(f"lost_{type(exc).__name__}", 0) + 1
                        continue
                    counts[result["status"]] = counts.get(result["status"], 0) + 1
                    for rule in result.get("rules", ()):
                        rules[rule] = rules.get(rule, 0) + 1
                    counts["tabs"] = counts.get("tabs", 0) + len(result.get("tabs", ()))
                    for tab in result.get("tabs", ()):
                        record = {key: result[key] for key in ("file", "book", "family", "rank")}
                        record.update(tab)
                        sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(at, "of", len(picks), counts, round(time.time() - started), "s", flush=True)
    os.replace(path + ".tmp", path)
    return {"counts": counts, "rules": rules, "seconds": round(time.time() - started)}


def main() -> None:
    from training.factory.paths import write_manifest
    from training.factory.split import picks
    parser = argparse.ArgumentParser()
    parser.add_argument("--side", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    pin()
    chosen = sorted(picks(args.side), key=lambda p: (p["rank"], p["file"]))
    if args.limit:
        chosen = chosen[:args.limit]
    print("workbooks", len(chosen), "tabs in the profile", sum(len(p["tabs"]) for p in chosen), flush=True)
    result = run(chosen, args.side, args.workers)
    print(json.dumps(result, indent=0), flush=True)
    write_manifest({f"prepare_{args.side}": result})
    sys.stdout.flush()


if __name__ == "__main__":
    main()
