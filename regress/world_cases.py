"""World-model-join tier — the product's differentiator (resolve a value to Wikidata, join the world DB).

Needs a seeded world Postgres (KB_PG_PASSWORD + world/wikipedia schemas — the real db/sync seed; a
hermetic mini-seed for fast CI is a documented follow-up, see regress/README.md). Two parts, both gated on Postgres:

  * CURATED goldens run through the LIVE KnowledgeReasoner.serve (the /api/reason entry) — the canonical
    customers+orders "total amount in France" = 270 (the README flagship) and the France customer count.
  * the existing maintained oracle suites in tests/ (test_world, test_world_joins, test_route_wired,
    test_nongeo) — reused rather than re-authored so expectations stay in one place.

Authored, NOT run in the offline dev environment (no Postgres here). Runs where the seed exists.
"""
from __future__ import annotations
import os
import subprocess
import sys

# customers + orders: Ada/Paris + Lin/Lyon are FR (120+150=270); Bo/Berlin is DE. The France filter comes from
# resolving each city -> its country via the world DB, NOT from any column in the upload — the whole point.
CUSTOMERS = {"name": "customers", "columns": ["name", "city"],
             "rows": [["Ada", "Paris"], ["Lin", "Lyon"], ["Bo", "Berlin"]]}
ORDERS = {"name": "orders", "columns": ["customer", "amount"],
          "rows": [["Ada", 120], ["Lin", 150], ["Bo", 200]]}

CURATED = [
    {"name": "world_total_amount_france", "tables": [CUSTOMERS, ORDERS],
     "question": "total amount in France", "expect_scalar": 270,
     "note": "README flagship: city->country world join then SUM(amount) over French customers = 270."},
    {"name": "world_count_customers_france", "tables": [CUSTOMERS],
     "question": "how many customers in France", "expect_min": 2,
     "note": "world join resolves Paris+Lyon -> France; Berlin -> Germany. >=2 (robust to resolution)."},
]

ENGINE_SUITES = ["tests.test_world", "tests.test_world_joins", "tests.test_route_wired", "tests.test_nongeo"]


def _scalar(res):
    rows = (res or {}).get("result", {}).get("rows") or []
    if rows and rows[0]:
        try:
            return int(float(str(rows[0][0]).replace(",", "")))
        except (ValueError, TypeError):
            return rows[0][0]
    return None


def run():
    failed = []
    from engine.knowledge import KnowledgeReasoner
    sub = os.environ.get("AUTH_TEST_SUB", "regress_world")
    Q = KnowledgeReasoner()
    for c in CURATED:
        try:
            res = Q.serve([dict(t) for t in c["tables"]], c["question"], sub)
            got = _scalar(res)
            ok = (got == c["expect_scalar"]) if "expect_scalar" in c else \
                 (isinstance(got, int) and got >= c["expect_min"])
            print(f"  {'ok  ' if ok else 'FAIL'} {c['name']}: got {got}"
                  f" (want {c.get('expect_scalar', '>=' + str(c.get('expect_min')))})")
            if not ok:
                failed.append(c["name"])
        except Exception as e:                               # noqa: BLE001
            print(f"  FAIL {c['name']}: {type(e).__name__}: {e}")
            failed.append(c["name"])
    # reuse the maintained oracle suites
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for mod in ENGINE_SUITES:
        rc = subprocess.call([sys.executable, "-m", mod], cwd=root)
        print(f"  {'ok  ' if rc == 0 else 'FAIL'} {mod} (exit {rc})")
        if rc != 0:
            failed.append(mod)
    return failed
