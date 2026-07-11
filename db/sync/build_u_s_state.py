"""Populate the qid-keyed world."u_s_state" from the name-keyed world."States" + the words index.

State/province/region spans many Wikidata types (U.S. state, region of Italy, German state, …), so unlike
city (Q515) / country (Q6256) there is no single wikipedia."<type>" table. This builds the aggregate,
qid-keyed world."u_s_state" (state qid PK; country/continent as qid FKs) so a state column can join
qid-keyed and filter by country/continent — the same path city/country use (docs/notes/naming.md).

NO Wikidata (WDQS) calls: it derives everything from data already in the DB
  - world."States"  : name-keyed (state name -> country name), populated by build_world
  - world."words"   : type='state' (state name -> qid), type='country' (country name -> qid)
  - wikipedia."country" : country qid -> continent qid (for the 2-hop continent FK)

Run AFTER build_world / build_words:  python -m db.sync.build_u_s_state
"""
from __future__ import annotations

import os

import psycopg2

from engine.embeddings import normalize_surface


def _conn():
    return psycopg2.connect(
        host=os.environ.get("WORLD_PG_HOST", "localhost"),
        port=int(os.environ.get("WORLD_PG_PORT", "5432")),
        dbname=os.environ.get("WORLD_PG_DB", "world"),
        user=os.environ.get("WORLD_PG_USER", "postgres"),
        password=os.environ["WORLD_PG_PASSWORD"],
        sslmode=os.environ.get("WORLD_PG_SSLMODE", "prefer"),
    )


def main():
    c = _conn()
    cur = c.cursor()
    cur.execute("SELECT norm, qid FROM world.\"words\" WHERE type='state'")
    states = dict(cur.fetchall())
    cur.execute("SELECT norm, qid FROM world.\"words\" WHERE type='country'")
    countries = dict(cur.fetchall())
    # country qid -> continent qid, for the 2-hop FK (parity with city.country.continent).
    cur.execute('SELECT qid, continent FROM wikipedia."country" WHERE continent IS NOT NULL')
    continent_of = dict(cur.fetchall())

    cur.execute('SELECT name, country FROM world."States"')
    src = cur.fetchall()

    # Idempotent rebuild: the table is the derived, qid-keyed projection of world."States".
    cur.execute('TRUNCATE world."u_s_state"')
    ins = skipped = 0
    for name, country in src:
        sq = states.get(normalize_surface(name or ""))
        if not sq:
            skipped += 1
            continue
        cq = countries.get(normalize_surface(country or ""))
        cont = continent_of.get(cq) if cq else None
        # ON CONFLICT: two state-name spellings in world."States" can normalize to the same qid.
        cur.execute('INSERT INTO world."u_s_state" (qid, name, country, continent) VALUES (%s,%s,%s,%s) '
                    'ON CONFLICT (qid) DO NOTHING', (sq, name, cq, cont))
        ins += cur.rowcount
    c.commit()
    cur.execute('SELECT count(*) FROM world."u_s_state"')
    total = cur.fetchone()[0]
    print(f"world.u_s_state populated: inserted {ins}, skipped {skipped} (no state qid), total {total}")


if __name__ == "__main__":
    main()
