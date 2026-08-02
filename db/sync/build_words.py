"""Build knowledgebase."words" — the pgvector entity-resolution index.

Each row is a SURFACE form (a string someone might type) -> its CANONICAL world entity:
  (surface, canonical, type, props, norm, embedding vector(384), qid, canon_country, is_primary)
  - canonical rows:  surface == canonical == the entity label            (e.g. "United States")
  - altLabel rows:   surface = a Wikidata alias, canonical = the entity   (e.g. "Holland" -> "Netherlands")
`norm` = normalize_surface(surface) for deterministic exact match; `embedding` = bge-small(surface)
for the fuzzy <=> nearest-neighbour fallback (typos / novel forms).

Phases (all in one run):
  1. canonical labels from knowledgebase."Countries"/"States"/"Elements"/"Continents" (+ "Cities" with --cities)
  2. country altLabels from Wikidata (mapped to OUR canonical name by QID via public.country)
  3. KEY the rows: qid / canon_country / is_primary backfilled from public.settlement/admin/country
     (enables the qid-keyed cell bridge + same-name disambiguation)
  4. city altLabels from Wikidata for cities with population >= 100k (--city-aliases; 'Bombay' -> Mumbai)

Requires: db/init.sql applied; sync_wikidata.py + build_world.py run first; torch+transformers
installed (bge-small-en-v1.5 downloads on first use); network for query.wikidata.org.

Run:
  export KB_PG_HOST=... KB_PG_PASSWORD=...        # see db/sync/_conn.py
  python db/sync/build_words.py --cities --city-aliases  # full index (~200k city labels; minutes on CPU)
  python db/sync/build_words.py --cities                 # minimal seed (skip the alias crawl)
"""
from __future__ import annotations
import argparse
import time

from psycopg2.extras import Json, execute_values

try:
    from _conn import connect
    from _embed import Embedder, pgvector_literal, normalize_surface
    from sync_wikidata import wdqs
except ImportError:
    from ._conn import connect
    from ._embed import Embedder, pgvector_literal, normalize_surface
    from .sync_wikidata import wdqs

SMALL = [("country", "Countries", "name"), ("state", "States", "name"),
         ("element", "Elements", "name"), ("continent", "Continents", "name")]
CITY = ("city", "Cities", "name")
MIN_POP = 100000                     # cities at/above this get altLabels (~7.4k cities)

# keying: qid + country + global is_primary backfilled from the raw public.* import
UPD_CITY = '''
UPDATE knowledgebase."words" w SET qid = s.qid,
       canon_country = w.props->>'country',
       is_primary = COALESCE((w.props->>'is_primary')::boolean, false)
FROM public.settlement s
WHERE w.type='city' AND w.surface = s.name
  AND w.props->>'country' IS NOT DISTINCT FROM s.country
  AND (w.props->>'population')::bigint IS NOT DISTINCT FROM s.population;
'''
UPD_STATE = '''
UPDATE knowledgebase."words" w SET qid = a.qid, canon_country = w.props->>'country', is_primary = true
FROM public.admin a
WHERE w.type='state' AND w.surface = a.name AND w.props->>'country' IS NOT DISTINCT FROM a.country;
'''
UPD_COUNTRY = '''
UPDATE knowledgebase."words" w SET qid = c.qid, is_primary = true
FROM public.country c WHERE w.type='country' AND w.canonical = c.name;
'''


def _insert(cur, emb, rows, chunk=2000):
    """rows = [(surface, canonical, type, props_dict)] -> embed surfaces + bulk insert."""
    n = 0
    for i in range(0, len(rows), chunk):
        part = rows[i:i + chunk]
        vecs = emb.encode([r[0] for r in part])
        data = [(s, c, t, Json(p), normalize_surface(s), pgvector_literal(v))
                for (s, c, t, p), v in zip(part, vecs)]
        execute_values(cur, 'INSERT INTO knowledgebase."words" (surface,canonical,type,props,norm,embedding) VALUES %s',
                       data, template='(%s,%s,%s,%s,%s,%s::vector)')
        n += len(part)
        if len(rows) > chunk:
            print(f"      {n}/{len(rows)}")
    return n


def canonical_rows(cur, type_, table, label_col):
    cur.execute(f'SELECT "{label_col}" AS w, row_to_json(t) AS p FROM knowledgebase."{table}" t WHERE "{label_col}" IS NOT NULL')
    return [(r[0], r[0], type_, r[1]) for r in cur.fetchall()]   # surface == canonical


def country_altlabel_rows(cur):
    """Wikidata English altLabels for our countries, mapped to OUR canonical name by QID (public.country.qid)."""
    cur.execute("SELECT qid, name FROM public.country WHERE qid IS NOT NULL AND name IS NOT NULL")
    qid2name = {(q if str(q).startswith("Q") else "Q" + str(q)): n for q, n in cur.fetchall()}
    qids, rows, seen = list(qid2name), [], set()
    for i in range(0, len(qids), 60):
        vals = " ".join("wd:" + q for q in qids[i:i + 60])
        q = (f'SELECT ?c ?alias WHERE {{ VALUES ?c {{ {vals} }} '
             f'?c skos:altLabel ?alias . FILTER(LANG(?alias)="en") }}')
        for b in wdqs(q, timeout=120, retries=4):
            qid = b["c"]["value"].rsplit("/", 1)[-1]
            nm = qid2name.get(qid)
            alias = b["alias"]["value"].strip()
            if nm and alias and normalize_surface(alias) and (alias.lower(), nm) not in seen:
                seen.add((alias.lower(), nm)); rows.append((alias, nm, "country", {"alias_of": nm}))
    return rows


def import_city_altlabels(cur, emb):
    """Wikidata altLabels for big cities, keyed by qid — 'Bombay' -> Mumbai's qid is DATA, not fuzzy match."""
    cur.execute("""SELECT qid, name, country, population FROM public.settlement s
                   WHERE population >= %s AND qid IS NOT NULL""", (MIN_POP,))
    meta = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}   # qid -> (name, country, population)
    # global is_primary per name (matches the city rows): compute from settlement
    cur.execute("""SELECT qid FROM (SELECT qid, row_number() OVER (PARTITION BY lower(name)
                   ORDER BY population DESC NULLS LAST) rn FROM public.settlement) t WHERE rn=1""")
    primary = {r[0] for r in cur.fetchall()}
    qids, rows, seen = list(meta), [], set()
    for i in range(0, len(qids), 50):
        vals = " ".join("wd:" + q for q in qids[i:i + 50])
        q = (f'SELECT ?c ?alias WHERE {{ VALUES ?c {{ {vals} }} '
             f'?c skos:altLabel ?alias . FILTER(LANG(?alias)="en") }}')
        try:
            res = wdqs(q, timeout=120, retries=3)
        except Exception as e:                                   # noqa: BLE001
            print(f"    WDQS batch {i} failed ({e}); continuing"); time.sleep(2); continue
        for b in res:
            qid = b["c"]["value"].rsplit("/", 1)[-1]
            alias = b["alias"]["value"].strip()
            nm, co, pop = meta.get(qid, (None, None, None))
            if nm and alias and normalize_surface(alias) and normalize_surface(alias) != normalize_surface(nm) \
               and (alias.lower(), qid) not in seen:
                seen.add((alias.lower(), qid))
                rows.append((alias, nm, co, qid, qid in primary,
                             Json({"country": co, "population": pop, "alias_of": nm})))
        if i % 1000 == 0:
            print(f"    altLabels: {i}/{len(qids)} cities scanned, {len(rows)} aliases")
    # embed + insert the alias surfaces
    for i in range(0, len(rows), 1000):
        part = rows[i:i + 1000]
        vecs = emb.encode([r[0] for r in part])
        data = [(s, cn, cc, qid, prim, "city", pr, normalize_surface(s), pgvector_literal(v))
                for (s, cn, cc, qid, prim, pr), v in zip(part, vecs)]
        execute_values(cur, 'INSERT INTO knowledgebase."words" '
                       '(surface,canonical,canon_country,qid,is_primary,type,props,norm,embedding) VALUES %s',
                       data, template='(%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)')
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cities", action="store_true", help="also embed all city canonical labels (~200k full sync)")
    ap.add_argument("--city-aliases", action="store_true", help="also crawl Wikidata altLabels for pop>=100k cities")
    a = ap.parse_args()

    emb = Embedder.get()
    cn = connect(); cur = cn.cursor()
    # rebuild from scratch; drop the HNSW index during the bulk load (recreate after — much faster)
    cur.execute('TRUNCATE knowledgebase."words"')
    cur.execute('DROP INDEX IF EXISTS world.ix_words_hnsw'); cn.commit()

    for type_, table, col in SMALL:
        print(f"  canonical <- {table} ({type_})")
        print(f"    {type_}: {_insert(cur, emb, canonical_rows(cur, type_, table, col))} rows"); cn.commit()
    print("  altLabels <- Wikidata (countries, by QID)")
    alt = country_altlabel_rows(cur)
    print(f"    country aliases: {_insert(cur, emb, alt)} surface rows"); cn.commit()
    if a.cities:
        print("  canonical <- Cities (city)  [minutes on CPU]")
        print(f"    city: {_insert(cur, emb, canonical_rows(cur, *CITY))} rows"); cn.commit()

    print("  keying qid / canon_country / is_primary from public.*")
    for label, sql in (("city", UPD_CITY), ("state", UPD_STATE), ("country", UPD_COUNTRY)):
        cur.execute(sql); print(f"    keyed {label} rows: {cur.rowcount}"); cn.commit()

    if a.city_aliases:
        print(f"  importing city altLabels (pop>={MIN_POP}) from Wikidata...")
        n = import_city_altlabels(cur, emb); cn.commit()
        print(f"    city altLabel surfaces added: {n}")

    # HNSW cosine index (pgvector defaults m=16, ef_construction=64) + the exact-match btrees (init.sql set)
    print("  building HNSW index...")
    cur.execute('CREATE INDEX IF NOT EXISTS ix_words_hnsw ON knowledgebase."words" USING hnsw (embedding vector_cosine_ops)')
    cn.commit()
    cur.execute('SELECT type, count(*) FROM knowledgebase."words" GROUP BY type ORDER BY 2 DESC')
    print("  totals by type:", dict(cur.fetchall()))
    cur.execute("SELECT count(*) FROM knowledgebase.\"words\" WHERE type='city' AND qid IS NOT NULL")
    print("  city rows with qid:", cur.fetchone()[0])
    cn.close()


if __name__ == "__main__":
    main()
