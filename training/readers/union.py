"""Union of several models' candidate pools for the same questions: each question's candidates from all of them,
the same SQL kept once (at its better rank), in order of rank then log probability, as serving pools its stage-2
models (engine.readers.search.ReadingSearch.pool). The re-scorer then chooses among the union.

usage: python -m training.readers.union <candidates a.jsonl> <candidates b.jsonl> [...] <out.jsonl>
"""
import json
import sys


def merge(pools):
    """One pool from several pools of one question."""
    seen, merged = set(), []
    for candidate in sorted((c for pool in pools for c in pool), key=lambda c: (c["rank"], -c["score"])):
        identity = candidate["sql"] or candidate["read_as"]
        if identity not in seen:
            seen.add(identity)
            merged.append(candidate)
    return merged


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__.strip().splitlines()[-1])
    *paths, out_path = sys.argv[1:]
    records = {}
    for path in paths:
        for line in open(path, encoding="utf-8"):
            record = json.loads(line)
            records.setdefault((record["source"], record["index"]), []).append(record)
    with open(out_path, "w", encoding="utf-8", newline="\n") as sink:
        for parts in records.values():
            sink.write(json.dumps({**parts[0], "candidates": merge([p["candidates"] for p in parts])}) + "\n")
    print(f"{len(records)} questions -> {out_path}")


if __name__ == "__main__":
    main()
