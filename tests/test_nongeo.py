"""NON-GEO world join over pre-synchronized facts. An uploaded table of a non-geo type (hospital/...)
joins its faithful world table, filtered by country, aggregating the uploaded metric; entities not in
world.words abstain (serving never fetches or writes shared facts). Live world Postgres.

  Needs a synced world Postgres (docker-compose + db/sync) and KB_PG_* env vars set.
  python -m tests.test_nongeo
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

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
    from regress.live_schema import live_schema
    schema = live_schema().name
    fails = []
    # -ies PLURALS must name the type (regression: the question gate matched only "<type>s?", so
    # "universities" never matched "university" and the whole family fell to the clarify path even
    # though the cells grounded). Positive UK + contrastive US on the same table.
    UNI = {"name": "applications", "columns": ["university", "applicants"], "rows": [
        ["Arts University Plymouth", 90], ["Bath Spa University", 60],          # Q145 (UK)
        ["Adelphi University", 120], ["Adams State University", 80]]}           # Q30 (US)
    for country, want in (("United Kingdom", 150), ("United States", 200)):
        ru = Q.serve([UNI], f"total applicants for universities in {country}", schema=schema)
        gu = _scalar(ru)
        print(f"applicants, {country} universities -> {gu} (exp {want})  model={(ru or {}).get('model','')[:46]}")
        if gu != want:
            fails.append(f"plural 'universities' {country} != {want} (got {gu})")

    # SUM the uploaded metric over US hospitals (every entity pre-synchronized in words)
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
    exact_table = {"name": "hospital_fees", "columns": ["hospital", "commission"], "rows": [
        ["Massachusetts General Hospital", "9007199254740993.1"],
        ["Cleveland Clinic", "0.1"], ["Johns Hopkins Hospital", "0.1"],
        ["Charite", "9999999999999999.9"],
    ]}
    exact_result = Q.serve(
        [exact_table], "total commission for hospitals in United States", schema=schema,
    )
    exact_rows = (exact_result or {}).get("result", {}).get("rows") or []
    exact_value = exact_rows[0][0] if exact_rows and exact_rows[0] else None
    print(f"exact commission, US     -> {exact_value} (exp 9007199254740993.3)")
    try:
        exact_ok = Decimal(str(exact_value)) == Decimal("9007199254740993.3")
    except Exception:  # noqa: BLE001
        exact_ok = False
    if not exact_ok or not isinstance(exact_value, str):
        fails.append(f"exact non-geo SUM rounded or used unsafe wire type (got {exact_value!r})")
    print("\n" + ("PASS — non-geo world join over pre-synchronized facts works" if not fails
                  else "FAIL:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
