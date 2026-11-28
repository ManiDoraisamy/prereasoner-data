"""How often the engine's own answer is right in each band of the choice's probability (the audit of 2026-11-28,
spider/results/RESULTS.md), from the repository's own sets and records.

  python -X utf8 -m regress.served_gates bands [RUN SET SYSTEM]
      On a public run's records (default: 2026-11-27-four-marks wtq-fresh3 engine) and on the dev references: what
      each of the four marks (engine/program/closed.py T_ERR, T_FLAG, T_SURE) says against what its answers do.

The script also counted, until 2026-11-28, the dev questions that rested on the coverage pre-gate's fourth check; the
pre-gate is removed (engine/knowledge.py), and the served replay (regress/run_dev_decisions.py --served) had shown it
turned away none of them.
"""
from __future__ import annotations

import gzip
import json
import sys

from regress import eval_sets

BANDS = (0.0, 0.2, 0.3, 0.42, 0.5, 0.575, 0.65, 0.75, 0.85, 0.9, 0.95, 0.98, 1.01)


def bands(run: str, name: str, system: str) -> None:
    def table(label: str, rows: list[tuple[float | None, bool]]) -> None:
        print(label)
        for low, high in zip(BANDS, BANDS[1:]):
            group = [right for probability, right in rows if probability is not None and low <= probability < high]
            if group:
                print(f"  {low:.3f} to {high:.3f}: {len(group):4d} answers, {sum(group):4d} right ({sum(group) / len(group):.0%})")

    path = eval_sets.SETS / "public" / "records" / run / name / f"{system}.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as source:
        records = [row for row in map(json.loads, source) if row and row.get("kind") != "refused"]
    table(f"{run} {name} {system} ({len(records)} questions)", [(row.get("confidence"), bool(row.get("engine"))) for row in records])

    from regress.run_dev_decisions import SETS, pick_right, reference
    dev = [(row.get("mass"), pick_right(row)) for set_name in SETS if set_name != "demo" for row in reference(set_name).values()]
    table(f"the dev references ({len(dev)} questions)", dev)


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "bands":
        bands(*(sys.argv[2:5] if len(sys.argv) >= 5 else ("2026-11-27-four-marks", "wtq-fresh3", "engine")))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
