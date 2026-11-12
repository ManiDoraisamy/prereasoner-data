"""How much of the sheets the name set reaches, per tier, on train and validation: the share of columns, of text columns' values, and of question links with a named coordinate at or above the threshold (assemble.THRESHOLD). Labels are Gemini's, over the side's own columns.

(a) structure: the compiler's role and kind are closed names, on every column; a link's word role and kind are closed names.
(b) meaning: a pack or schema.org name above threshold.
(c) own words: a column whose header has words of its own; a link whose words reach their target by rule (assemble.rule_reaches).
Then the union, and what is left, which the reader must ask about ("named or asked").

usage: python -m training.factory.coverage [side ...]    Writes FACTORY/out/coverage.json; prints it. No cell text.
"""
from __future__ import annotations

import json
import sys
from collections import Counter

from training.factory.paths import OUT


def own_name(row: list) -> bool:
    """A column record [position, name, kind, role, letter] whose name the sheet wrote: the compiler names a headerless column by its letter."""
    return any(ch.isalpha() for ch in row[1]) and row[1] != row[4]


def measure(side: str) -> dict:
    columns: Counter = Counter()
    pack: Counter = Counter()
    with open(f"{OUT}{side}.labels.jsonl", encoding="utf-8") as source:
        for line in source:
            tab = json.loads(line)
            labels = tab["labels"]
            for table, rows in tab["columns"].items():
                axis_labelled = any(key.startswith(f"{table}.a") and value["coordinates"] for key, value in labels.items())
                for row in rows:
                    position, name, kind, role = row[:4]
                    if role == "spacer":
                        continue
                    columns["all"] += 1
                    columns["structure"] += 1
                    label = labels.get(f"{table}.{position}")
                    coordinates = (label or {}).get("coordinates") or []
                    has_meaning = bool(coordinates) or (role == "peer" and axis_labelled)
                    has_words = own_name(row)
                    columns["labelled_by_gemini"] += label is not None or (role == "peer" and axis_labelled)
                    columns["meaning"] += has_meaning
                    columns["own_words"] += has_words
                    columns["meaning_or_words"] += has_meaning or has_words
                    for value in coordinates:
                        pack[value.split(":", 1)[0]] += 1
                    if kind == "text":
                        columns["text"] += 1
                        columns["text_value_meaning"] += bool((label or {}).get("value_coordinates"))
    links: Counter = Counter()
    kinds: Counter = Counter()
    with open(f"{OUT}{side}.jsonl", encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if row["label"] == "refusal":
                continue
            for link in row.get("links", ()):
                links["all"] += 1
                links["structure"] += 1
                kinds[link["kind"]] += 1
                meaning = bool(link.get("coordinates"))
                words = bool(link.get("own_words"))
                links["meaning"] += meaning
                links["own_words"] += words
                links["meaning_or_words"] += meaning or words or link["kind"] == "context"
                links["context"] += link["kind"] == "context"
    share = lambda part, whole: round(part / whole, 3) if whole else None
    return {
        "columns": {"n": columns["all"], "structure": share(columns["structure"], columns["all"]), "meaning_above_threshold": share(columns["meaning"], columns["all"]), "own_words": share(columns["own_words"], columns["all"]), "meaning_or_own_words": share(columns["meaning_or_words"], columns["all"]), "labelled_by_gemini": share(columns["labelled_by_gemini"], columns["all"])},
        "values": {"text_columns": columns["text"], "value_meaning_above_threshold": share(columns["text_value_meaning"], columns["text"])},
        "links": {"n": links["all"], "structure": share(links["structure"], links["all"]), "meaning_above_threshold": share(links["meaning"], links["all"]), "own_words_by_rule": share(links["own_words"], links["all"]), "context": share(links["context"], links["all"]), "meaning_or_words_or_context": share(links["meaning_or_words"], links["all"]), "by_kind": dict(kinds)},
        "meaning_names_used": dict(pack),
    }


def main() -> None:
    sides = sys.argv[1:] or ["train", "val"]
    report = {side: measure(side) for side in sides}
    with open(f"{OUT}coverage.json", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(report, sink, indent=1, sort_keys=True)
    print(json.dumps(report, indent=0, sort_keys=True))


if __name__ == "__main__":
    main()
