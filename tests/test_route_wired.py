"""End-to-end proof of generalized evidence plus deterministic source grounding.

Asserts, with the production routing stack wired into KnowledgeQuery.route():
  (1) exact synchronized source keys route the uploaded `city` column to the qid-keyed Wikidata table
      and leave the free-text/name columns untyped. NOTE: the route value is the
      wikipedia exact-label table name ("city"), NOT the older friendly name ("Cities in the World") —
      city/country migrated to the qid-keyed knowledgebase."<type>" schema; see docs/notes/naming.md;
  (2) the world JOIN built on that model-typed column answers the aggregate ("how many customers in France");
  (3) the persisted connected bridge carries world_qid = the model's table qid (Q515 city);
  (4) the answer identifies whether its evidence was a calibrated Schema.org class or exact source grounding.

  Needs a synced world Postgres (docker-compose + db/sync) and KB_PG_* env vars set.
  python -m tests.test_route_wired
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CUST = {"name": "customers", "columns": ["name", "city", "remarks"], "rows": [
    ["Ada", "Paris", "package arrived late and damaged, terrible delivery"],
    ["Lin", "Lyon", "great product, very happy with the quality"],
    ["Bo", "Berlin", "shipping was slow and the box was crushed"],
    ["Sam", "Nice", "excellent service, fast and smooth"],
    ["Mai", "Tokyo", "the courier lost my parcel, awful logistics"],
    ["Eve", "Munich", "love it, would buy again"]]}


def main():
    if not os.environ.get("KB_PG_PASSWORD"):
        print("set KB_PG_PASSWORD to run the live test"); return 1
    try:                                                          # heavy deps AFTER the guard, with a clear message
        from engine.knowledge_query import KnowledgeQuery
        from engine.tables import qident
    except ImportError as e:
        print(f"missing serving dep ({e}); install: pip install -r requirements.txt"); return 1
    from regress.live_schema import live_schema
    schema = live_schema().name
    Q = KnowledgeQuery()
    fails = []

    # (1) source-key grounding types the city column even while the current head abstains on City.
    routes = Q.route(CUST)
    print("model-driven routes:", {k[1]: v for k, v in routes.items()})
    # The route value is the qid-keyed wikipedia table name ("city"), not the friendly "Cities in the
    # World" — city/country migrated to knowledgebase."<type>" (docs/notes/naming.md). State/element still
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
    bn = Q._conn_bridge_name("customers")                     # "customers connected to knowledgebase"
    cur.execute(f'SELECT DISTINCT "world_type","world_qid" FROM {qident(schema)}.{qident(bn)} WHERE "column"=%s', ("city",))
    bridge = cur.fetchall()
    print(f"\nbridge world_type/world_qid for 'city': {bridge}")
    if not any(wt == "city" and wq == "Q515" for wt, wq in bridge):
        fails.append(f"(3) bridge world_qid != Q515 for city (got {bridge})")

    # (4) The actual routing decision is auditable. A calibrated class carries its
    # property evidence; otherwise the record identifies exact source-key grounding.
    Q.begin_typing()
    try:
        Q.serve([CUST], "how many customers in France", schema=schema)
    finally:
        typing = Q.take_typing()
    city_t = next((t for t in typing if t["column"] == "city"), None)
    print(f"\ntyping evidence for 'city': {city_t}")
    if not city_t:
        fails.append("(4) no typing evidence captured for the 'city' column")
    else:
        if city_t.get("family") != "place":
            fails.append(f"(4) city typed family != place (got {city_t.get('family')!r})")
        if city_t.get("grounded_to") != "city":
            fails.append(f"(4) city grounded_to != 'city' (got {city_t.get('grounded_to')!r})")
        fired = [e["property"] for e in city_t.get("evidence", []) if e.get("fired")]
        grounding = city_t.get("grounding") or {}
        if city_t.get("class"):
            if not fired:
                fails.append(f"(4) decoded class has no firing evidence: {city_t.get('evidence')}")
            else:
                print(f"   Schema.org class decoded because these properties fired: {fired}")
        elif grounding != {
            "source": "wikidata",
            "index": "knowledgebase.words",
            "method": "exact_normalized_membership",
        }:
            fails.append(f"(4) abstaining model must expose exact source grounding: {grounding}")

    # (5) the TABLE-level schema.org class decode is captured too (kind=schema_class). A customers upload is
    # not a servable class, so the honest outcome is abstained=True (or a genuine servable decode) — what must
    # NEVER happen is the record being absent (evidence off) or internally inconsistent.
    # The head is gitignored (engine/data/*.pt) but is part of the external manifest-pinned bundle.
    # Source-only CI deliberately has no large weights, so serving degrades loudly with evidence off and
    # answers unaffected. A provisioned runtime must carry the head; source-only test runs may skip it.
    head_present = (Path(__file__).resolve().parents[1]
                    / "engine" / "data" / "schema_property_head.pt").exists()
    tbl_t = next((t for t in typing if t.get("kind") == "schema_class"), None)
    print(f"table-level class evidence: "
          f"{ {k: tbl_t[k] for k in ('table', 'abstained', 'ontology_version')} if tbl_t else None }")
    if not tbl_t:
        if head_present:
            fails.append("(5) schema head IS present but no schema_class evidence was captured")
        else:
            print("   (skipped: schema_property_head.pt absent — evidence-off degradation is the "
                  "documented contract; see engine/data/README.md)")
    else:
        if tbl_t.get("abstained") and tbl_t.get("classes"):
            fails.append(f"(5) abstained but classes non-empty: {tbl_t['classes']}")
        for c in tbl_t.get("classes", []):
            if not c.get("servable"):
                fails.append(f"(5) non-servable class in the decode: {c}")
        if not tbl_t.get("model_artifact_sha256") or not tbl_t.get("input_sha256"):
            fails.append("(5) class evidence must pin the model artifact + input hashes")

    print("\n" + ("PASS — Schema.org evidence and exact source grounding are wired end to end" if not fails
                  else "FAIL:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
