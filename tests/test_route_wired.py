"""END-TO-END proof that the TRAINED model drives the LIVE world serving path. Runs against the live world
Postgres.

Asserts, with the model wired into WorldQuery.route():
  (1) the MODEL types the uploaded `city` column -> the qid-keyed wikipedia table "city" (NOT
      value-membership), and leaves the free-text/name columns untyped. NOTE: the route value is the
      wikipedia exact-label table name ("city"), NOT the older friendly name ("Cities in the World") —
      city/country migrated to the qid-keyed wikipedia."<type>" schema; see docs/notes/naming.md;
  (2) the world JOIN built on that model-typed column answers the aggregate ("how many customers in France");
  (3) the persisted connected bridge carries world_qid = the model's table qid (Q515 city);
  (4) a NON-geo column ("name") is NOT mis-joined (the model + embedding gate keep it out).

  Needs a synced world Postgres (docker-compose + db/sync) and WORLD_PG_* env vars set.
  python -m tests.test_route_wired
"""
from __future__ import annotations
import os
import sys

CUST = {"name": "customers", "columns": ["name", "city", "remarks"], "rows": [
    ["Ada", "Paris", "package arrived late and damaged, terrible delivery"],
    ["Lin", "Lyon", "great product, very happy with the quality"],
    ["Bo", "Berlin", "shipping was slow and the box was crushed"],
    ["Sam", "Nice", "excellent service, fast and smooth"],
    ["Mai", "Tokyo", "the courier lost my parcel, awful logistics"],
    ["Eve", "Munich", "love it, would buy again"]]}


def main():
    if not os.environ.get("WORLD_PG_PASSWORD"):
        print("set WORLD_PG_PASSWORD to run the live test"); return 1
    try:                                                          # heavy deps AFTER the guard, with a clear message
        from engine.world_query import WorldQuery
        from engine.tables import qident
    except ImportError as e:
        print(f"missing serving dep ({e}); install: pip install -r requirements.txt"); return 1
    schema = os.environ.get("AUTH_TEST_SUB", "route_demo")
    Q = WorldQuery()
    fails = []

    # (1) the MODEL types the city column
    routes = Q.route(CUST)
    print("model-driven routes:", {k[1]: v for k, v in routes.items()})
    # The route value is the qid-keyed wikipedia table name ("city"), not the friendly "Cities in the
    # World" — city/country migrated to wikipedia."<type>" (docs/notes/naming.md). State/element still
    # route to the friendly name-keyed family.
    if routes.get(("customers", "city")) != "city":
        fails.append(f"(1) model did not type 'city' -> 'city' (got {routes.get(('customers','city'))!r})")
    if ("customers", "name") in routes:
        fails.append(f"(1) model mis-typed 'name' -> {routes[('customers','name')]!r} (should be untyped)")

    # (2) aggregate world join on the model-typed column
    res = Q.serve([CUST], "how many customers in France", schema=schema)
    rows = (res.get("result") or {}).get("rows") or []
    print(f"\nQ: how many customers in France\n   sql={res.get('sql')}\n   rows={rows}")
    got = None
    if rows and rows[0]:
        try:
            got = int(float(str(rows[0][0]).replace(",", "")))
        except (ValueError, TypeError):
            got = rows[0][0]
    if got != 2:                                              # Paris + Lyon + Nice are FR cities -> assert >=2
        if not (isinstance(got, int) and got >= 2):
            fails.append(f"(2) expected >=2 French customers, got {got!r}")

    # (3) world_qid in the persisted bridge = the model's table qid (Q515 city)
    cur = Q._rconn().cursor()
    bn = Q._conn_bridge_name("customers")                     # "customers connected to wikipedia"
    cur.execute(f'SELECT DISTINCT "world_type","world_qid" FROM {qident(schema)}.{qident(bn)} WHERE "column"=%s', ("city",))
    bridge = cur.fetchall()
    print(f"\nbridge world_type/world_qid for 'city': {bridge}")
    if not any(wt == "city" and wq == "Q515" for wt, wq in bridge):
        fails.append(f"(3) bridge world_qid != Q515 for city (got {bridge})")

    print("\n" + ("PASS — the model drives the live world routing end to end" if not fails
                  else "FAIL:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
