"""The audit's draw and its tally. A separate reader, handed only the question, the claimed answer, the workbook's path and tab, reads the cells itself with no factory code and says right, wrong or unclear. Seed 20261106.

usage: python -m training.factory.audit draw [--size 200] [--parts 4]     FACTORY/audit/part_<n>.json
       python -m training.factory.audit tally                             FACTORY/audit/verdicts_<n>.json -> FACTORY/audit/result.json
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
from collections import Counter

from training.factory.paths import AUDIT_SEED, FACTORY, FILES, OUT

FOLDER = FACTORY + "audit/"


def draw(size: int, parts: int, extra: int) -> None:
    with open(f"{OUT}val.jsonl", encoding="utf-8") as source:
        rows = [json.loads(line) for line in source]
    gold = sorted((r for r in rows if r["label"] == "gold"), key=lambda r: r["id"])
    rng = random.Random(AUDIT_SEED)
    chosen = rng.sample(gold, min(size, len(gold)))
    others = []
    for label in ("ambiguous", "refusal"):
        pool = sorted((r for r in rows if r["label"] == label), key=lambda r: r["id"])
        others.extend(rng.sample(pool, min(extra, len(pool))))
    os.makedirs(FOLDER, exist_ok=True)
    packets = []
    for row in chosen:
        packets.append({"id": row["id"], "kind": "gold", "question": row["question"], "claimed_answer": row["answer"][:12], "workbook": FILES + row["file"], "tab": row["tab"], "role": row.get("role", "")})
    for row in others:
        packets.append({"id": row["id"], "kind": row["label"], "question": row["question"], "workbook": FILES + row["file"], "tab": row["tab"], "role": row.get("role", ""), **({"reading_one": row["answer"][:12], "reading_two": row["second_reading"]} if row["label"] == "ambiguous" else {"why_refused": row.get("why", "")})})
    for at in range(parts):
        with open(f"{FOLDER}part_{at}.json", "w", encoding="utf-8", newline="\n") as sink:
            json.dump(packets[at::parts], sink, ensure_ascii=False, indent=0)
    print("gold", len(chosen), "others", len(others), "parts", parts)


def tally() -> None:
    verdicts = []
    for path in sorted(glob.glob(FOLDER + "verdicts_*.json")):
        with open(path, encoding="utf-8") as source:
            verdicts.extend(json.load(source))
    kinds = {}
    for path in sorted(glob.glob(FOLDER + "part_*.json")):
        with open(path, encoding="utf-8") as source:
            kinds.update({row["id"]: row["kind"] for row in json.load(source)})
    counts = Counter((kinds.get(v["id"], "?"), v["verdict"]) for v in verdicts)
    gold = {verdict: n for (kind, verdict), n in counts.items() if kind == "gold"}
    judged = sum(gold.values())
    right = gold.get("right", 0)
    half = 1.96 * math.sqrt(right / judged * (1 - right / judged) / judged) if judged else 0
    result = {"gold": gold, "right_share_of_all": round(right / judged, 3) if judged else None, "right_share_excluding_unclear": round(right / max(1, judged - gold.get("unclear", 0)), 3), "half_width_95": round(half, 3), "all": {f"{k}|{v}": n for (k, v), n in sorted(counts.items())}, "bar": 0.90}
    with open(FOLDER + "result.json", "w", encoding="utf-8", newline="\n") as sink:
        json.dump(result, sink, indent=1, sort_keys=True)
    print(json.dumps(result, indent=0, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("draw", "tally"))
    parser.add_argument("--size", type=int, default=200)
    parser.add_argument("--parts", type=int, default=4)
    parser.add_argument("--extra", type=int, default=20)
    args = parser.parse_args()
    if args.command == "draw":
        draw(args.size, args.parts, args.extra)
    else:
        tally()


if __name__ == "__main__":
    main()
