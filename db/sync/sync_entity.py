"""Faithful per-TYPE Wikidata sync + the LAZY single-entity sync the engine uses at
query time (standalone copy of the engine's sync_wikidata_world module).

For a taxonomy type (qid), this:
  1. DISCOVERS the type's real schema from Wikidata (property-frequency over a sample of
     its instances), keeping real-ATTRIBUTE properties (WikibaseItem / Quantity / Time /
     String / Monolingualtext / GlobeCoordinate) and dropping Wikimedia cruft.
  2. CREATES the faithful table with one TEXT column per kept property (snake_cased
     label) + qid + name; item-valued properties store the related entity's QID (FK).
  3. POPULATES it from WDQS — bulk (--max N), schema-only (--schema-only), or lazily one
     entity at a time (--lazy VALUE, or ensure_entity()/lazy_resolve() when imported).

The lazy path is what a fresh deployment relies on: knowledgebase."<exact Wikidata label>"
tables start EMPTY (or nonexistent — ensure_table creates them on demand) and fill as
CSV cells resolve. Each lazily-synced entity is also registered in knowledgebase."words" so it
resolves instantly next time.

Run:
  export KB_PG_HOST=... KB_PG_PASSWORD=...        # see db/sync/_conn.py
  python db/sync/sync_entity.py --qid Q6256 --label country --max 1000   # bulk one type
  python db/sync/sync_entity.py --qid Q515  --label city --schema-only   # table only, data lazy
  python db/sync/sync_entity.py --qid Q515  --label city --lazy "Kyoto"  # sync one entity
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

try:
    from _conn import connect
    from _embed import Embedder, pgvector_literal, normalize_surface
    from sync_wikidata import wdqs, qid_of, V, ENDPOINT, UA
except ImportError:
    from ._conn import connect
    from ._embed import Embedder, pgvector_literal, normalize_surface
    from .sync_wikidata import wdqs, qid_of, V, ENDPOINT, UA


def _pg():
    return connect()


def wbsearch(value, limit=7):
    """Wikidata search API (INDEXED, fast) — label/altLabel candidates for a CSV cell value."""
    url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "wbsearchentities", "search": value, "language": "en", "format": "json", "limit": limit, "type": "item"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
        return [s["id"] for s in json.load(r).get("search", [])]


def ask(q):
    """A SPARQL ASK -> bool (cheap type check, no full scan)."""
    data = urllib.parse.urlencode({"query": q, "format": "json"}).encode()
    req = urllib.request.Request(ENDPOINT, data=data, headers={
        "User-Agent": UA, "Accept": "application/sparql-results+json", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("boolean", False)

KEEP_TYPES = {"WikibaseItem", "Quantity", "Time", "String", "Monolingualtext", "GlobeCoordinate"}
DROP_LABEL = re.compile(r"\b(image|category|id|logo|banner|icon|audio|gallery|template|wikimedia)\b", re.I)


def snake(label):
    s = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return s or "prop"


def discover(qid, topk=16):
    """Return [(pid, col_name, label, wd_type)] — the type's real-ATTRIBUTE Wikidata properties,
    schema-FROM-Wikidata. Two cheap queries: (1) property frequency over a SAMPLE of instances,
    no labels; (2) labels for just the ~50 candidate properties."""
    ids = [qid_of(V(b, "x")) for b in wdqs(f"SELECT ?x WHERE {{ ?x wdt:P31 wd:{qid} }} LIMIT 40", timeout=30, retries=3)]
    ids = [i for i in ids if i]
    if not ids:
        return []
    xvals = " ".join(f"wd:{i}" for i in ids)                              # explicit instances -> WDQS stays bounded
    q1 = (f"SELECT ?prop ?t (COUNT(DISTINCT ?x) AS ?n) WHERE {{ VALUES ?x {{ {xvals} }} ?x ?dc ?v . "
          f"?prop wikibase:directClaim ?dc ; wikibase:propertyType ?t . }} GROUP BY ?prop ?t ORDER BY DESC(?n) LIMIT 50")
    cand = []
    for b in wdqs(q1, timeout=45, retries=3):
        pid = qid_of(V(b, "prop")); wt = (V(b, "t") or "").rsplit("#", 1)[-1]
        if pid != "P31" and wt in KEEP_TYPES:
            cand.append((pid, wt))
    if not cand:
        return []
    vals = " ".join(f"wd:{p}" for p, _ in cand)
    q2 = (f"SELECT ?prop ?propLabel WHERE {{ VALUES ?prop {{ {vals} }} "
          f"SERVICE wikibase:label {{ bd:serviceParam wikibase:language 'en'. }} }}")
    lbls = {qid_of(V(b, "prop")): (b.get("propLabel", {}) or {}).get("value", "") for b in wdqs(q2, timeout=30)}
    out, seen = [], {"qid", "name"}                          # reserve the key columns (a prop labeled "name"
    #                                                          otherwise collides -> CREATE TABLE DuplicateColumn)
    for pid, wt in cand:
        lbl = lbls.get(pid) or pid
        if DROP_LABEL.search(lbl):
            continue
        col = snake(lbl)
        if col in seen:
            continue
        seen.add(col); out.append((pid, col, lbl, wt))
        if len(out) >= topk:
            break
    return out


def fetch(qid, props, limit):
    """Instances of qid + each kept property, ONE ROW PER ENTITY via GROUP BY ?x + SAMPLE — multi-valued
    props (a country has several official languages) otherwise cross-product and LIMIT eats it."""
    sel = ["?x", "(SAMPLE(?nm) AS ?name)"]
    where = ["OPTIONAL { ?x rdfs:label ?nm . FILTER(lang(?nm)='en') }"]
    for i, (pid, col, _lbl, wt) in enumerate(props):                          # item-valued props keep the ENTITY IRI
        v = f"v{i}"                                                           # -> the related entity's qid below:
        where.append(f"OPTIONAL {{ ?x wdt:{pid} ?{v} }}")                    # a qid FK, NOT a label
        sel.append(f"(SAMPLE(?{v}) AS ?{col})")
    q = (f"SELECT {' '.join(sel)} WHERE {{ ?x wdt:P31 wd:{qid} . FILTER NOT EXISTS {{ ?x wdt:P576 ?d }} "
         f"{' '.join(where)} }} GROUP BY ?x LIMIT {limit}")
    rows = []
    for b in wdqs(q, timeout=58, retries=4):
        xq = qid_of(V(b, "x"))
        if not xq or not V(b, "name") or V(b, "name") == xq:
            continue
        r = {"qid": xq, "name": V(b, "name")}
        for _pid, col, _lbl, wt in props:
            val = V(b, col)
            if val is not None:
                r[col] = qid_of(val) if wt == "WikibaseItem" else val        # FK column -> related entity's qid
        rows.append(r)
    return rows


def find_entity(value, type_qid):
    """The Wikidata entity named `value` that is an instance (P31/P279*) of type_qid — the lazy lookup when
    a CSV cell didn't resolve in world.words. Search API for candidates, then a cheap ASK per candidate."""
    for cq in wbsearch(value):
        try:
            if ask(f"ASK {{ wd:{cq} wdt:P31/wdt:P279* wd:{type_qid} }}"):
                return cq
        except Exception:                                          # noqa: BLE001 — one bad candidate shouldn't abort
            continue
    return None


def fetch_one(eqid, props):
    """Property values for ONE entity (the faithful row); item-valued props keep the related entity's qid (FK)."""
    sel, where = ["(SAMPLE(?nm) AS ?name)"], [f"BIND(wd:{eqid} AS ?x)",
                                               "OPTIONAL { ?x rdfs:label ?nm . FILTER(lang(?nm)='en') }"]
    for i, (pid, col, _l, wt) in enumerate(props):
        v = f"v{i}"
        where.append(f"OPTIONAL {{ ?x wdt:{pid} ?{v} }}")                    # item-valued -> the entity IRI (qid below)
        sel.append(f"(SAMPLE(?{v}) AS ?{col})")
    bs = wdqs(f"SELECT {' '.join(sel)} WHERE {{ {' '.join(where)} }}", timeout=30, retries=2)
    if not bs:
        return None
    b = bs[0]; r = {"qid": eqid, "name": V(b, "name")}
    for _pid, col, _l, wt in props:
        val = V(b, col)
        if val is not None:
            r[col] = qid_of(val) if wt == "WikibaseItem" else val            # FK column -> related entity's qid
    return r


def wlabel(cur, type_qid):
    """the EXACT Wikidata label = the wikipedia table name for a type qid (knowledgebase."types".label)."""
    cur.execute('SELECT label FROM knowledgebase."types" WHERE qid=%s', (type_qid,))
    r = cur.fetchone()
    return (str(r[0]) if r and r[0] else type_qid)[:63]


def ensure_table(cur, label, props):
    """the faithful wikipedia table (exact-label name, qid PK + qid-FK columns); created on demand if a
    type was never mirrored (build_wikipedia.py pre-creates the known ones; this covers the long tail)."""
    coldefs = ['"qid" TEXT PRIMARY KEY', '"name" TEXT'] + [f'"{c}" TEXT' for _p, c, _l, _t in props]
    cur.execute(f'CREATE TABLE IF NOT EXISTS knowledgebase."{label}" ({", ".join(coldefs)})')


_PROPS_CACHE = {}                                                            # type_qid -> discovered props


def ensure_entity(eqid, type_qid, value=None):
    """LAZY SYNC: make sure entity `eqid` exists in knowledgebase."<exact label>" (fetch the faithful row from
    Wikidata + INSERT, qid PK + qid FKs) and is registered in knowledgebase."words" (so it resolves next time).
    Used both when a cell resolves to a qid that isn't in the (empty) table yet AND from lazy_resolve.
    Returns the qid. Idempotent."""
    conn = _pg(); conn.autocommit = True; cur = conn.cursor()
    wl = wlabel(cur, type_qid)
    props = _PROPS_CACHE.get(type_qid)
    if props is None:
        props = _PROPS_CACHE[type_qid] = discover(type_qid)
    ensure_table(cur, wl, props)
    cur.execute(f'SELECT 1 FROM knowledgebase."{wl}" WHERE qid=%s', (eqid,))
    if cur.fetchone():
        return eqid                                                          # already synced
    row = fetch_one(eqid, props) or {"qid": eqid, "name": value or eqid}
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='knowledgebase' AND table_name=%s", (wl,))
    existing = {r[0] for r in cur.fetchall()}                                # insert only into columns that exist
    cols = [c for c in (["qid", "name"] + [c for _p, c, _l, _t in props]) if c in existing]
    cur.execute(f'INSERT INTO knowledgebase."{wl}" ({", ".join(chr(34)+c+chr(34) for c in cols)}) '
                f'VALUES ({", ".join(["%s"]*len(cols))}) ON CONFLICT (qid) DO NOTHING',
                [str(row.get(c)) if row.get(c) is not None else None for c in cols])
    nm = row.get("name") or value or eqid
    vec = Embedder.get().encode([nm])[0]
    cur.execute('INSERT INTO knowledgebase."words"(surface,canonical,type,norm,embedding,qid) VALUES (%s,%s,%s,%s,%s::vector,%s) '
                'ON CONFLICT DO NOTHING', (value or nm, nm, wl, normalize_surface(nm), pgvector_literal(vec), eqid))
    print(f'  lazy: synced {nm!r} ({eqid}) -> knowledgebase."{wl}"', flush=True)
    return eqid


def lazy_resolve(value, type_qid, label=None):
    """A CSV cell that didn't resolve in world.words -> find it in Wikidata (search+ASK), then
    ensure_entity syncs the faithful row into knowledgebase."<exact label>". Returns the qid or None."""
    eqid = find_entity(value, type_qid)
    if not eqid:
        print(f"  lazy: {value!r} not found in Wikidata as {type_qid}", flush=True); return None
    return ensure_entity(eqid, type_qid, value)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--qid"); ap.add_argument("--label", required=True)
    ap.add_argument("--max", type=int, default=1000); ap.add_argument("--show", action="store_true")
    ap.add_argument("--schema-only", action="store_true", help="create the faithful table but DON'T bulk-populate (lazy data)")
    ap.add_argument("--lazy", help="LAZY: find this value in Wikidata as --label/--qid, sync it + register in words")
    a = ap.parse_args()

    if a.lazy:
        lazy_resolve(a.lazy, a.qid, a.label); return 0

    print(f"discovering Wikidata schema for {a.label} ({a.qid})...", flush=True)
    props = discover(a.qid)
    print("  faithful columns:", [c for _p, c, _l, _t in props], flush=True)
    if not props:
        print("  no real-attribute properties (abstract type?) — skipped"); return 0

    if a.schema_only:                                          # FULL SCHEMA MIRROR: create the table, data stays lazy
        conn = _pg(); conn.autocommit = True; cur = conn.cursor()
        ensure_table(cur, a.label, props)
        print(f'  CREATED (schema only) knowledgebase."{a.label}" ({len(props)} property columns) — data lazy', flush=True)
        return 0

    rows = fetch(a.qid, props, a.max)
    print(f"  fetched {len(rows)} instances", flush=True)

    conn = _pg(); conn.autocommit = True; cur = conn.cursor()
    tbl = a.label
    coldefs = ['"qid" TEXT PRIMARY KEY', '"name" TEXT'] + [f'"{c}" TEXT' for _p, c, _l, _t in props]
    cur.execute(f'DROP TABLE IF EXISTS knowledgebase."{tbl}"')
    cur.execute(f'CREATE TABLE knowledgebase."{tbl}" ({", ".join(coldefs)})')
    cols = ["qid", "name"] + [c for _p, c, _l, _t in props]
    ins = f'INSERT INTO knowledgebase."{tbl}" ({", ".join(chr(34)+c+chr(34) for c in cols)}) VALUES ({", ".join(["%s"]*len(cols))}) ON CONFLICT (qid) DO NOTHING'
    for r in rows:
        cur.execute(ins, [str(r.get(c)) if r.get(c) is not None else None for c in cols])
    cur.execute(f'SELECT COUNT(*) FROM knowledgebase."{tbl}"')
    print(f'  CREATED knowledgebase."{tbl}" ({", ".join(cols)}) = {cur.fetchone()[0]} rows', flush=True)
    if a.show:
        cur.execute(f'SELECT * FROM knowledgebase."{tbl}" ORDER BY name LIMIT 4')
        for r in cur.fetchall():
            print("   ", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
