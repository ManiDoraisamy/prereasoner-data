"""NON-GEO world join + LAZY Wikidata fill. An uploaded table of a non-geo type (hospital/...) joins its
faithful world table, filtered by country, aggregating the uploaded metric; entities not in world.words are
lazy-filled from Wikidata first. Live world Postgres.

  Needs a synced world Postgres (docker-compose + db/sync) and KB_PG_* env vars set.
  python -m tests.test_nongeo
"""
from __future__ import annotations
import os
import sys

HOSP = {"name": "hospitals", "columns": ["hospital", "beds"], "rows": [
    ["Massachusetts General Hospital", 100], ["Cleveland Clinic", 80],
    ["Johns Hopkins Hospital", 60], ["Charite", 50]]}              # Charite = Berlin/Germany -> excluded from US


def _scalar(res):
    rows = (res or {}).get("result", {}).get("rows") or []
    if rows and rows[0]:
        try:
            return int(float(str(rows[0][0]).replace(",", "")))
        except (ValueError, TypeError):
            return rows[0][0]
    return None


def main():
    if not os.environ.get("KB_PG_PASSWORD"):
        print("set KB_PG_PASSWORD"); return 1
    from engine.knowledge_query import KnowledgeQuery
    Q = KnowledgeQuery()
    schema = os.environ.get("AUTH_TEST_SUB", "nongeo_test")
    fails = []
    # SUM the uploaded metric over US hospitals (lazy-fills the ones not already in words)
    r1 = Q.serve([HOSP], "total beds for hospitals in United States", schema=schema)
    got1 = _scalar(r1)
    print(f"total beds, US hospitals -> {got1} (exp 240)  model={r1.get('model','')[:46]}")
    if got1 != 240:
        fails.append(f"SUM beds US != 240 (got {got1})")
    if "columns" not in (r1.get("result") or {}):                  # the client render reads result.columns (NOT .cols);
        fails.append("result missing 'columns' key — the UI table would render empty")  # the value alone isn't enough
    # COUNT US hospitals
    r2 = Q.serve([HOSP], "how many hospitals in United States", schema=schema)
    got2 = _scalar(r2)
    print(f"count US hospitals       -> {got2} (exp 3)")
    if got2 != 3:
        fails.append(f"COUNT US hospitals != 3 (got {got2})")
    print("\n" + ("PASS — non-geo world join + lazy Wikidata fill works" if not fails
                  else "FAIL:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
