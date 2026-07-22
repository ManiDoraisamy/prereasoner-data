"""knowledge_sync.py — LAZY Wikidata fill for the faithful world tables, used at serving time.

The world DB mirrors Wikidata EXACTLY: for a taxonomy type (qid) the table's columns are the type's ACTUAL
Wikidata properties (property-frequency schema discovery), not hand-picked friendly names. At serving time a
CSV cell that didn't resolve in knowledgebase."words" is looked up in Wikidata (search API + a cheap SPARQL ASK type
check), its faithful row is fetched and inserted into knowledgebase."<exact label>" (qid PK + qid FKs), and the
entity is registered in knowledgebase."words" so it resolves next time. Idempotent, best-effort — a sync miss never
blocks the query.
"""
from __future__ import annotations
import json
import re
import time
import urllib.parse
import urllib.request

from engine.pg import _pg
from engine.taxonomy import snake  # noqa: F401  (re-exported: callers import snake from here)

UA = "prereasoner-engine/1.0 (mani.doraisamy@gmail.com)"
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
            print(f"    WDQS retry {attempt+1}/{retries} ({getattr(e,'code',None) or type(e).__name__}) "
                  f"wait {wait}s", flush=True)
            time.sleep(wait)
    raise last


def V(b, k):
    return b[k]["value"] if k in b else None


def qid_of(uri):
    return uri.rsplit("/", 1)[-1] if uri else None


def wbsearch(value, limit=7):
    """Wikidata search API (INDEXED, fast) — label/altLabel candidates for a CSV cell value."""
    url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "wbsearchentities", "search": value, "language": "en", "format": "json",
         "limit": limit, "type": "item"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
        return [s["id"] for s in json.load(r).get("search", [])]


def ask(q):
    """A SPARQL ASK -> bool (cheap type check, no full scan)."""
    data = urllib.parse.urlencode({"query": q, "format": "json"}).encode()
    req = urllib.request.Request(ENDPOINT, data=data, headers={
        "User-Agent": UA, "Accept": "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("boolean", False)


KEEP_TYPES = {"WikibaseItem", "Quantity", "Time", "String", "Monolingualtext", "GlobeCoordinate"}
DROP_LABEL = re.compile(r"\b(image|category|id|logo|banner|icon|audio|gallery|template|wikimedia)\b", re.I)


def discover(qid, topk=16):
    """Return [(pid, col_name, label, wd_type)] — the type's real-ATTRIBUTE Wikidata properties, schema-FROM-
    Wikidata. Two cheap queries (the label SERVICE over the full statement scan is what timed out): (1) property
    frequency over a SAMPLE of instances, no labels; (2) labels for just the ~50 candidate properties."""
    ids = [qid_of(V(b, "x")) for b in wdqs(f"SELECT ?x WHERE {{ ?x wdt:P31 wd:{qid} }} LIMIT 40",
                                           timeout=30, retries=3)]
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
    out, seen = [], {"qid", "name"}                          # reserve the key columns (a Wikidata prop labeled "name"
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


def find_entity(value, type_qid):
    """The Wikidata entity named `value` that is an instance (P31/P279*) of type_qid — the lazy lookup when a
    CSV cell didn't resolve in world.words. Search API for candidates (fast), then a cheap ASK per candidate
    for the type."""
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
    # Widened for dev-machine robustness: WDQS reads can be slow/rate-limited off Cloud Run, and a timeout
    # here makes an entity silently fail to lazy-fill -> undercounted sums (see docs/notes/naming.md).
    bs = wdqs(f"SELECT {' '.join(sel)} WHERE {{ {' '.join(where)} }}", timeout=60, retries=4)
    if not bs:
        return None
    b = bs[0]; r = {"qid": eqid, "name": V(b, "name")}
    for _pid, col, _l, wt in props:
        val = V(b, col)
        if val is not None:
            r[col] = qid_of(val) if wt == "WikibaseItem" else val            # FK column -> related entity's qid
    return r


def wlabel(cur, type_qid):
    """the EXACT Wikidata label = the wikipedia table name for a type qid (knowledgebase.\"types\".label)."""
    cur.execute('SELECT label FROM knowledgebase."types" WHERE qid=%s', (type_qid,))
    r = cur.fetchone()
    return (str(r[0]) if r and r[0] else type_qid)[:63]


def ensure_table(cur, label, props):
    """the faithful wikipedia table (exact-label name, qid PK + qid-FK columns); created on demand if a type was
    never mirrored. (The offline schema build pre-creates the known ones; this covers the long tail.)"""
    coldefs = ['"qid" TEXT PRIMARY KEY', '"name" TEXT'] + [f'"{c}" TEXT' for _p, c, _l, _t in props]
    cur.execute(f'CREATE TABLE IF NOT EXISTS knowledgebase."{label}" ({", ".join(coldefs)})')


_PROPS_CACHE = {}                                        # type_qid -> discovered props (avoid re-discovery per entity)


def ensure_entity(eqid, type_qid, value=None):
    """Lazy sync: make sure entity `eqid` exists in knowledgebase.\"<exact label>\" (fetch the faithful row from
    Wikidata + INSERT, qid PK + qid FKs) and is registered in knowledgebase.\"words\" (so it resolves next time). Used
    both when a cell resolves to a qid that isn't in the (empty) table yet AND from lazy_resolve. Returns the
    qid. Idempotent."""
    from engine.embeddings import Embedder, pgvector_literal, normalize_surface
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
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='knowledgebase' "
                "AND table_name=%s", (wl,))
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
    """A CSV cell that didn't resolve in world.words -> find it in Wikidata (search+ASK), then ensure_entity
    syncs the faithful row into knowledgebase."<exact label>". `label` is ignored (the wikipedia table name comes
    from the type qid). Returns the qid or None."""
    eqid = find_entity(value, type_qid)
    if not eqid:
        print(f"  lazy: {value!r} not found in Wikidata as {type_qid}", flush=True); return None
    return ensure_entity(eqid, type_qid, value)
