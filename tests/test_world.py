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
    # Match the serving architecture: one KnowledgeReasoner owns the unified encoder,
    # Schema.org interpreter/router, and composed planner. Standalone Router and duplicate
    # ComposedKnowledgeQuery instances can load extra copies and inflate peak memory unnecessarily.
    wr = KnowledgeReasoner()
    qc = wr.composed
    r = wr.qw._router()
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

    # --- (J) a word that names a world type is never one of its members (2026-09-28) ---
    # 'how many countries are the customers in' answered 0: the resolver's embedding fallback read 'countries'
    # as China (0.803, over the 0.80 threshold), and that filter cancelled the country column the question
    # counts. Paris and Lyon are in France, Berlin in Germany, Tokyo in Japan.
    def first_cell(response):
        return (((response.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]

    for question in ("how many countries are the customers in", "how many countries"):
        response = served(sub, wr.serve, [CUST], question, sub)
        ok(f"type noun: '{question}' counts the customers' countries", str(first_cell(response)) == "3",
           f"got={first_cell(response)} views={[v.get('label') for v in response.get('views') or []]}")
    # Contrastive: a country's name still filters by that country (its label is the knowledgebase's,
    # "People's Republic of China").
    china = served(sub, wr.serve, [CUST], "how many customers in China", sub)
    filters = [view.get("label") for view in china.get("views") or [] if view.get("op") == "filter"]
    ok("type noun: 'how many customers in China' still filters China",
       str(first_cell(china)) == "0" and len(filters) == 1 and "China" in filters[0],
       f"got={first_cell(china)} filters={filters}")
    # Negative: 'which countries' names the type, not the United Kingdom (0.807 before).
    which = served(sub, wr.serve, [CUST], "which countries are the customers in", sub)
    listed = {str(row[0]) for row in (which.get("result") or {}).get("rows") or []}
    ok("type noun: 'which countries are the customers in' lists France, Germany and Japan",
       listed == {"France", "Germany", "Japan"}, f"rows={(which.get('result') or {}).get('rows')}")

    # --- (K) a comparison binds the attribute it names (2026-09-28) ---
    # 'What is the total sales in big cities with population over 1,000,000?' was served as an empty table of
    # cities: the comparison thresholded the sales total. Tokyo, Osaka and Nagoya have over 1,000,000 people;
    # Lyon (519,127) and Marseille (886,040) do not. Both programs run and are compared.
    CITY_SALES = {"name": "s", "columns": ["city", "sales"],
                  "rows": [["Tokyo", 100], ["Osaka", 200], ["Nagoya", 50], ["Lyon", 30], ["Marseille", 40]]}

    def rows_of(response):
        return (response.get("result") or {}).get("rows") or []

    for question, expected, condition in (
            ("What is the total sales in big cities with population over 1,000,000?", 350, "population > 1000000"),
            ("What is the total sales in cities with population under 1,000,000?", 70, "population < 1000000")):
        response = served(sub, wr.serve, [CITY_SALES], question, sub, mode="verify")
        filters = [view.get("label") for view in response.get("views") or [] if view.get("op") == "filter"]
        ok(f"threshold: '{question}' -> {expected}",
           rows_of(response) == [[expected]] and filters == [f"where {condition}"]
           and (response.get("execution") or {}).get("verified"),
           f"rows={rows_of(response)} filters={filters} error={response.get('error')}")
    # Contrast: a comparison on the measure thresholds each city's total.
    response = served(sub, wr.serve, [CITY_SALES], "cities with total sales over 100", sub)
    ok("threshold: 'cities with total sales over 100' keeps Osaka's total",
       [list(row) for row in rows_of(response)] in ([["Osaka", 200]], [["Osaka"]]), f"rows={rows_of(response)}")
    # Negative: an explicit grouping keeps one row per restricted city, and 'which cities' lists them.
    response = served(sub, wr.serve, [CITY_SALES],
                      "total sales by city for cities with population over 1,000,000", sub)
    ok("threshold: grouped by city keeps a row per big city",
       {tuple(row) for row in rows_of(response)} == {("Tokyo", 100), ("Osaka", 200), ("Nagoya", 50)},
       f"rows={rows_of(response)}")
    CITY_ORDERS = {"name": "c", "columns": ["city", "orders"],
                   "rows": [["Tokyo", 1], ["Osaka", 1], ["Nagoya", 1], ["Lyon", 1], ["Marseille", 1]]}
    response = served(sub, wr.serve, [CITY_ORDERS], "Which cities have a population greater than 1,000,000?", sub)
    ok("threshold: 'which cities have a population over 1,000,000' lists them",
       sorted(str(row[0]) for row in rows_of(response)) == ["Nagoya", "Osaka", "Tokyo"], f"rows={rows_of(response)}")

    # --- (L) a world measure sums a numerically typed column (2026-09-28) ---
    # knowledgebase."city" and "country" kept population as text through every rebuild, so "What is the total
    # population?" concatenated the populations, and once text sums were refused it was declined. The rebuild
    # now converges both to their declared bigint (db/sync/build_qid_world.py). The five cities hold 14,264,798
    # + 2,751,862 + 2,326,844 + 519,127 + 886,040 people. Section K holds the contrast: the big-cities total,
    # which compose reads from "Cities", stays 350.
    response = served(sub, wr.serve, [CITY_SALES], "What is the total population?", sub, mode="verify")
    ok("world measure: the five cities' total population is 20748671",
       [[str(cell) for cell in row] for row in rows_of(response)] == [["20748671"]]
       and (response.get("execution") or {}).get("verified"),
       f"rows={rows_of(response)} error={response.get('error')}")
    COUNTRY_SALES = {"name": "c", "columns": ["country", "sales"],
                     "rows": [["France", 120], ["Germany", 80], ["Japan", 60]]}
    cn = _pg(); cur = cn.cursor()
    cur.execute("SELECT sum(population) FROM public.country WHERE qid = ANY(%s)", (["Q142", "Q183", "Q17"],))
    expected = str(cur.fetchone()[0]); cn.close()
    response = served(sub, wr.serve, [COUNTRY_SALES], "What is the total population?", sub, mode="verify")
    ok("world measure: the three countries' total population is their synchronized sum",
       [[str(cell) for cell in row] for row in rows_of(response)] == [[expected]]
       and (response.get("execution") or {}).get("verified"),
       f"rows={rows_of(response)} expected={expected} error={response.get('error')}")
    # Negative: a text attribute is never summed; the currencies are listed with their row counts.
    response = served(sub, wr.serve, [CITY_SALES], "What is the total currency?", sub)
    ok("world measure: a text attribute (currency) is listed, never summed",
       {tuple(str(cell) for cell in row) for row in rows_of(response)} == {("JPY", "3"), ("EUR", "2")},
       f"rows={rows_of(response)} error={response.get('error')}")

    # --- (M) a noun an aggregate runs over is not a grouping (2026-09-28) ---
    # 'What is the average population of these cities?' and 'What is the total population of these cities
    # combined?' were served as per-city tables: compose grouped by every text column a question mentioned. A
    # noun named only as the set an aggregate runs over is one set, so the measure is one number. The five cities
    # hold 20,748,671 people, 4,149,734.2 on average.
    from decimal import Decimal

    for question, total in (("What is the average population of these cities?", Decimal("4149734.2")),
                            ("What is the total population of these cities combined?", Decimal("20748671"))):
        response = served(sub, wr.serve, [CITY_SALES], question, sub, mode="verify")
        got = rows_of(response)
        ok(f"aggregate domain: '{question}' is one number",
           len(got) == 1 and len(got[0]) == 1 and Decimal(str(got[0][0])) == total
           and (response.get("execution") or {}).get("verified"),
           f"rows={got} error={response.get('error')} reason={response.get('reason')}")
    # Contrast: a grouping cue keeps a row per city, and a grouping by a world attribute stays.
    response = served(sub, wr.serve, [CITY_SALES], "What is the population of each city?", sub)
    ok("aggregate domain: 'the population of each city' keeps a row per city",
       {tuple(str(cell) for cell in row) for row in rows_of(response)} == {
           ("Tokyo", "14264798"), ("Osaka", "2751862"), ("Nagoya", "2326844"), ("Lyon", "519127"),
           ("Marseille", "886040")}, f"rows={rows_of(response)}")
    response = served(sub, wr.serve, [CITY_SALES], "total sales by country", sub)
    ok("aggregate domain: 'total sales by country' keeps a row per country",
       {tuple(str(cell) for cell in row) for row in rows_of(response)} == {("Japan", "350"), ("France", "70")},
       f"rows={rows_of(response)}")
    # Negative: a ranking keeps the cities it ranks.
    response = served(sub, wr.serve, [CITY_SALES], "Which are the top 2 cities by population?", sub)
    ok("aggregate domain: 'the top 2 cities by population' ranks the cities",
       [str(row[0]) for row in rows_of(response)] == ["Tokyo", "Osaka"], f"rows={rows_of(response)}")

    print(f"\n{P}/{P+F} passed" + ("" if not F else f"  ({F} FAILED)"))
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
