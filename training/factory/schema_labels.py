"""The schema.org labels of every column the factory's questions can read: Gemini names, per column, the basis property whose values it holds and, for named things, the schema.org class of its values. The reader's meaning tier is the owner's selected property basis (engine/data/alloc.json through engine/data/named_coordinates.json, never the 1,521-property catalogue); these labels are its teacher signal, and a value class gives a question's fine answer type (engine/named/names.py:fine_of), never a word list.

The prompt lists the basis; a property outside it, or a value class outside the fine answer types' classes, is dropped and counted. Every column of a tab with factory rows is asked, none allowed, so a missing label is a column Gemini said none for, not one it skipped.

usage: python -m training.factory.schema_labels plan --side train|val|pub_eia|pub_sec     FACTORY/staged/<side>/schema_labels.requests.jsonl
       (from the repository root: python training/factory/llm_run.py <that file> schema_labels_<side>)
       python -m training.factory.schema_labels write --side S                         FACTORY/out/<side>.schema_labels.jsonl
       python -m training.factory.schema_labels measure                                FACTORY/out/schema_labels_coverage.json
Prints counts only, never a cell, a header or a question.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter

from training.factory.factory import load_tabs, read_journal, tab_key
from training.factory.paths import OUT, STAGED

THRESHOLD = 6                  # a label's 1-9 at or above this counts (assemble.THRESHOLD)
MAX_COLUMNS = 45
SIDES = ("train", "val", "pub_eia", "pub_sec")
SYSTEM = """You say what each column of a working spreadsheet's table holds, in schema.org terms. Reply with lines only, one per column you are given:
<table>.<position> <property> <1-9> [val <Class> <1-9>]
property is the one of the schema.org properties listed below whose values this column holds, written exactly as listed; say none when none of them fits rather than force one. The first 1-9 is how sure you are that the property says what the column holds.
val is the schema.org class of the column's values when they are named things (people, organizations, places, products, events and the like), written exactly as schema.org writes it; leave val out for numbers, dates, codes and free text. The second 1-9 is how sure you are of the class."""
_LINE = re.compile(r"^([tfn]\d+)\.(\d+)\s+(\S+)(?:\s+([1-9]))?(?:\s+val\s+(\S+)\s+([1-9]))?\s*$")


def basis_names() -> tuple[list[str], set[str]]:
    """(the basis properties in their order, the classes that carry a fine answer type), from the reader's name set."""
    from engine.named.names import CLASS_PREFIX, fine_of, meaning_names
    from engine.named.names import _meaning
    return [name.split(":", 1)[1] for name in meaning_names()], {name[len(CLASS_PREFIX):] for name in _meaning()["fine_classes"] if fine_of(name)}


def labelled_keys(side: str) -> set[str]:
    """The tabs the factory wrote rows for: the ones a training step reads."""
    keys = set()
    with open(f"{OUT}{side}.labels.jsonl", encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if record:
                keys.add(record["key"])
    return keys


def plan(side: str) -> dict:
    properties, _ = basis_names()
    system = SYSTEM + "\n\nschema.org properties: " + " ".join(properties)
    wanted = labelled_keys(side)
    done = read_journal(f"schema_labels_{side}")
    planned = columns_asked = 0
    os.makedirs(f"{STAGED}{side}", exist_ok=True)
    with open(f"{STAGED}{side}/schema_labels.requests.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for record in sorted(load_tabs(side), key=tab_key):
            key = tab_key(record)
            if key not in wanted or f"schema_labels|{key}" in done:
                continue
            columns = [f"{table}.{row[0]}" for table, rows in record["columns"].items() for row in rows if row[3] != "spacer"][:MAX_COLUMNS]
            if not columns:
                continue
            prompt = f"{record['rows_view'][:6000]}\n\nCompiled tables:\n{record['slots_view']}\n\nColumns to name: {' '.join(columns)}"
            sink.write(json.dumps({"key": f"schema_labels|{key}", "system": system, "prompt": prompt, "max_output": 200 + 24 * len(columns)}, ensure_ascii=False) + "\n")
            planned += 1
            columns_asked += len(columns)
    return {"side": side, "tabs_with_rows": len(wanted), "planned": planned, "columns": columns_asked, "already": len(done)}


def parse(reply: str, properties: set[str], classes: set[str], counts: Counter) -> dict[str, dict]:
    """A reply's lines as labels by '<table>.<position>': {coordinates, conf, value_class, value_conf}. Names outside the basis are dropped and counted."""
    out = {}
    for raw in reply.splitlines():
        match = _LINE.match(raw.strip().strip("`*- "))
        if not match:
            counts["junk_lines"] += bool(raw.strip())
            continue
        table, position, name, conf, value, value_conf = match.groups()
        name = name.removeprefix("schema:")
        label = {"coordinates": [], "conf": int(conf or 0), "raw": name}
        if name.lower() == "none":
            counts["said_none"] += 1
        elif name in properties:
            label["coordinates"] = [f"schema:{name}"]
            counts["property"] += 1
        else:
            counts["property_outside_basis"] += 1
        if value:
            value = value.removeprefix("schema:")
            if value in classes:
                label["value_class"] = value
                label["value_conf"] = int(value_conf)
                counts["value_class"] += 1
            else:
                counts["class_outside_fine_types"] += 1
        out[f"{table}.{position}"] = label
    return out


def write(side: str) -> dict:
    properties, classes = basis_names()
    properties = set(properties)
    journal = read_journal(f"schema_labels_{side}")
    counts: Counter = Counter()
    with open(f"{OUT}{side}.schema_labels.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for key in sorted(journal):
            reply = journal[key].get("reply")
            if reply is None:
                counts["no_reply"] += 1
                continue
            labels = parse(reply, properties, classes, counts)
            counts["tabs"] += 1
            counts["columns"] += len(labels)
            sink.write(json.dumps({"key": key.split("|", 1)[1], "labels": labels}, ensure_ascii=False, sort_keys=True) + "\n")
    return {"side": side, **dict(counts)}


def measure() -> dict:
    report = {}
    for side in SIDES:
        path = f"{OUT}{side}.schema_labels.jsonl"
        if not os.path.exists(path):
            continue
        counts: Counter = Counter()
        properties: Counter = Counter()
        with open(path, encoding="utf-8") as source:
            for line in source:
                for label in json.loads(line)["labels"].values():
                    counts["columns"] += 1
                    strong = bool(label["coordinates"]) and label["conf"] >= THRESHOLD
                    counts["property_above_threshold"] += strong
                    if strong:
                        properties[label["coordinates"][0]] += 1
                    counts["value_class_above_threshold"] += label.get("value_conf", 0) >= THRESHOLD
        share = lambda a, b: round(a / b, 3) if b else None
        report[side] = {"columns": counts["columns"], "property_above_threshold": share(counts["property_above_threshold"], counts["columns"]), "value_class_above_threshold": share(counts["value_class_above_threshold"], counts["columns"]), "distinct_properties": len(properties)}
    with open(f"{OUT}schema_labels_coverage.json", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(report, sink, indent=1, sort_keys=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "write", "measure"))
    parser.add_argument("--side", choices=SIDES)
    args = parser.parse_args()
    result = plan(args.side) if args.command == "plan" else write(args.side) if args.command == "write" else measure()
    print(json.dumps(result, indent=0, sort_keys=True))


if __name__ == "__main__":
    main()
