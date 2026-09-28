"""Expanded world test suite (LIVE world Postgres). Covers the expanded world model: the synced type hierarchy,
the column router, population ranking, aggregates, and the lat/lng NEARBY geo primitive + composite views.

  Needs a synced world Postgres (docker-compose + db/sync) and KB_PG_* env vars set.
  python -m tests.test_world
"""
from __future__ import annotations

import os
import sys

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
    from engine.knowledge import KnowledgeReasoner
    from engine.knowledge_compose import ComposedKnowledgeQuery
    from engine.pg import _pg

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

    from regress.live_schema import live_schema, served
    sub = live_schema().name
    CUST = {"name": "customers", "columns": ["name", "city", "amount"],
            "rows": [["Ada", "Paris", 100], ["Bob", "Lyon", 80], ["Eve", "Berlin", 40], ["Sam", "Tokyo", 50]]}
    qc = ComposedKnowledgeQuery()
    wr = KnowledgeReasoner()
    # --- (C) aggregate baseline (world join) ---
    ra = served(sub, qc.serve, [CUST], "total amount in France", sub)
    av = (((ra.get("answer") or ra.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]
    ok("aggregate: total amount in France = 180", av == 180, f"got={av}")

    SALES = {"name": "sales", "columns": ["country", "amount"],
             "rows": [["France", 120], ["Germany", 80], ["China", 200], ["India", 50],
                      ["United States", 300], ["Brazil", 90], ["Japan", 60]]}
    # A world projection is a grouped total, as own-data and compose answer it: Asia totals 200 + 50 + 60.
    rt = served(sub, wr.serve, [SALES], "which continent has the highest total amount", sub)
    tv = (rt.get("result") or {}).get("rows") or []
    ok("ranking: highest grouped total returns Asia", tv == [["Asia", 310]], f"got={tv} error={rt.get('error')}")
    rc = served(sub, wr.serve, [SALES], "total amount by currency", sub)
    cv = (rc.get("result") or {}).get("rows") or []
    ok("currency: grouped totals use ISO codes", {tuple(row) for row in cv} == {
        ("BRL", 90), ("CNY", 200), ("EUR", 200), ("INR", 50), ("JPY", 60), ("USD", 300)},
       f"got={cv}")
    SAMPLES = {"name": "samples", "columns": ["element", "qty"],
               "rows": [["Hydrogen", 2], ["Oxygen", 1], ["Carbon", 3]]}
    rm = served(sub, wr.serve, [SAMPLES], "average atomic mass", sub)
    mv = (rm.get("result") or {}).get("rows") or []
    ok("elements: average atomic mass uses world mass", bool(mv) and abs(float(mv[0][0]) - 9.6726666667) < 1e-8,
       f"got={mv} error={rm.get('error')}")

    # --- (D) population ranking (existing world measure) ---
    rp = served(sub, qc.serve, [CUST], "top 3 cities by population", sub)
    pr = (rp.get("answer") or rp.get("result") or {}).get("rows") or []
    pops = [row[-1] for row in pr if isinstance(row[-1], (int, float))]
    ok("population: top cities sorted desc", len(pops) >= 2 and pops == sorted(pops, reverse=True), f"pops={pops}")

    # --- (E) lat/lng nearby ---
    rn = served(sub, wr.serve, [CUST], "big cities near Paris", sub)
    nr = (rn.get("result") or {}).get("rows") or []
    kms = [row[-1] for row in nr]
    ok("nearby: returns cities", len(nr) >= 3, f"n={len(nr)}")
    ok("nearby: ascending by km", kms == sorted(kms), f"kms={kms[:5]}")
    ok("nearby: reference resolved to Paris", (rn.get("reference") or {}).get("name", "").lower() == "paris")
    rn2 = served(sub, wr.serve, [CUST], "cities near Tokyo", sub)
    ok("nearby: Tokyo reference works", bool((rn2.get("result") or {}).get("rows")),
       f"ref={(rn2.get('reference') or {}).get('name')}")

    # --- (F) non-nearby delegates unchanged ---
    rd = served(sub, wr.serve, [CUST], "total amount in France", sub)
    dv = (((rd.get("answer") or rd.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]
    ok("delegate: KnowledgeReasoner passes aggregates through", dv == 180, f"got={dv}")

    # --- (G) the owner's orders: what reads as a count, a listing, or a search (2026-09-27) ---
    # 'who ordered a trench coat in France' was answered COUNT = 5 because the article 'a' carried the COUNT
    # intent, and 'everything in France' searched the free text for 'everything'. Served as production
    # serves them: an analysis context on a disposable schema.
    import csv
    from pathlib import Path

    from engine.closed_class import closed_class_words
    from engine.deterministic.context import analysis_execution_context

    closed = closed_class_words("who ordered a trench coat in France")
    ok("grammar: articles, wh-words and prepositions are closed-class", {"who", "a", "in"} <= closed and
       "trench" not in closed, f"closed={sorted(closed)}")
    ok("grammar: a negation is never ignorable", "not" not in closed_class_words("orders not in France"))
    path = Path(__file__).resolve().parents[1] / "web" / "public" / "dataset" / "customer-orders" / "orders.csv"
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    orders = {"name": "sales", "columns": rows[0], "rows": rows[1:]}

    def ask(question):
        return served(sub, wr.serve, [orders], question, sub)

    trench = ask("who ordered a trench coat in France")
    got = (trench.get("result") or {}).get("rows") or []
    ok("search: the order that mentions a trench coat, not a count",
       [str(row[0]) for row in got] == ["109"], f"rows={got} model={trench.get('model')}")
    listing = ask("everything in France")
    got = (listing.get("result") or {}).get("rows") or []
    ok("listing: every France order, through the lookup and the filter",
       sorted(str(row[0]) for row in got) == ["109", "110", "121", "122", "123"]
       and "filter" in [view.get("op") for view in listing.get("views") or []], f"rows={len(got)}")
    counted = ask("how many orders in France")
    dv = (((counted.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]
    ok("count: 'how many' still counts", str(dv) == "5", f"got={dv}")
    # A quoted cell value is an exact filter, not search text: 'Gold' is a tier and 'Paris' a city.
    gold = ask("gold customers in France")
    got = (gold.get("result") or {}).get("rows") or []
    ok("filter: gold customers in France are Lupin's three orders",
       sorted(str(row[0]) for row in got) == ["121", "122", "123"], f"rows={got}")
    paris = ask("who ordered a trench coat in Paris")
    got = (paris.get("result") or {}).get("rows") or []
    ok("search + filter: the Paris order that mentions a trench coat",
       [str(row[0]) for row in got] == ["109"], f"rows={got}")

    # --- (H) the trail as the user sees it (docs/SHEETS_AS_REASONING.md rules 3 and 5, 2026-09-27) ---
    # 'amount in France' opened with a 'combined' copy of the one uploaded sheet, and its lookup, filter and
    # result showed Q90 and Q142. One sheet starts at the reference lookup; displayed rows show labels while
    # the executed programs keep the stored QIDs. Paris is the sheet's one French city: orders 109, 110 and
    # 121-123 total 310 + 210 + 180 + 95 + 175 = 970.
    import re
    from types import SimpleNamespace

    from engine.pg import _TableQueryPg

    def qids(response):
        cells = [cell for view in response.get("views") or [] for row in view.get("rows") or [] for cell in row]
        cells += [cell for row in (response.get("result") or {}).get("rows") or [] for cell in row]
        return sorted({str(cell) for cell in cells if re.fullmatch(r"Q\d+", str(cell))})

    for question, answer in (("amount in France", None), ("total amount in France", "970")):
        shown = ask(question)
        views = shown.get("views") or []
        ok(f"rule 3: '{question}' starts at the reference lookup, reading the upload",
           bool(views) and views[0].get("op") == "world_join" and views[0].get("inputs") == ["sales"],
           f"views={[(view.get('name'), view.get('inputs')) for view in views]}")
        ok(f"rule 5: '{question}' shows no bare QID", not qids(shown), f"qids={qids(shown)}")
        ok(f"rule 5: '{question}' shows France and runs the stored QID",
           bool(views) and "France" in {str(cell) for row in views[0].get("rows") or [] for cell in row}
           and "'Q142'" in " ".join(str(view.get("sql") or "") for view in views))
        if answer is not None:
            dv = (((shown.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]
            ok(f"rule 5: '{question}' still totals the France orders", str(dv) == answer, f"got={dv}")
    try:
        with analysis_execution_context(None, sub, execution_mode=None):
            _TableQueryPg._execute_deterministic(
                None, {}, SimpleNamespace(tables=(SimpleNamespace(schema="knowledgebase"),)))
        refused = False
    except RuntimeError as exc:
        refused = "labels" in str(exc)
    ok("rule 5: the one executor refuses a world trail without its entity labels", refused)

    # --- (I) world projections on the served path, both programs compared (2026-09-27) ---
    # 'which continent has the highest total amount' failed in production with "world projection requires
    # typed projection bindings"; section (C) passed only because it served without an analysis context.
    def projected(tables, question):
        response = served(sub, wr.serve, tables, question, sub, mode="verify")
        return response, ((response.get("result") or {}).get("rows") or [])

    for tables, question, expected in (
        ([SALES], "which continent has the lowest total amount", [["South America", 90]]),
        # Two hops (city -> country -> continent): Paris 100 + Lyon 80 + Berlin 40 in Europe, Tokyo 50 in Asia.
        ([CUST], "which continent has the highest total amount", [["Europe", 220]]),
        # A sum over a double precision world column: exact, as both programs read it as NUMERIC.
        ([SAMPLES], "total atomic mass", [["29.018"]]),
    ):
        response, got = projected(tables, question)
        ok(f"projection: '{question}' on {tables[0]['name']}",
           [[str(cell) for cell in row] for row in got] == [[str(cell) for cell in row] for row in expected]
           and (response.get("execution") or {}).get("actual") == "verify" and not qids(response),
           f"got={got} execution={response.get('execution')} error={response.get('error')} qids={qids(response)}")
    # Negative: a question that names no world column is not a projection, and compose still owns 'which
    # continent is Tokyo in'. Both answers are unchanged.
    response, got = projected([SALES], "total amount in France")
    ok("projection: no world column named stays a filtered total", [[str(c) for c in r] for r in got] == [["120"]]
       and [view.get("op") for view in response.get("views") or []] == ["filter", "group_agg"], f"got={got}")
    response, got = projected([CUST], "which continent is Tokyo in")
    ok("projection: compose still answers 'which continent is Tokyo in'", got == [["Asia", 1]], f"got={got}")

    print(f"\n{P}/{P+F} passed" + ("" if not F else f"  ({F} FAILED)"))
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
