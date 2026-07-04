"""Bulk world sync from LIVE Wikidata (WDQS SPARQL) into public.* — the RECOMMENDED
bulk path (the parquet dump behind import_dump.py is a subset missing major cities).

Fills the same public tables init.sql creates (settlement / country / admin /
continent / currency / element / timezone / entity_label), so the downstream
build_world.py + build_words.py transforms work unchanged.

WDQS returns small JSON (KB-MB), so this runs anywhere with network access.
Settlements are pulled in global population bands (descending); a band that is
too heavy for WDQS's 60s timeout is bisected by population; the low-population
long tail is pulled per-country (the P17 index keeps each query bounded).

Run (after `psql -f db/init.sql`):
  export WORLD_PG_HOST=... WORLD_PG_PASSWORD=...        # see db/sync/_conn.py
  python db/sync/sync_wikidata.py --reset               # full (pop>=1000), hours
  python db/sync/sync_wikidata.py --reset --high-only   # fast seed (pop>=100000), ~minutes
"""
from __future__ import annotations
import argparse
import json
import time
import urllib.parse
import urllib.request

try:
    from import_dump import connect, upsert, ensure_schema, _CONTINENT   # run as a script from db/sync
except ImportError:
    from .import_dump import connect, upsert, ensure_schema, _CONTINENT  # imported as a package module

UA = "prereasoner-db-sync/1.0 (https://github.com/manidoraisamy/prereasoner; mani.doraisamy@gmail.com)"
ENDPOINT = "https://query.wikidata.org/sparql"


def wdqs(query, timeout=58, retries=4):
    """POST a SPARQL query, return list of binding dicts. Retry (with backoff) on 429/5xx/network."""
    data = urllib.parse.urlencode({"query": query, "format": "json"}).encode()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(ENDPOINT, data=data, headers={
                "User-Agent": UA, "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)["results"]["bindings"]
        except Exception as e:                       # noqa: BLE001
            last = e
            if attempt == retries - 1:
                break
            wait = min(45, 5 * (attempt + 1) ** 2)
            print(f"    WDQS retry {attempt+1}/{retries} ({getattr(e,'code',None) or type(e).__name__}) wait {wait}s", flush=True)
            time.sleep(wait)
    raise last


def V(b, k):
    return b[k]["value"] if k in b else None


def qid_of(uri):
    return uri.rsplit("/", 1)[-1] if uri else None


def parse_point(wkt):
    """'Point(lng lat)' -> (lat, lng)."""
    if not wkt or not wkt.startswith("Point("):
        return (None, None)
    try:
        lng, lat = wkt[6:-1].split()
        return (float(lat), float(lng))
    except Exception:                                 # noqa: BLE001
        return (None, None)


# Wikidata's official English label -> the colloquial form people type in a CSV / question ("in China").
# Applied to country.name AND settlement.country (denormalized) so name-based filters match.
NAME_FIX = {
    "People's Republic of China": "China",
    "United States of America": "United States",
    "Kingdom of the Netherlands": "Netherlands",
    "Czech Republic": "Czechia",
    "Republic of the Congo": "Congo",
    "Democratic Republic of the Congo": "DR Congo",
}


def fix_name(n):
    return NAME_FIX.get(n, n) if n else n


# --- queries -----------------------------------------------------------------------------------------------
COUNTRY_Q = """SELECT ?c ?cLabel ?iso2 ?iso3 ?cont ?contLabel ?cap ?cur ?curCode ?curLabel ?pop ?area ?langLabel WHERE {
  { ?c wdt:P31 wd:Q3624078 } UNION { ?c wdt:P31 wd:Q6256 }
  FILTER NOT EXISTS { ?c wdt:P576 ?dis }
  OPTIONAL { ?c wdt:P297 ?iso2 }
  OPTIONAL { ?c wdt:P298 ?iso3 }
  OPTIONAL { ?c wdt:P30 ?cont }
  OPTIONAL { ?c wdt:P36 ?cap }
  OPTIONAL { ?c wdt:P38 ?cur . OPTIONAL { ?cur wdt:P498 ?curCode } }
  OPTIONAL { ?c wdt:P1082 ?pop }
  OPTIONAL { ?c wdt:P2046 ?area }
  OPTIONAL { ?c wdt:P37 ?lang }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""

CURRENCY_Q = """SELECT ?cur ?curLabel ?code ?symbol WHERE {
  ?country wdt:P31 wd:Q3624078 ; wdt:P38 ?cur .
  OPTIONAL { ?cur wdt:P498 ?code }
  OPTIONAL { ?cur wdt:P5061 ?symbol }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""

ELEMENT_Q = """SELECT ?e ?eLabel ?sym ?num ?mass WHERE {
  ?e wdt:P31 wd:Q11344 .
  OPTIONAL { ?e wdt:P246 ?sym }
  OPTIONAL { ?e wdt:P1086 ?num }
  OPTIONAL { ?e wdt:P2067 ?mass }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""

SETTLE_TMPL = """SELECT ?s ?sLabel ?pop ?coord ?country ?countryLabel ?admin ?adminLabel WHERE {
  ?s wdt:P31/wdt:P279* wd:Q486972 ;
     wdt:P1082 ?pop ;
     wdt:P17 ?country .
  FILTER(?pop >= __LO__ && ?pop < __HI__)
  OPTIONAL { ?s wdt:P625 ?coord }
  OPTIONAL { ?s wdt:P131 ?admin }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""

# Per-COUNTRY variant: binding P17 to one country lets WDQS use the country index, so the query is bounded
# and fast even at low populations (the global P279* path times out below ~50k). Used for the long tail.
SETTLE_C_TMPL = """SELECT ?s ?sLabel ?pop ?coord ?admin ?adminLabel WHERE {
  ?s wdt:P31/wdt:P279* wd:Q486972 ;
     wdt:P17 wd:__CQ__ ;
     wdt:P1082 ?pop .
  FILTER(?pop >= __LO__ && ?pop < __HI__)
  OPTIONAL { ?s wdt:P625 ?coord }
  OPTIONAL { ?s wdt:P131 ?admin }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""


# --- fetch + normalize (uniform dict keys: upsert reads columns from rows[0]) -------------------------------
def fetch_countries():
    by = {}
    for b in wdqs(COUNTRY_Q, retries=5):
        cq = qid_of(V(b, "c"))
        if not cq:
            continue
        r = by.setdefault(cq, {"qid": cq, "name": None, "iso2": None, "iso3": None, "continent_qid": None,
                               "continent": None, "capital_qid": None, "currency_code": None,
                               "currency_name": None, "population": None, "area_km2": None,
                               "official_language": None, "_cont": None})
        def setn(k, val):
            if val is not None and not r.get(k):
                r[k] = val
        setn("name", V(b, "cLabel"))
        setn("iso2", V(b, "iso2")); setn("iso3", V(b, "iso3"))
        setn("continent_qid", qid_of(V(b, "cont"))); setn("_cont", V(b, "contLabel"))
        setn("capital_qid", qid_of(V(b, "cap")))
        setn("currency_code", V(b, "curCode") or qid_of(V(b, "cur")))
        setn("currency_name", V(b, "curLabel"))
        setn("official_language", V(b, "langLabel"))
        if V(b, "pop") and not r["population"]:
            try: r["population"] = int(float(V(b, "pop")))
            except Exception: pass                                          # noqa: BLE001
        if V(b, "area") and not r["area_km2"]:
            try: r["area_km2"] = float(V(b, "area"))
            except Exception: pass                                          # noqa: BLE001
    out = []
    for r in by.values():
        if not r["name"] or r["name"] == r["qid"]:                          # skip label-less stubs
            continue
        r["continent"] = _CONTINENT.get(r.get("iso2")) or r.pop("_cont", None)
        r.pop("_cont", None)
        r["name"] = fix_name(r["name"])
        out.append(r)
    return out


def fetch_currencies():
    by = {}
    for b in wdqs(CURRENCY_Q, retries=5):
        cq = qid_of(V(b, "cur"))
        code = V(b, "code") or cq
        if not code or not V(b, "curLabel"):
            continue
        by.setdefault(code, {"code": code, "qid": cq, "name": V(b, "curLabel"), "symbol": V(b, "symbol")})
    return list(by.values())


def fetch_elements():
    by = {}
    for b in wdqs(ELEMENT_Q, retries=5):
        sym = V(b, "sym")
        if not sym or not V(b, "eLabel"):
            continue
        r = by.setdefault(sym, {"symbol": sym, "qid": qid_of(V(b, "e")), "name": V(b, "eLabel"),
                                "atomic_number": None, "mass": None})
        if V(b, "num") and r["atomic_number"] is None:
            try: r["atomic_number"] = int(float(V(b, "num")))
            except Exception: pass                                          # noqa: BLE001
        if V(b, "mass") and r["mass"] is None:
            try: r["mass"] = float(V(b, "mass"))
            except Exception: pass                                          # noqa: BLE001
    return list(by.values())


def settlements_from(bindings, capitals, cq=None, cname=None):
    by = {}
    for b in bindings:
        sq = qid_of(V(b, "s"))
        if not sq:
            continue
        if sq not in by:
            lat, lng = parse_point(V(b, "coord")); pop = V(b, "pop")
            by[sq] = {"qid": sq, "name": (V(b, "sLabel") or sq),
                      "country_qid": (cq or qid_of(V(b, "country"))),
                      "country": (cname or fix_name(V(b, "countryLabel"))), "admin_qid": qid_of(V(b, "admin")),
                      "admin": V(b, "adminLabel"),
                      "population": (int(float(pop)) if pop else None), "lat": lat, "lng": lng,
                      "timezone": None, "is_capital": (sq in capitals)}
        else:
            r = by[sq]
            if r["lat"] is None:
                r["lat"], r["lng"] = parse_point(V(b, "coord"))
            if not r["admin_qid"] and V(b, "admin"):
                r["admin_qid"] = qid_of(V(b, "admin")); r["admin"] = V(b, "adminLabel")
    return list(by.values())


def fetch_band(lo, hi, depth=0):
    """One population band; if too heavy for WDQS, bisect by population and recurse."""
    try:
        return wdqs(SETTLE_TMPL.replace("__LO__", str(lo)).replace("__HI__", str(hi)), timeout=55, retries=2)
    except Exception as e:                                                  # noqa: BLE001
        if hi - lo <= 1 or depth >= 30:
            print(f"    [{lo},{hi}) FAILED (depth {depth}): {e}", flush=True)
            return []
        mid = (lo + hi) // 2
        print(f"    [{lo},{hi}) heavy -> bisect at {mid}", flush=True)
        return fetch_band(lo, mid, depth + 1) + fetch_band(mid, hi, depth + 1)


def fetch_country_band(cq, lo, hi, depth=0):
    """Settlements of ONE country in a population band (country-indexed -> fast); bisect on timeout."""
    try:
        q = SETTLE_C_TMPL.replace("__CQ__", cq).replace("__LO__", str(lo)).replace("__HI__", str(hi))
        return wdqs(q, timeout=55, retries=2)
    except Exception as e:                                                  # noqa: BLE001
        if hi - lo <= 1 or depth >= 26:
            print(f"      {cq} [{lo},{hi}) FAILED: {e}", flush=True)
            return []
        mid = (lo + hi) // 2
        return fetch_country_band(cq, lo, mid, depth + 1) + fetch_country_band(cq, mid, hi, depth + 1)


LABELS_SQL = """INSERT INTO entity_label(qid,label,lang,is_alias,kind)
SELECT qid,name,'en',false,'settlement' FROM settlement WHERE name IS NOT NULL
UNION ALL SELECT qid,name,'en',false,'country' FROM country WHERE name IS NOT NULL
UNION ALL SELECT qid,name,'en',false,'continent' FROM continent
UNION ALL SELECT COALESCE(qid,code),name,'en',false,'currency' FROM currency WHERE name IS NOT NULL
UNION ALL SELECT COALESCE(qid,symbol),name,'en',false,'element' FROM element WHERE name IS NOT NULL"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="TRUNCATE world tables first (clean build)")
    ap.add_argument("--min-pop", type=int, default=1000, help="lowest settlement population to import")
    ap.add_argument("--high-only", action="store_true", help="only pop>=100000 (fast seed)")
    a = ap.parse_args()
    min_pop = 100000 if a.high_only else a.min_pop

    cn = connect(); cur = cn.cursor()
    ensure_schema(cn)
    if a.reset:
        cur.execute("TRUNCATE settlement,country,admin,continent,currency,element,timezone,entity_label")
        cn.commit(); print("reset: truncated world tables", flush=True)

    conts = {"Q15": "Africa", "Q48": "Asia", "Q46": "Europe", "Q49": "North America",
             "Q18": "South America", "Q538": "Oceania", "Q51": "Antarctica"}
    upsert(cn, "continent", [{"qid": q, "name": n} for q, n in conts.items()]); cn.commit()

    print("countries...", flush=True)
    crows = fetch_countries(); upsert(cn, "country", crows); cn.commit()
    capitals = {r["capital_qid"] for r in crows if r.get("capital_qid")}
    print(f"  countries {len(crows)} ({sum(1 for r in crows if r.get('continent'))} with continent)", flush=True)

    print("currencies...", flush=True)
    cu = fetch_currencies(); upsert(cn, "currency", cu); cn.commit(); print(f"  currencies {len(cu)}", flush=True)
    print("elements...", flush=True)
    el = fetch_elements(); upsert(cn, "element", el); cn.commit(); print(f"  elements {len(el)}", flush=True)

    # Big cities (pop>=50000): GLOBAL bands — small result sets, fast, and catches cities in dependent
    # territories not in the sovereign-country list. Below 50k the global P279* path times out, so the
    # long tail is pulled per-country (the P17 index makes each query bounded + fast).
    HI_FLOOR = 50000
    total = 0
    print("settlements (global, pop>=50000)...", flush=True)
    for lo, hi in [(100000, 10 ** 9), (50000, 100000)]:
        if hi <= min_pop:
            continue
        lo = max(lo, min_pop)
        srows = settlements_from(fetch_band(lo, hi), capitals)
        upsert(cn, "settlement", srows); cn.commit(); total += len(srows)
        print(f"  band [{lo},{hi}): +{len(srows)}  (total {total})", flush=True)
        time.sleep(1)
    if min_pop < HI_FLOOR:
        print(f"settlements (per-country, pop {min_pop}-{HI_FLOOR})...", flush=True)
        for i, r in enumerate(crows):
            cq, cname = r["qid"], r["name"]
            srows = settlements_from(fetch_country_band(cq, min_pop, HI_FLOOR), capitals, cq, cname)
            if srows:
                upsert(cn, "settlement", srows); cn.commit(); total += len(srows)
                print(f"  [{i+1}/{len(crows)}] {cname}: +{len(srows)}  (total {total})", flush=True)

    print("entity_label...", flush=True)
    cur.execute(LABELS_SQL); cn.commit()
    cur.execute("SELECT count(*) FROM settlement"); ns = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM country"); nc = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM entity_label"); nl = cur.fetchone()[0]
    print(f"DONE: {ns} settlements / {nc} countries / {nl} labels", flush=True)
    cn.close()


if __name__ == "__main__":
    main()
