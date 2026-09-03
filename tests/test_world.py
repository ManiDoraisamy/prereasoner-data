"""Expanded world test suite (LIVE world Postgres). Covers the expanded world model: the synced type hierarchy,
the column router, population ranking, aggregates, and the lat/lng NEARBY geo primitive + composite views.

  Needs a synced world Postgres (docker-compose + db/sync) and KB_PG_* env vars set.
  python -m tests.test_world
"""
from __future__ import annotations
import os
import sys
import time

P, F = 0, 0


def ok(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print(f"  PASS  {name}")
    else:
        F += 1; print(f"  FAIL  {name}  {detail}")


def main():
    if not os.environ.get("KB_PG_PASSWORD"):
        print("KB_PG_PASSWORD not set — skipping (live world Postgres)"); return
    from engine.pg import _pg
    from engine.knowledge_compose import ComposedKnowledgeQuery
    from engine.knowledge import KnowledgeReasoner

    # --- (A) expanded type hierarchy synced ---
    cn = _pg(); cur = cn.cursor()
    cur.execute('SELECT count(*) FILTER (WHERE is_leaf), count(*) FROM knowledgebase."types"')
    nleaf, ntot = cur.fetchone()
    ok("types: >=60 leaves synced", nleaf >= 60, f"leaves={nleaf}")
    ok("types: ancestors to root present", ntot > nleaf + 30, f"total={ntot}")
    cur.execute('SELECT world_table FROM knowledgebase."types" WHERE qid=%s', ("Q515",))
    ok("city -> Cities world_table", (cur.fetchone() or [None])[0] == "Cities")
    cur.execute('SELECT parent_qid FROM knowledgebase."types" WHERE qid=%s', ("Q16917",))
    ok("hospital has a parent chain", bool((cur.fetchone() or [None])[0]))
    cur.execute("SELECT count(*) FROM knowledgebase.\"words\" WHERE type='type'")
    ok("type labels in words", cur.fetchone()[0] >= 60)
    cn.close()

    # --- (B) column router types columns ---
    from engine.router import Router
    r = Router()
    o = r.route(["Mayo Clinic", "Cleveland Clinic", "Mount Sinai", "Johns Hopkins Hospital"], header="hospital")
    ok("router: hospital emits only a calibrated servable class",
       o is None or r.decoder.classes[o["class"]]["servable"], f"got={o}")
    o2 = r.route(["Photoshop", "Microsoft Word", "Blender", "Visual Studio Code"], header="software")
    ok("router: software emits only a calibrated servable class",
       o2 is None or r.decoder.classes[o2["class"]]["servable"], f"got={o2}")
    ok("router: decoded output carries canonical evidence",
       all(x is None or (x.get("class", "").startswith("https://schema.org/") and x.get("evidence"))
           for x in (o, o2)))

    sub = f"test_world_{int(time.time())}"
    CUST = {"name": "customers", "columns": ["name", "city", "amount"],
            "rows": [["Ada", "Paris", 100], ["Bob", "Lyon", 80], ["Eve", "Berlin", 40], ["Sam", "Tokyo", 50]]}
    qc = ComposedKnowledgeQuery()
    wr = KnowledgeReasoner()

    # --- (C) aggregate baseline (world join) ---
    ra = qc.serve([CUST], "total amount in France", sub)
    av = (((ra.get("answer") or ra.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]
    ok("aggregate: total amount in France = 180", av == 180, f"got={av}")

    SALES = {"name": "sales", "columns": ["country", "amount"],
             "rows": [["France", 120], ["Germany", 80], ["China", 200], ["India", 50],
                      ["United States", 300], ["Brazil", 90], ["Japan", 60]]}
    rt = wr.serve([SALES], "which continent has the highest total amount", sub)
    tv = (rt.get("result") or {}).get("rows") or []
    ok("ranking: highest grouped total returns Asia", tv == [["Asia"]], f"got={tv}")
    rc = wr.serve([SALES], "total amount by currency", sub)
    cv = (rc.get("result") or {}).get("rows") or []
    ok("currency: grouped totals use ISO codes", {tuple(row) for row in cv} == {
        ("BRL", 90), ("CNY", 200), ("EUR", 200), ("INR", 50), ("JPY", 60), ("USD", 300)},
       f"got={cv}")
    SAMPLES = {"name": "samples", "columns": ["element", "qty"],
               "rows": [["Hydrogen", 2], ["Oxygen", 1], ["Carbon", 3]]}
    rm = wr.serve([SAMPLES], "average atomic mass", sub)
    mv = (rm.get("result") or {}).get("rows") or []
    ok("elements: average atomic mass uses world mass", bool(mv) and abs(float(mv[0][0]) - 9.6726666667) < 1e-8,
       f"got={mv}")

    # --- (D) population ranking (existing world measure) ---
    rp = qc.serve([CUST], "top 3 cities by population", sub)
    pr = (rp.get("answer") or rp.get("result") or {}).get("rows") or []
    pops = [row[-1] for row in pr if isinstance(row[-1], (int, float))]
    ok("population: top cities sorted desc", len(pops) >= 2 and pops == sorted(pops, reverse=True), f"pops={pops}")

    # --- (E) lat/lng nearby ---
    rn = wr.serve([CUST], "big cities near Paris", sub)
    nr = (rn.get("result") or {}).get("rows") or []
    kms = [row[-1] for row in nr]
    ok("nearby: returns cities", len(nr) >= 3, f"n={len(nr)}")
    ok("nearby: ascending by km", kms == sorted(kms), f"kms={kms[:5]}")
    ok("nearby: reference resolved to Paris", (rn.get("reference") or {}).get("name", "").lower() == "paris")
    rn2 = wr.serve([CUST], "cities near Tokyo", sub)
    ok("nearby: Tokyo reference works", bool((rn2.get("result") or {}).get("rows")),
       f"ref={(rn2.get('reference') or {}).get('name')}")

    # --- (F) non-nearby delegates unchanged ---
    rd = wr.serve([CUST], "total amount in France", sub)
    dv = (((rd.get("answer") or rd.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]
    ok("delegate: KnowledgeReasoner passes aggregates through", dv == 180, f"got={dv}")

    print(f"\n{P}/{P+F} passed" + ("" if not F else f"  ({F} FAILED)"))
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
