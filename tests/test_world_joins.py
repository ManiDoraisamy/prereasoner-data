"""THOROUGH join coverage over EVERY independently-routable synced world table. The model (engine.router)
types the uploaded column to its world table; the planner joins it + filters/aggregates by a WORLD attribute
the upload never had.

Synced + joinable world tables (taxonomy leaves with a world_table, all populated): Cities (200,886), Countries
(209), States (175), Places (167,781). Places is the CITY-alternate (LEAF_TABLES['city']=['Cities','Places']) —
a city column routes to Cities, so Places is exercised via the city leaf, not independently. So the
independently-routed joins are:
  city    -> Cities    (filter by country / continent)
  country -> Countries (filter by continent — the attribute a bare country list lacks)
  u_s_state -> States  (filter by country — Lombardy/Sicily are in Italy)
Runs against the LIVE world Postgres on the consolidated single-model path.

  Needs a synced world Postgres (docker-compose + db/sync) and WORLD_PG_* env vars set.
  python -m tests.test_world_joins
"""
from __future__ import annotations
import os
import sys

CITY = {"name": "s", "columns": ["city", "amount"],
        "rows": [["Paris", 100], ["Lyon", 80], ["Berlin", 50], ["Nice", 40], ["Tokyo", 30]]}
COUNTRY = {"name": "s", "columns": ["country", "amount"],
           "rows": [["France", 100], ["Germany", 50], ["Japan", 80], ["Brazil", 40]]}
STATE = {"name": "s", "columns": ["state", "amount"],
         "rows": [["Lombardy", 100], ["Bavaria", 50], ["Texas", 80], ["Sicily", 30]]}

# (label, table, expected_table, question, expected_scalar)
CASES = [
    ("city->Cities (country filter)",   CITY,    "Cities in the World",    "total amount in France",   220),  # Paris+Lyon+Nice
    ("city->Cities (continent filter)", CITY,    "Cities in the World",    "total amount in Europe",   270),  # +Berlin, -Tokyo
    ("city->Cities (Asia)",             CITY,    "Cities in the World",    "total amount in Asia",      30),  # Tokyo
    ("country->Countries (continent)",  COUNTRY, "Countries in the World", "total amount in Asia",      80),  # Japan
    ("country->Countries (Europe)",     COUNTRY, "Countries in the World", "total amount in Europe",   150),  # France+Germany
    ("u_s_state->States (country)",     STATE,   "States in the World",    "total amount in Italy",    130),  # Lombardy+Sicily
]


def _scalar(res):
    rows = (res or {}).get("result", {}).get("rows") or []
    if rows and rows[0]:
        try:
            return int(float(str(rows[0][0]).replace(",", "")))
        except (ValueError, TypeError):
            return rows[0][0]
    return None


def main():
    if not os.environ.get("WORLD_PG_PASSWORD"):
        print("set WORLD_PG_PASSWORD to run"); return 1
    from engine.world_query import WorldQuery
    Q = WorldQuery()
    schema = os.environ.get("AUTH_TEST_SUB", "joins_test")
    npass = 0
    for label, tbl, exp_table, q, exp in CASES:
        routes = Q.route(tbl)
        routed = routes.get((tbl["name"], tbl["columns"][0]))
        res = Q.serve([tbl], q, schema=schema)
        got = _scalar(res)
        ok_route = routed == exp_table
        ok_val = got == exp and not res.get("clarify")
        ok = ok_route and ok_val
        npass += ok
        print(f"  {'OK ' if ok else 'XX '} {label:34s} route={routed!s:24s} {q!r} -> {got} (exp {exp})"
              + ("" if not res.get("clarify") else " [CLARIFY!]"))
    print(f"\n==== {npass}/{len(CASES)} world-table joins pass ====")
    return 1 if npass < len(CASES) else 0


if __name__ == "__main__":
    sys.exit(main())
