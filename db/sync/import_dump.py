r"""Bulk world import from the philippesaade/wikidata Hugging Face dataset (546 parquet
chunks, ~95GB, columns id/labels/descriptions/aliases/sitelinks/claims as JSON strings).
Loads the geo hierarchy (continent > country > admin > settlement) + currency/element/
timezone into public.* of the world database.

LEGACY / ALTERNATIVE PATH: the dump turned out to be a ~1M-entity subset missing many
major cities, so `sync_wikidata.py` (live WDQS) is the recommended bulk sync. This
importer is kept because it is fully self-contained (no WDQS dependency for routing —
P31 class labels are inline) and resumable.

The dataset uses a SIMPLIFIED Wikibase JSON: claims[Pxx] = [{"mainsnak":{"datavalue": V, "datatype": ...}}]
  - entity ref:  V = {"id":"Q..","labels":{lang:{"value":..}}}      -> id + inline en label (denormalize!)
  - quantity:    V = {"amount":"+717710","unit":"1"}                 -> amount
  - coordinate:  V = {"latitude":..,"longitude":..}
  - string/id:   V = "Pt" / "SBD"                                    -> the string itself

Streams each chunk (download, parse, delete), upserts per chunk, checkpoints per chunk
in public.import_ckpt (resumable). Chunks are cached under $CHUNK_DIR (default: a
temp dir); only one chunk lives on disk at a time.

Run (after `psql -f db/init.sql`):
  export WORLD_PG_HOST=... WORLD_PG_PASSWORD=...        # see db/sync/_conn.py
  python db/sync/import_dump.py [--max-chunks N] [--start I] [--pop-min P]
"""
from __future__ import annotations
import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from psycopg2.extras import execute_values

try:
    from _conn import connect                     # run as a script from db/sync
except ImportError:
    from ._conn import connect                    # imported as a package module

REPO = "philippesaade/wikidata"
N_CHUNKS = 546
INIT_SQL = Path(__file__).resolve().parent.parent / "init.sql"

# --- P31 routing -------------------------------------------------------------------------------------------
QID = {  # precise small classes: route by the instance-of QID
    "element": {"Q11344"}, "currency": {"Q8142"}, "timezone": {"Q12143", "Q17272692"},
    "continent": {"Q5107"}, "country": {"Q6256", "Q3624078", "Q1520223", "Q3024240"},
}
# settlements / admin have hundreds of subclasses -> match the inline P31 class label (cheap, robust)
SETTLE_KW = ("city", "town", "village", "municipality", "settlement", "capital", "commune", "borough",
             "metropolis", "locality", "hamlet", "urban area")
ADMIN_KW = ("province", "state of", "region", "district", "county", "prefecture", "department", "oblast",
            "canton", "territory", "governorate", "voivodeship", "federal subject", "autonomous", "administrative")
# QID backstop for the common roots (catches entities whose P31 class label lacks an 'en' inline label)
SETTLE_QID = {"Q486972", "Q515", "Q3957", "Q532", "Q1549591", "Q5119", "Q15284", "Q1093829", "Q3327873", "Q702492",
              # settlement classes major cities are actually typed as (megacity / largest city / metropolis /
              # global city / national capital / port city / national central city / direct-admin municipality /
              # state capital) — the dump types Mumbai/Tokyo/Beijing as these, NOT plain "city"
              "Q174844", "Q51929311", "Q200250", "Q208511", "Q108178728", "Q2264924", "Q1066538", "Q1208802", "Q11271835"}
ADMIN_QID = {"Q56061", "Q10864048", "Q13220204", "Q35657", "Q7275", "Q28575", "Q34876", "Q1799794"}
# the FULL P279* subclass-closure of human-settlement / admin-territorial-entity, fetched from WDQS at startup
SETTLE_CLOSURE = set(SETTLE_QID)
ADMIN_CLOSURE = set(ADMIN_QID)


def wdqs_closure(root):
    import urllib.request, urllib.parse
    q = f"SELECT ?c WHERE {{ ?c wdt:P279* wd:{root} }}"
    url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={"User-Agent": "prereasoner-db-sync/1.0",
                                               "Accept": "application/sparql-results+json"})
    d = json.load(urllib.request.urlopen(req, timeout=120))
    return {b["c"]["value"].rsplit("/", 1)[-1] for b in d["results"]["bindings"]
            if b["c"]["value"].rsplit("/", 1)[-1].startswith("Q")}


# clean ISO-3166 alpha-2 -> continent (Wikidata P30 is inconsistent: 'Eurasia'/'Americas'/'Insular Oceania'/missing)
_CONTINENT = {}
for _cont, _codes in {
    "Africa": "DZ AO BJ BW BF BI CM CV CF TD KM CG CD CI DJ EG GQ ER ET GA GM GH GN GW KE LS LR LY MG MW ML MR MU YT MA MZ NA NE NG RE RW SH ST SN SC SL SO ZA SS SD SZ TZ TG TN UG EH ZM ZW",
    "Asia": "AF AM AZ BH BD BT BN KH CN CY GE HK IN ID IR IQ IL JP JO KZ KP KR KW KG LA LB MO MY MV MN MM NP OM PK PH QA SA SG LK SY TW TJ TH TL TR TM AE UZ VN YE PS",
    "Europe": "AL AD AT BY BE BA BG HR CZ DK EE FO FI FR DE GI GR GG HU IS IE IM IT JE XK LV LI LT LU MT MD MC ME NL MK NO PL PT RO RU SM RS SK SI ES SE CH UA GB VA",
    "North America": "AG BS BB BZ BM CA CR CU DM DO SV GL GD GP GT HT HN JM MQ MX NI PA PR KN LC PM VC TT US AI AW VG KY CW SX BQ",
    "South America": "AR BO BR CL CO EC FK GF GY PE PY SR UY VE",
    "Oceania": "AS AU CK FJ PF GU KI MH FM NR NC NZ NU NF MP PW PG PN WS SB TK TO TV VU WF",
    "Antarctica": "AQ",
}.items():
    for _c in _codes.split():
        _CONTINENT[_c] = _cont

# --- claims accessors (simplified Wikibase JSON of this dataset) --------------------------------------------
def _dv(st):
    return (st or {}).get("mainsnak", {}).get("datavalue")

def first(cl, p):
    """the representative statement for property p: prefer rank 'preferred', skip 'deprecated' — so a country's
    CURRENT currency (Euro, preferred) wins over a former one (Deutsche Mark, normal/deprecated)."""
    sts = [s for s in (cl.get(p) or []) if isinstance(s, dict) and s.get("rank") != "deprecated"]
    if not sts:
        return None
    pref = [s for s in sts if s.get("rank") == "preferred"]
    return (pref or sts)[0]

def ref_id(st):
    dv = _dv(st)
    return dv.get("id") if isinstance(dv, dict) else None

def ref_label(st):
    dv = _dv(st)
    if isinstance(dv, dict):
        labs = dv.get("labels")
        en = labs.get("en") if isinstance(labs, dict) else None
        if isinstance(en, dict):        # top-level entity-label format {"value": ".."}
            return en.get("value")
        if isinstance(en, str):         # INLINE ref-label format (P31/P17/P30 datavalue): flat string
            return en
    return None

def amount(st):
    dv = _dv(st)
    if isinstance(dv, dict) and dv.get("amount") is not None:
        try:
            return float(str(dv["amount"]).replace("+", ""))
        except ValueError:
            return None
    return None

def coord(st):
    dv = _dv(st)
    if isinstance(dv, dict):
        return dv.get("latitude"), dv.get("longitude")
    return None, None

def strval(st):
    dv = _dv(st)
    return dv if isinstance(dv, str) else None

def en_label(labels_json):
    try:
        d = json.loads(labels_json)
    except Exception:
        return None, None
    en = d.get("en") or d.get("en-gb") or d.get("mul")
    name = en.get("value") if isinstance(en, dict) else None
    if not name and d:                       # fall back to any language
        any_v = next(iter(d.values()))
        name = any_v.get("value") if isinstance(any_v, dict) else None
    return name, d

def en_aliases(aliases_json):
    try:
        d = json.loads(aliases_json)
    except Exception:
        return []
    return [a.get("value") for a in d.get("en", []) if isinstance(a, dict) and a.get("value")]

_PF = None
def prefilter_hit(cs):
    """cheap substring gate: skip json.loads for entities with no target-class signal at all (most of the dump)."""
    global _PF
    if _PF is None:
        kw = ("city", "town", "village", "municipal", "settlement", "capital", "commune", "borough", "metropolis",
              "hamlet", "province", "region", "district", "county", "prefecture", "department", "oblast", "canton",
              "governorate", "voivod", "continent", "country", "sovereign state", "currency", "chemical element",
              "time zone")
        qids = QID["element"] | QID["currency"] | QID["timezone"] | QID["continent"] | QID["country"] | SETTLE_QID | ADMIN_QID
        _PF = tuple('"' + q + '"' for q in qids) + kw
    return any(s in cs for s in _PF)


def route(cl):
    """return table name for this entity from its P31 statements, or None."""
    ids, labs = set(), []
    for st in cl.get("P31", []):
        i = ref_id(st)
        if i:
            ids.add(i)
        l = ref_label(st)
        if l:
            labs.append(l.lower())
    for tbl in ("element", "currency", "timezone", "continent", "country"):
        if ids & QID[tbl]:
            return tbl
    if (ids & SETTLE_CLOSURE) or any(any(k in l for k in SETTLE_KW) for l in labs):
        return "settlement"
    if (ids & ADMIN_CLOSURE) or any(any(k in l for k in ADMIN_KW) for l in labs):
        return "admin"
    return None

# --- per-table row extraction ------------------------------------------------------------------------------
def extract(tbl, qid, name, cl):
    if tbl == "continent":
        return {"qid": qid, "name": name}
    if tbl == "country":
        cont = first(cl, "P30")
        cur = first(cl, "P38")
        cap = first(cl, "P36")
        iso2 = strval(first(cl, "P297"))
        return {"qid": qid, "name": name, "iso2": iso2, "iso3": strval(first(cl, "P299")),
                "continent_qid": ref_id(cont), "continent": _CONTINENT.get(iso2) or ref_label(cont),
                "capital_qid": ref_id(cap),
                "currency_code": strval(first(cl, "P498")) or ref_id(cur), "currency_name": ref_label(cur),
                "population": _i(amount(first(cl, "P1082"))), "area_km2": amount(first(cl, "P2046")),
                "official_language": ref_label(first(cl, "P37"))}
    if tbl == "admin":
        c = first(cl, "P17"); par = first(cl, "P131")
        lvl = next((l.lower() for st in cl.get("P31", []) for l in [ref_label(st)] if l), None)
        return {"qid": qid, "name": name, "country_qid": ref_id(c), "country": ref_label(c),
                "parent_qid": ref_id(par), "level": lvl, "population": _i(amount(first(cl, "P1082"))),
                "capital_qid": ref_id(first(cl, "P36"))}
    if tbl == "settlement":
        c = first(cl, "P17"); adm = first(cl, "P131"); lat, lng = coord(first(cl, "P625"))
        iscap = bool(cl.get("P1376"))        # P1376 = capital of
        return {"qid": qid, "name": name, "country_qid": ref_id(c), "country": ref_label(c),
                "admin_qid": ref_id(adm), "admin": ref_label(adm), "population": _i(amount(first(cl, "P1082"))),
                "lat": lat, "lng": lng, "timezone": ref_label(first(cl, "P421")), "is_capital": iscap}
    if tbl == "currency":
        return {"code": strval(first(cl, "P498")) or qid, "qid": qid, "name": name,
                "symbol": strval(first(cl, "P5061")) or strval(first(cl, "P489"))}
    if tbl == "element":
        return {"symbol": strval(first(cl, "P246")) or qid, "qid": qid, "name": name,
                "atomic_number": _i(amount(first(cl, "P1086"))), "mass": amount(first(cl, "P2067"))}
    if tbl == "timezone":
        return {"qid": qid, "name": name, "utc_offset": strval(first(cl, "P2907"))}
    return None

def _i(x):
    return int(x) if x is not None else None

# --- DB ----------------------------------------------------------------------------------------------------
PK = {"continent": "qid", "country": "qid", "admin": "qid", "settlement": "qid",
      "currency": "code", "element": "symbol", "timezone": "qid"}

def ensure_schema(conn):
    """apply db/init.sql (idempotent) so the target tables exist."""
    if INIT_SQL.exists():
        conn.cursor().execute(INIT_SQL.read_text(encoding="utf-8")); conn.commit(); return
    raise FileNotFoundError(f"init.sql not found at {INIT_SQL}")

def ckpt_done(conn):                              # checkpoint lives IN Postgres -> resumable + shared local/cloud
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS import_ckpt (chunk int PRIMARY KEY)"); conn.commit()
    cur.execute("SELECT chunk FROM import_ckpt"); return {r[0] for r in cur.fetchall()}

def ckpt_mark(conn, ci):
    conn.cursor().execute("INSERT INTO import_ckpt(chunk) VALUES (%s) ON CONFLICT DO NOTHING", (ci,)); conn.commit()

def upsert(conn, tbl, rows):
    if not rows:
        return
    cols = list(rows[0].keys())
    pk = PK[tbl]
    setc = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != pk)
    sql = (f'INSERT INTO {tbl} ({",".join(cols)}) VALUES %s '
           f'ON CONFLICT ({pk}) DO UPDATE SET {setc}' if setc else
           f'INSERT INTO {tbl} ({",".join(cols)}) VALUES %s ON CONFLICT ({pk}) DO NOTHING')
    execute_values(conn.cursor(), sql, [[r.get(c) for c in cols] for r in rows], page_size=500)

def upsert_labels(conn, rows):
    if rows:
        execute_values(conn.cursor(),
                       "INSERT INTO entity_label (qid,label,lang,is_alias,kind) VALUES %s", rows, page_size=1000)

# --- main --------------------------------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-chunks", type=int, default=N_CHUNKS)
    ap.add_argument("--start", type=int, default=None, help="override resume index")
    ap.add_argument("--pop-min", type=int, default=0, help="skip settlements below this population")
    ap.add_argument("--reset", action="store_true", help="ignore checkpoint, start at 0")
    args = ap.parse_args()

    chunk_dir = os.environ.get("CHUNK_DIR", os.path.join(tempfile.gettempdir(), "wd"))
    global SETTLE_CLOSURE, ADMIN_CLOSURE
    try:                                          # the proper routing: P279* subclass closures from WDQS
        SETTLE_CLOSURE = (wdqs_closure("Q486972") | wdqs_closure("Q515") | SETTLE_QID)
        ADMIN_CLOSURE = (wdqs_closure("Q56061") | ADMIN_QID)
        print(f"WDQS closures: settlement={len(SETTLE_CLOSURE)} admin={len(ADMIN_CLOSURE)}", flush=True)
    except Exception as e:
        print(f"WDQS closure fetch failed ({e}) -> keyword/QID fallback", flush=True)
    conn = connect(); ensure_schema(conn)
    if args.reset:
        cur = conn.cursor()
        for t in list(PK) + ["entity_label"]:
            cur.execute(f"TRUNCATE {t}")
        cur.execute("DELETE FROM import_ckpt"); conn.commit()
        print("reset: truncated world tables + checkpoint", flush=True)
    done = ckpt_done(conn)
    start = args.start if args.start is not None else 0
    totals = {t: 0 for t in PK}
    t0 = time.time()
    for ci in range(start, min(args.max_chunks, N_CHUNKS)):
        if ci in done:
            continue
        fn = f"data/chunk_0-{ci:05d}-of-{N_CHUNKS:05d}.parquet"
        lp = None
        try:                                       # whole-file download (1 request) >> streaming
            from huggingface_hub import hf_hub_download   # lazy: only the dump path needs these deps
            import pyarrow.parquet as pq
            lp = hf_hub_download(REPO, fn, repo_type="dataset", local_dir=chunk_dir)
            pf = pq.ParquetFile(lp)
        except Exception as e:
            print(f"[chunk {ci}] download failed: {e}", flush=True)
            if lp and os.path.exists(lp):
                os.remove(lp)
            continue
        buf = {t: [] for t in PK}; labels = []
        for rg in range(pf.num_row_groups):
            tb = pf.read_row_group(rg, columns=["id", "claims", "labels", "aliases"])
            ids = tb.column("id").to_pylist(); CL = tb.column("claims").to_pylist()
            LB = tb.column("labels").to_pylist(); AL = tb.column("aliases").to_pylist()
            for qid, cs, lb, al in zip(ids, CL, LB, AL):
                if not qid or not qid.startswith("Q") or not prefilter_hit(cs):
                    continue
                try:                                       # one malformed entity must never kill the overnight run
                    cl = json.loads(cs)
                    tbl = route(cl)
                    if not tbl:
                        continue
                    name, _ = en_label(lb)
                    if not name:
                        continue
                    if tbl == "settlement" and args.pop_min:
                        p = amount(first(cl, "P1082"))
                        if p is None or p < args.pop_min:
                            continue
                    row = extract(tbl, qid, name, cl)
                    if not row or not row.get(PK[tbl]):
                        continue
                    buf[tbl].append(row)
                    labels.append((qid, name, "en", False, tbl))
                    for a in en_aliases(al)[:8]:
                        labels.append((qid, a, "en", True, tbl))
                except Exception:
                    continue
        for t in PK:
            upsert(conn, t, buf[t]); totals[t] += len(buf[t])
        upsert_labels(conn, labels)
        conn.commit()
        ckpt_mark(conn, ci)
        if lp and os.path.exists(lp):              # delete the chunk file -> disk stays ~1 chunk, not 95GB
            os.remove(lp)
        got = ", ".join(f"{t}:{len(buf[t])}" for t in PK if buf[t])
        print(f"[chunk {ci+1}/{args.max_chunks}] {got or 'none'}  | totals {totals}  | {time.time()-t0:.0f}s", flush=True)
    conn.close()
    print(f"DONE. totals={totals}")

if __name__ == "__main__":
    main()
