"""Two counts the audit of 2026-11-28 reports (spider/results/RESULTS.md), from the repository's own sets and records.

  python -X utf8 -m regress.served_gates pregate messy freshdev wtq
      The served entry (engine/knowledge.py KnowledgeReasoner._serve) asks engine/knowledge_compose.py
      _has_data_signal whether a question is a data question before any path reads it. Its first three checks are
      plain: "how many/much", an English intent word (_DATA_INTENT), a word of a header or tab name. A question that
      passes none of them rests on the fourth, a guess that one of its words names a world entity, and is answered as
      chat when that guess fails. This counts, per dev set, the questions that pass none of the three. The dev replay
      and the public benchmark call TableQuery.select_query, below this gate, so they never see it.

  python -X utf8 -m regress.served_gates bands [RUN SET SYSTEM]
      How often the engine's own answer is right in each band of the choice's probability, on a public run's records
      (default: 2026-11-27-four-marks wtq-fresh3 engine) and on the dev references: what each of the four marks
      (engine/program/closed.py T_ERR, T_FLAG, T_SURE) says against what its answers do.
"""
from __future__ import annotations

import collections
import gzip
import inspect
import json
import re
import sys

from regress import eval_sets

BANDS = (0.0, 0.2, 0.3, 0.42, 0.5, 0.575, 0.65, 0.75, 0.85, 0.9, 0.95, 0.98, 1.01)


def pregate(names: list[str]) -> None:
    import engine.knowledge_compose as compose
    import regress.run_dev_decisions as dev

    holder = next(cls for _name, cls in inspect.getmembers(compose, inspect.isclass) if hasattr(cls, "_DATA_INTENT"))

    def passes(question: str, tables) -> str:
        text = (question or "").lower()
        if re.search(r"\bhow\s+(?:many|much)\b", text):
            return "how many or how much"
        words = set(re.findall(r"[^\W\d_]+", text))
        if words & holder._DATA_INTENT:
            return "an English intent word"
        schema = holder._schema_tokens(None, tables)
        if words & schema or any(not part.isascii() and part in text for part in schema):
            return "a header or tab word"
        return "none of the three"

    for name in names:
        counts, examples, held = collections.Counter(), [], {}
        items = dev.items_of(name, None)
        for item in items:
            if item.book not in held:
                held[item.book] = dev.tables_of(item)
            result = passes(item.question, held[item.book])
            counts[result] += 1
            if result == "none of the three" and len(examples) < 5:
                examples.append(item.question[:90])
        print(json.dumps({"set": name, "questions": len(items), **counts, "examples": examples}, ensure_ascii=False))


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
    if len(sys.argv) >= 2 and sys.argv[1] == "pregate":
        pregate(sys.argv[2:] or ["messy", "freshdev", "wtq"])
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "bands":
        bands(*(sys.argv[2:5] if len(sys.argv) >= 5 else ("2026-11-27-four-marks", "wtq-fresh3", "engine")))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
