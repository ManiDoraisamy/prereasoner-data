"""The tokens spent, read from the journals. Standard library only: llm_run.py loads this file beside the readers' engine. Thought tokens count as output, as they bill."""
from __future__ import annotations

import glob
import json
import os

HARD_OUTPUT = 8_000_000
HARD_INPUT = 120_000_000
STOP_SHARE = 0.97


def spent(journal_dir: str) -> dict:
    """Totals per journal and overall: calls, prompt, output, thought."""
    out = {}
    total = {"calls": 0, "prompt": 0, "output": 0, "thought": 0}
    for path in sorted(glob.glob(os.path.join(journal_dir, "*.jsonl"))):
        row = {"calls": 0, "prompt": 0, "output": 0, "thought": 0}
        with open(path, encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                row["calls"] += 1
                row["prompt"] += record.get("prompt_tokens") or 0
                row["output"] += record.get("output_tokens") or 0
                # Whatever the total holds beyond prompt and reply bills as thought, named or not.
                row["thought"] += max(record.get("thought_tokens") or 0, (record.get("total_tokens") or 0) - (record.get("prompt_tokens") or 0) - (record.get("output_tokens") or 0))
        out[os.path.basename(path)[:-6]] = row
        for key in total:
            total[key] += row[key]
    out["TOTAL"] = total
    return out


def main() -> None:
    import sys
    report = spent(sys.argv[1])
    for name, row in report.items():
        print(f"{name:28s} calls {row['calls']:7d} prompt {row['prompt']:11d} output {row['output']:10d} thought {row['thought']:10d}")
    total = report["TOTAL"]
    print(f"output+thought {total['output'] + total['thought']:,} of {HARD_OUTPUT:,}; prompt {total['prompt']:,} of {HARD_INPUT:,}")


if __name__ == "__main__":
    main()
