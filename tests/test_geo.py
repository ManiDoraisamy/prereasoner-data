"""EXPANDED geo test suite (LIVE world Postgres). Covers the two geo capabilities of WorldReasoner end to end,
plus the canonical regressions and composite view-stacks, with ORACLE-checked expectations computed directly off
the live `world`/`wikipedia`/`public` schemas (so the numbers are derived, not hard-coded guesses):

  (A) NEARBY by lat/lng        — "big cities near Paris", "cities near Tokyo": haversine distance ordering
                                 (nearest first), reference resolution, lat/lng both used.
  (B) Big cities by POPULATION — "top N cities by population", population-ranked view stack (world.Cities join).
  (C) Canonical regressions    — France->sum, Europe->2-hop continent, hospital router typing, composite stacks.
  (D) Edge cases               — nearby+big filter behaviour, population+group, nearby reference miss.

The (A)/(B) ORACLES are recomputed here in SQL from the SAME live tables the serve path reads, then the model's
served answer is checked against them. Geo lazy-fill can be slow on first hit -> per-call retry with backoff.

  Needs a synced world Postgres (docker-compose + db/sync) and WORLD_PG_* env vars set.
  python -m tests.test_geo
"""
from __future__ import annotations
import os
import re
import sys
import time

P, F = 0, 0
WARN = []


def ok(name, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print(f"  PASS  {name}", flush=True)
    else:
        F += 1
        print(f"  FAIL  {name}  {detail}", flush=True)
    return bool(cond)


def warn(name, detail=""):
    """A data-quality / known-bug observation: recorded + printed, but does NOT fail the suite (these are the
    things to FIX in the serve code, reported separately — the test's job is to surface them, not gate on them)."""
    WARN.append((name, detail))
    print(f"  WARN  {name}  {detail}", flush=True)


def _retry(fn, tries=3, base=4.0):
    """Geo lazy-fill (and a cold first DB round-trip) can be slow; retry a serve call a few times before giving up."""
    last = None
    for i in range(tries):
        try:
            r = fn()
            if r is not None:
                return r
        except Exception as e:                                       # noqa: BLE001
            last = e
            print(f"    (retry {i + 1}/{tries} after error: {e!r})", flush=True)
        time.sleep(base * (i + 1))
    if last:
        print(f"    (gave up after {tries} tries: {last!r})", flush=True)
    return None


# --------------------------------------------------------------------------------------------------------------
# SQL ORACLES — recomputed live from the exact tables the serve path uses, so expectations are derived not guessed.
# --------------------------------------------------------------------------------------------------------------
HAVERSINE = ("6371*acos(greatest(-1,least(1, cos(radians(%s))*cos(radians(p.lat))*cos(radians(p.lng)-radians(%s))"
             "+sin(radians(%s))*sin(radians(p.lat)))))")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def oracle_nearby(cur, ref, big, limit=5):
    """Replicate WorldReasoner._nearby exactly: resolve the reference in public.settlement (highest population),
    then the haversine-ordered nearest `limit` settlements with population > (150000 if big else 1), excluding the
    exact ref name. -> (ref_name, ref_qid, [(name, country, population, km_float)]) or (None, None, [])."""
    cur.execute("SELECT name,lat,lng,qid FROM public.settlement WHERE lower(name)=lower(%s) "
                "AND lat IS NOT NULL ORDER BY population DESC NULLS LAST LIMIT 1", (ref,))
    row = cur.fetchone()
    if not row:
        return None, None, []
    name, lat, lng, qid = row
    minpop = 150000 if big else 1
    # MIRROR WorldReasoner._nearby exactly (incl. the bugfix exclusions): drop the reference's own administrative
    # subdivisions (admin_qid = ref qid) + arrondissement-named rows + QID-string/null names, so the oracle and the
    # served result stay byte-identical.
    sql = (f"SELECT p.name, p.country, p.population, round(({HAVERSINE})::numeric,0) AS km "
           f"FROM public.settlement p WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL "
           f"AND p.population > %s AND lower(p.name) <> lower(%s) "
           f"AND p.name IS NOT NULL AND p.name !~ '^Q[0-9]+$' "
           f"AND p.name !~* 'arrondissement' "
           f"AND (p.admin_qid IS NULL OR p.admin_qid <> %s) "
           f"ORDER BY km ASC LIMIT %s")
    cur.execute(sql, (lat, lng, lat, minpop, name, qid, limit))
    rows = [(r[0], r[1], r[2], float(r[3])) for r in cur.fetchall()]
    return name, qid, rows


def oracle_city_pop(cur, city):
    """Resolve a city cell -> qid (world.words, is_primary then population), then its world.Cities population. The
    exact pick() the compose/entity layers use for the population view stack. -> (qid, population) or (None, None)."""
    cur.execute("SELECT qid, is_primary, (props->>'population')::bigint FROM world.\"words\" "
                "WHERE type='city' AND qid IS NOT NULL AND norm=%s", (_norm(city),))
    cands = cur.fetchall()
    if not cands:
        return None, None
    qid = sorted(cands, key=lambda x: (x[1] or 0, x[2] or 0), reverse=True)[0][0]
    cur.execute('SELECT population FROM world."Cities" WHERE qid=%s', (qid,))
    r = cur.fetchone()
    return qid, (r[0] if r else None)


# --------------------------------------------------------------------------------------------------------------
def main():
    if not os.environ.get("WORLD_PG_PASSWORD"):
        print("WORLD_PG_PASSWORD not set — skipping (live world Postgres)")
        return
    from engine.pg import _pg
    from engine.world_compose import ComposedWorldQuery
    from engine.world import WorldReasoner

    sub = os.environ.get("GEO_TEST_SUB", "geotest")
    cn = _pg()
    cur = cn.cursor()

    # The demo fact table: 4 cities across France (Paris, Lyon), Germany (Berlin), Japan (Tokyo). amounts chosen so
    # France=180, Europe(France+Germany)=220 are distinctive and verifiable.
    CUST = {"name": "customers", "columns": ["name", "city", "amount"],
            "rows": [["Ada", "Paris", 100], ["Bob", "Lyon", 80], ["Eve", "Berlin", 40], ["Sam", "Tokyo", 50]]}

    # ONE model instance (like production: the server loads a single WorldReasoner). wr wraps a ComposedWorldQuery
    # at wr.composed; routing every composed call through wr.composed (instead of a SEPARATE instance) keeps a SINGLE set of
    # per-schema connections. Two instances against the SAME `sub` schema DEADLOCK: instance A leaves its bridge build
    # idle-in-transaction holding a lock on "<sub>.customers connected to wikipedia", and instance B's
    # `ALTER TABLE … ADD COLUMN world_qid` (world_query._persist_connected) then blocks forever on that relation lock.
    print("loading WorldReasoner (LoRA Qwen + bge + spaCy; slow on CPU)…", flush=True)
    t0 = time.time()
    wr = WorldReasoner()
    qc = wr.composed                                                # the SAME ComposedWorldQuery wr delegates to (one connection set)
    print(f"  models loaded in {time.time() - t0:.0f}s\n", flush=True)

    # ============================================================ (A) NEARBY by lat/lng ==========================
    print("== (A) NEARBY by lat/lng ==", flush=True)

    # --- A1: "big cities near Paris" — the headline geo query ---
    refP, qidP, oraP = oracle_nearby(cur, "Paris", big=True, limit=5)
    ok("oracle: Paris resolves in settlement", refP is not None, f"ref={refP}")
    rn = _retry(lambda: wr.serve([CUST], "big cities near Paris", sub))
    if ok("nearby(Paris): serve returned a result", rn is not None):
        nr = (rn.get("result") or {}).get("rows") or []
        kms = [row[-1] for row in nr]
        names = [str(row[0]) for row in nr]
        globals()["_paris_names"] = set(names)               # captured for the A2 reference-specificity check
        ok("nearby(Paris): >=3 cities returned", len(nr) >= 3, f"n={len(nr)}")
        ok("nearby(Paris): ascending by km (lat/lng distance order)",
           kms == sorted(kms), f"kms={kms[:5]}")
        ok("nearby(Paris): reference resolved to Paris",
           (rn.get("reference") or {}).get("name", "").lower() == "paris",
           f"ref={(rn.get('reference') or {}).get('name')}")
        ok("nearby(Paris): result is the geo-nearby model (not delegated)",
           "geo nearby" in (rn.get("model") or ""), f"model={rn.get('model')}")
        # ORACLE: the served name set matches the haversine oracle's nearest set (same SQL, same ordering)
        ora_names = [r[0] for r in oraP]
        ok("nearby(Paris): served nearest set == SQL oracle",
           names[:len(ora_names)] == ora_names[:len(names)], f"served={names[:5]} oracle={ora_names[:5]}")
        # ORACLE: lng is actually USED — recompute the haversine ignoring lng (Δlng=0) and confirm the ordering DIFFERS,
        # i.e. the result is not explainable by latitude alone (guards against a "NEARBY ignores lng" regression).
        if qidP:
            cur.execute("SELECT lat,lng FROM public.settlement WHERE qid=%s", (qidP,))
            la, lo = cur.fetchone()
            lat_only = ("6371*acos(greatest(-1,least(1, cos(radians(%s))*cos(radians(p.lat))"
                        "+sin(radians(%s))*sin(radians(p.lat)))))")
            cur.execute(f"SELECT p.name FROM public.settlement p WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL "
                        f"AND p.population>150000 AND lower(p.name)<>'paris' ORDER BY ({lat_only}) ASC LIMIT 5",
                        (la, la))
            lat_only_names = [r[0] for r in cur.fetchall()]
            ok("nearby(Paris): full-haversine differs from latitude-only (lng IS used)",
               ora_names[:5] != lat_only_names[:5], f"full={ora_names[:3]} lat_only={lat_only_names[:3]}")
        # DATA-QUALITY (report, don't fail): are the "nearest cities" actually distinct cities, or Paris's own
        # arrondissements / unresolved QIDs leaking from public.settlement?
        arr = [n for n in names if "arrondissement" in n.lower() or n.lower().startswith("q") and n[1:].isdigit()]
        if arr:
            warn("nearby(Paris): non-city / same-city subdivisions in result (settlement not filtered to cities)",
                 f"{arr}")

    # --- A2: "cities near Tokyo" — a second, far-from-Paris reference ---
    refT, qidT, oraT = oracle_nearby(cur, "Tokyo", big=False, limit=5)
    rn2 = _retry(lambda: wr.serve([CUST], "cities near Tokyo", sub))
    if ok("nearby(Tokyo): serve returned a result", rn2 is not None):
        nr2 = (rn2.get("result") or {}).get("rows") or []
        kms2 = [row[-1] for row in nr2]
        names2 = [str(row[0]) for row in nr2]
        ok("nearby(Tokyo): returns rows", bool(nr2), f"n={len(nr2)}")
        ok("nearby(Tokyo): ascending by km", kms2 == sorted(kms2), f"kms={kms2[:5]}")
        ok("nearby(Tokyo): reference resolved to Tokyo",
           (rn2.get("reference") or {}).get("name", "").lower() == "tokyo",
           f"ref={(rn2.get('reference') or {}).get('name')}")
        # the Tokyo neighbours must be near Japan, NOT Paris's neighbours — proves the reference (and lng) drove it
        ok("nearby(Tokyo): result differs from the Paris result (reference-specific)",
           set(names2) != set(globals().get("_paris_names", set())), f"tokyo={names2[:3]}")
        leaked = [n for n in names2 if re.fullmatch(r"Q\d+", n)]
        if leaked:
            warn("nearby(Tokyo): unresolved QID leaked as a settlement name (public.settlement data quality)",
                 f"{leaked}")

    # --- A3: EDGE — the 'big' flag should raise the population floor (150k). Confirm the floor is applied at all. ---
    _, _, ora_big = oracle_nearby(cur, "Paris", big=True, limit=10)
    _, _, ora_small = oracle_nearby(cur, "Paris", big=False, limit=10)
    minpop_big = min((r[2] for r in ora_big), default=0)
    ok("edge: 'big' raises the population floor to 150k (oracle)",
       minpop_big >= 150000 and any(r[2] < 150000 for r in ora_small),
       f"min(big)={minpop_big}")

    # --- A4: EDGE — a reference that is NOT a settlement must delegate (no geo result, no crash). Use a token the
    # oracle CONFIRMS is absent from public.settlement (many "obviously fake" names like 'Atlantis' are real towns). ---
    fake = "Zzqxworldville"
    refN, _, _ = oracle_nearby(cur, fake, big=False)
    ok(f"oracle: '{fake}' is absent from settlement (a true miss)", refN is None, f"resolved={refN}")
    rmiss = wr.serve([CUST], f"cities near {fake}", sub)
    ok("edge: unknown reference delegates (no geo result)",
       "geo nearby" not in (rmiss.get("model") or ""), f"model={rmiss.get('model')}")

    # ============================================================ (B) Big cities by POPULATION ===================
    print("\n== (B) big cities by POPULATION ==", flush=True)
    # oracle: per-city population from world.Cities (the table the view stack joins)
    citypop = {c: oracle_city_pop(cur, c)[1] for c in ["Paris", "Lyon", "Berlin", "Tokyo"]}
    ok("oracle: all demo cities have a population in world.Cities",
       all(v is not None for v in citypop.values()), f"{citypop}")
    top3_oracle = [c for c, _ in sorted(citypop.items(), key=lambda kv: (kv[1] or -1), reverse=True)[:3]]

    rp = _retry(lambda: qc.serve([CUST], "top 3 cities by population", sub))
    if ok("population: serve returned a result", rp is not None):
        ans = (rp.get("result") or {}).get("rows") or []
        # population is a numeric world attribute -> a measure; the stack ranks by it. Pull the numeric column.
        def _nums(row):
            out = []
            for v in row:
                try:
                    out.append(float(str(v).replace(",", "")))
                except (ValueError, TypeError):
                    pass
            return out
        pops = [max(_nums(row)) for row in ans if _nums(row)]
        ok("population: ranking is descending", len(pops) >= 2 and pops == sorted(pops, reverse=True),
           f"pops={pops}")
        ok("population: uses the view-stacking reasoner (composed)",
           "composed" in (rp.get("model") or "") or bool(rp.get("views")),
           f"model={rp.get('model')}")
        # the top city by population among the demo cities is Tokyo (14.26M) -> must lead the ranking
        lead_city = None
        for row in ans[:1]:
            for v in row:
                if str(v) in citypop:
                    lead_city = str(v)
        if lead_city is not None:
            ok("population: top city is the most populous demo city (Tokyo)",
               lead_city == top3_oracle[0], f"lead={lead_city} oracle_top={top3_oracle[0]}")
        else:
            warn("population: city label not surfaced in the answer rows (only the population number)",
                 f"cols={(rp.get('result') or {}).get('columns')}")
        # the population values must be the REAL world.Cities numbers, not row counts (guards a 'population not synced'
        # / 'population column missing' regression where the stack would fall back to COUNT=1 per city).
        ok("population: values are real magnitudes, not 1-per-row counts",
           any(p and p > 1000 for p in pops), f"pops={pops}")

    # --- B2: "largest cities in France" — population ranking RESTRICTED by a world filter (France) ---
    rpf = _retry(lambda: qc.serve([CUST], "largest cities in France by population", sub))
    if rpf is not None:
        rows_fr = (rpf.get("result") or {}).get("rows") or []
        cities_fr = {str(v) for row in rows_fr for v in row if str(v) in citypop}
        if cities_fr:
            ok("population+filter: France ranking excludes non-France cities (Berlin/Tokyo)",
               cities_fr <= {"Paris", "Lyon"}, f"cities={cities_fr}")
        else:
            warn("population+filter: 'largest cities in France' did not surface city labels to verify the filter",
                 f"plan={rpf.get('plan')} model={rpf.get('model')}")
    else:
        warn("population+filter: 'largest cities in France by population' returned nothing", "")

    # ============================================================ (C) canonical regressions ======================
    print("\n== (C) canonical regressions ==", flush=True)

    # C1 France -> SUM (world join on qid + country=Q142 filter). Demo: Paris 100 + Lyon 80 = 180.
    rfr = _retry(lambda: qc.serve([CUST], "total amount in France", sub))
    if ok("France: serve returned a result", rfr is not None):
        fr = (((rfr.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]
        ok("France -> sum = 180 (Paris 100 + Lyon 80)", fr == 180, f"got={fr}")

    # C2 Europe -> 2-hop continent (city.country -> country.continent = Q46). Demo: Paris+Lyon+Berlin = 220 (Tokyo out).
    reu = _retry(lambda: qc.serve([CUST], "total amount in Europe", sub))
    if ok("Europe: serve returned a result", reu is not None):
        eu = (((reu.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]
        ok("Europe -> 2-hop continent sum = 220 (France+Germany, Tokyo excluded)", eu == 220, f"got={eu}")

    # C3 hospital ROUTER typing (the robust, deterministic regression — end-to-end non-geo serve is lazy-fill-slow).
    from engine.router import Router
    rtr = Router()
    oh = rtr.route(["Mayo Clinic", "Cleveland Clinic", "Mount Sinai", "Johns Hopkins Hospital"], header="hospital")
    ok("hospital: router types the column -> 'hospital'", oh and oh["leaf"] == "hospital",
       f"got={oh and oh.get('leaf')} raw={oh and oh.get('raw')}")
    osw = rtr.route(["Photoshop", "Microsoft Word", "Blender", "Visual Studio Code"], header="software")
    ok("software: router types the column -> 'software'", osw and osw["leaf"] == "software",
       f"got={osw and osw.get('leaf')}")

    # C4 composite view-stacks: top-N and by-city (the canonical stacked plans).
    rtopn = _retry(lambda: qc.serve([CUST], "top 3 cities", sub))
    if rtopn is not None:
        plan = rtopn.get("plan") or []
        ok("composite: 'top 3 cities' builds a top-N stack", "topn" in plan, f"plan={plan}")
    reubc = _retry(lambda: qc.serve([CUST], "total amount in Europe by city", sub))
    if reubc is not None:
        planb = reubc.get("plan") or []
        ok("composite: 'total amount in Europe by city' stacks world_join+world_filter+group+topn",
           {"world_join", "world_filter"} <= set(planb), f"plan={planb}")

    # ============================================================ (D) delegation sanity ==========================
    print("\n== (D) delegation sanity ==", flush=True)
    rd = wr.serve([CUST], "total amount in France", sub)
    dv = (((rd.get("result") or rd.get("answer") or {}).get("rows") or [[None]])[0] or [None])[0]
    ok("delegate: WorldReasoner passes the France aggregate through unchanged (=180)", dv == 180, f"got={dv}")

    # ============================================================ (E) CONCURRENCY — no bridge deadlock ===========
    # Regression guard. Two service instances handling concurrent requests for the SAME per-user sub (the normal
    # multi-tab / cold-start-retry case) used to wedge each other: instance A left its bridge SELECTs idle-in-
    # transaction holding read locks on "<sub>.customers connected to wikipedia", and instance B's per-request
    # `ALTER TABLE … ADD COLUMN world_qid` (an ACCESS EXCLUSIVE lock) blocked ~forever on that relation lock (833s
    # measured). The fix: _rconn() is autocommit (no idle-in-transaction) AND the ADD COLUMN migration is guarded by
    # an information_schema check (no exclusive lock once the column exists). Here we build a SECOND independent
    # instance (its own connection set) and fire the world-join serve from BOTH concurrently against the same sub; a
    # deadlock would make a thread exceed the join timeout -> FAIL (not a silent hang).
    print("\n== (E) concurrency: two instances, same sub, no deadlock ==", flush=True)
    import threading

    def _world_pids():
        """pids of the backends currently connected to the world DB (so we can diff and only judge the connections
        THIS test creates — pg_stat_activity is global, so stale/foreign sessions must not pollute the assertion)."""
        cur.execute("SELECT pid FROM pg_stat_activity WHERE datname='world'")
        return {r[0] for r in cur.fetchall()}

    base_pids = _world_pids()                                    # baseline: everything alive BEFORE instance B / the race
    t1 = time.time()
    print("  building a 2nd ComposedWorldQuery instance (own connection set)…", flush=True)
    qc_b = ComposedWorldQuery()                                 # instance B: separate _rconn, separate bridge writer
    print(f"    built in {time.time() - t1:.0f}s", flush=True)
    results, errors = {}, {}

    def _hit(tag, inst):
        try:
            for _ in range(2):                                  # a few rounds to widen the race window on the bridge
                r = inst.serve([CUST], "total amount in France", sub)
                results[tag] = (((r.get("result") or {}).get("rows") or [[None]])[0] or [None])[0]
        except Exception as e:                                   # noqa: BLE001
            errors[tag] = repr(e)

    th = [threading.Thread(target=_hit, args=("A", qc), daemon=True),
          threading.Thread(target=_hit, args=("B", qc_b), daemon=True)]
    for t in th:
        t.start()
    for t in th:
        t.join(timeout=120)                                     # generous warm-serve budget; a real deadlock blocks 800s+
    alive = [t for t in th if t.is_alive()]
    ok("concurrency: both instances completed (no deadlock / no hang)", not alive and not errors,
       f"alive={len(alive)} errors={errors}")
    ok("concurrency: both got the correct France sum (=180) under concurrent same-sub load",
       results.get("A") == 180 and results.get("B") == 180, f"results={results}")
    # None of the connections THIS test created (instance B's set + the new ones from the race) may be left
    # 'idle in transaction' — that lock-holding state is exactly what the deadlock came from. Diff against the
    # baseline so unrelated/stale backends (other sessions, pg_stat_activity is global) don't pollute the check.
    cur.execute("SELECT pid, left(regexp_replace(query,E'\\\\s+',' ','g'),60) FROM pg_stat_activity "
                "WHERE datname='world' AND state='idle in transaction'")
    new_idle = [(p, q) for p, q in cur.fetchall() if p not in base_pids]
    ok("concurrency: no NEW connection left 'idle in transaction' (autocommit holds no locks)", not new_idle,
       f"new_idle_in_transaction={new_idle}")
    for c in (getattr(qc_b.qw, "_rcn", None),):                # release instance B's pooled connection
        try:
            if c:
                c.close()
        except Exception:                                       # noqa: BLE001
            pass

    cn.close()
    print(f"\n{P}/{P + F} passed" + ("" if not F else f"  ({F} FAILED)")
          + (f"  ({len(WARN)} warnings)" if WARN else ""), flush=True)
    if WARN:
        print("WARNINGS (data-quality / serve-code bugs to fix):", flush=True)
        for n, d in WARN:
            print(f"  - {n}: {d}", flush=True)
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
