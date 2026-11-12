"""The honest measure of tier (b): Gemini names EVERY column of a seeded sample of tabs, none allowed, because the author only labels columns a question could read. Train and validation apart. The share with a name at or above the threshold is the column coverage; the share with a specific name (not one of the pack's general ones) is reported beside it.

usage: python -m training.factory.label_sample plan --side train --tabs 300    FACTORY/staged/<side>/labels.requests.jsonl
       (llm_run.py <that file> labels_<side>)
       python -m training.factory.label_sample measure                         FACTORY/out/coverage_all_columns.json
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter

from training.factory.assemble import THRESHOLD, named, schema_names
from training.factory.factory import load_tabs, read_journal, tab_key, usable
from training.factory.paths import OUT, SEED, STAGED, pin
from training.factory.coverage import own_name
from training.factory.vocabulary import PACK

GENERIC = frozenset({"line_description", "description", "comment", "category", "code_general", "amount_money", "quantity_count", "price_general", "location_general", "person_name", "company_name", "flag", "reference_number", "sequence_number", "percent", "ratio"})
SYSTEM = """You say what each column of a working spreadsheet's table means, in named coordinates. Reply with lines only, one per column you are given: <table>.<position> <name> <1-9> [val <name> <1-9>]. name is a biz: name below, or a schema.org term as schema:term, or none when nothing there fits: say none rather than force a name. 1-9 is how sure you are that the name says what this column holds. val names what its text values are, when they are named things and that adds something.

biz: names: """ + " ".join(name for _, name, _ in PACK)
_LINE = re.compile(r"^(?:COL\s+)?([tfn]\d+)\.(\d+)\s+(\S+)\s+([1-9])(?:\s+val\s+(\S+)\s+([1-9]))?\s*$")
MAX_COLUMNS = 45


def plan(side: str, tabs: int) -> dict:
    records = [r for r in load_tabs(side) if usable(r)]
    records.sort(key=tab_key)
    random.Random(SEED + 7).shuffle(records)
    # One tab a family, so a big template cannot fill the sample.
    seen, chosen = set(), []
    for record in records:
        if str(record["family"]) not in seen:
            seen.add(str(record["family"]))
            chosen.append(record)
        if len(chosen) == tabs:
            break
    done = read_journal(f"labels_{side}")
    planned = 0
    with open(f"{STAGED}{side}/labels.requests.jsonl", "w", encoding="utf-8", newline="\n") as sink:
        for record in chosen:
            key = f"labels|{tab_key(record)}"
            if key in done:
                continue
            columns = [f"{table}.{row[0]}" for table, rows in record["columns"].items() for row in rows if row[3] != "spacer"][:MAX_COLUMNS]
            prompt = f"{record['rows_view'][:6000]}\n\nCompiled tables:\n{record['slots_view']}\n\nColumns to name: {' '.join(columns)}"
            sink.write(json.dumps({"key": key, "system": SYSTEM, "prompt": prompt, "max_output": 1500 + 22 * len(columns)}, ensure_ascii=False) + "\n")
            planned += 1
    return {"chosen": len(chosen), "planned": planned}


def measure() -> dict:
    schema = schema_names()
    report = {}
    for side in ("train", "val"):
        journal = read_journal(f"labels_{side}")
        records = {tab_key(r): r for r in load_tabs(side)}
        counts: Counter = Counter()
        used: Counter = Counter()
        unknown: Counter = Counter()
        for key, entry in journal.items():
            record = records[key.split("|", 1)[1]]
            asked = {(table, row[0]): row for table, rows in record["columns"].items() for row in rows if row[3] != "spacer"}
            answered = {}
            for line in entry["reply"].splitlines():
                match = _LINE.match(line.strip())
                if match:
                    answered[(match[1], int(match[2]))] = match
            for at, row in list(asked.items())[:MAX_COLUMNS]:
                counts["columns"] += 1
                match = answered.get(at)
                has_words = own_name(row)
                counts["own_words"] += has_words
                if match is None:
                    counts["unanswered"] += 1
                    counts["meaning_or_words"] += has_words
                    continue
                name = named(match[3], schema)
                if match[3].lower() == "none":
                    counts["said_none"] += 1
                elif name is None:
                    unknown[match[3]] += 1
                strong = name is not None and int(match[4]) >= THRESHOLD
                counts["meaning"] += strong
                counts["meaning_or_words"] += strong or has_words
                if strong:
                    used[name.split(":", 1)[0]] += 1
                    counts["specific"] += name.split(":", 1)[-1] not in GENERIC
                if row[2] == "text":
                    counts["text_columns"] += 1
                    value = named(match[5], schema) if match[5] and int(match[6]) >= THRESHOLD else None
                    counts["text_value_named"] += bool(value or strong)
        share = lambda a, b: round(a / b, 3) if b else None
        report[side] = {"tabs": len(journal), "columns": counts["columns"], "meaning_above_threshold": share(counts["meaning"], counts["columns"]), "specific_meaning": share(counts["specific"], counts["columns"]), "own_words": share(counts["own_words"], counts["columns"]), "meaning_or_own_words": share(counts["meaning_or_words"], counts["columns"]), "said_none": share(counts["said_none"], counts["columns"]), "unanswered": share(counts["unanswered"], counts["columns"]),
                        "text_columns": counts["text_columns"], "text_values_named": share(counts["text_value_named"], counts["text_columns"]), "names_by_namespace": dict(used), "names_outside_the_set": unknown.most_common(25)}
    with open(f"{OUT}coverage_all_columns.json", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(report, sink, indent=1, sort_keys=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "measure"))
    parser.add_argument("--side")
    parser.add_argument("--tabs", type=int, default=300)
    args = parser.parse_args()
    pin()
    print(json.dumps(plan(args.side, args.tabs) if args.command == "plan" else measure(), indent=0, sort_keys=True))


if __name__ == "__main__":
    main()
