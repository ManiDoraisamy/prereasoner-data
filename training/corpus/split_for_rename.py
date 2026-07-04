"""
gen20 — split the full-corpus clusters for parallel LLM renaming. A cheap proper-noun heuristic auto-marks the
non-entity clusters (codes/ids/times/hashes -> renamed='non-entity'); the entity-likely ones are written into N
compact chunk files for renamer agents. Merge happens in merge_renames.py.

  $env:PYTHONUTF8=1; python -m training.corpus.split_for_rename [N]
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent.parent / "training/data"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 4
CODE = re.compile(r"[@/\\:]|\.\w|\d{3,}|^[A-Z0-9_\-]{2,8}$")


def proper(v):
    s = str(v).strip()
    return bool(s) and s[:1].isalpha() and not CODE.search(s) and len(s.split()) <= 4 and len(s) <= 40


def main():
    cl = json.load(open(OUT / "clusters.json", encoding="utf-8"))
    ent, base = [], {}
    for c in cl:
        vals = c.get("values", [])
        if sum(proper(v) for v in vals) / max(1, len(vals)) >= 0.5 and len(vals) >= 2:
            ent.append({"cluster": c["cluster"], "name": c["name"], "headers": c["headers"][:6], "values": c["values"][:8]})
        else:
            base[str(c["cluster"])] = {"renamed": "non-entity", "is_entity": False, "wikidata_query": ""}
    json.dump(base, open(OUT / "renames_base.json", "w", encoding="utf-8"), indent=0)
    ent.sort(key=lambda c: -len([1]))                                      # keep order; chunk round-robin for balance
    chunks = [ent[i::N] for i in range(N)]
    for i, ch in enumerate(chunks):
        json.dump(ch, open(OUT / f"ent_chunk_{i}.json", "w", encoding="utf-8"), indent=0)
    print(f"{len(ent)} entity-likely clusters -> {N} chunks (~{len(chunks[0])} each); "
          f"{len(base)} non-entity auto-marked -> renames_base.json")


if __name__ == "__main__":
    main()
