"""Build the friendly `world` tables the SQL planner references, from the raw
public.* import (run sync_wikidata.py or import_dump.py first):

  world."Cities"      <- public.settlement  (+ qid, + is_primary = most populous per name)
  world."Countries"   <- public.country     (+ valid_from/valid_to for as-of filters)
  world."Places"      <- canonical settlement per name + country names
  world."Elements"    <- public.element
  world."Continents"  <- public.continent
  world."States"      <- public.admin
  world."Country Aliases" <- curated alias list (legacy; world."words" supersedes it)

The tables and their "... in the World" views are created by db/init.sql; this
script only (re)populates them. Consumed by the engine's planner (query14/15)
and the composition path (world18 ENTITY_ATTRS), and read by build_words.py to
seed the words index.

Run:
  export WORLD_PG_HOST=... WORLD_PG_PASSWORD=...        # see db/sync/_conn.py
  python db/sync/build_world.py
"""
from __future__ import annotations
from datetime import date

try:
    from _conn import connect                     # run as a script from db/sync
except ImportError:
    from ._conn import connect                    # imported as a package module

SRC = "Wikidata (live WDQS)"

TRUNCATE = '''
TRUNCATE world."Cities", world."Countries", world."Places",
         world."Elements", world."Continents", world."States", world."Country Aliases";
'''

# one row per settlement; is_primary = 1 where its population is the max for that (lowercased) name;
# qid carried through so the bridge / world18 can join Cities by the stable key.
POP_CITIES = '''
INSERT INTO world."Cities" (name, country, population, is_primary, updated_at, source, qid)
SELECT s.name, COALESCE(s.country, ''), COALESCE(s.population, 0),
       CASE WHEN COALESCE(s.population, 0) = m.maxpop THEN 1 ELSE 0 END, %(stamp)s, %(src)s, s.qid
FROM public.settlement s
JOIN (SELECT lower(name) ln, max(COALESCE(population, 0)) maxpop
        FROM public.settlement GROUP BY lower(name)) m
  ON lower(s.name) = m.ln
WHERE s.name IS NOT NULL;
'''

POP_COUNTRIES = '''
INSERT INTO world."Countries" (name, currency, currency_name, continent, valid_from, valid_to, updated_at, source)
SELECT name, COALESCE(currency_code, ''), lower(COALESCE(currency_name, '')), COALESCE(continent, '?'),
       '1900-01-01', NULL, %(stamp)s, %(src)s
FROM public.country WHERE name IS NOT NULL;
'''

# the canonical (most-populous) settlement per name, kind='city'
POP_PLACES_CITIES = '''
INSERT INTO world."Places" (name, kind, lat, lng, hemisphere, population, updated_at, source)
SELECT DISTINCT ON (lower(s.name)) s.name, 'city', s.lat, s.lng,
       CASE WHEN COALESCE(s.lat, 0) >= 0 THEN 'northern' ELSE 'southern' END,
       COALESCE(s.population, 0), %(stamp)s, %(src)s
FROM public.settlement s WHERE s.name IS NOT NULL
ORDER BY lower(s.name), COALESCE(s.population, 0) DESC;
'''
# + country names that aren't already a place (centroid unknown -> 0,0), kind='country'
POP_PLACES_COUNTRIES = '''
INSERT INTO world."Places" (name, kind, lat, lng, hemisphere, population, updated_at, source)
SELECT c.name, 'country', 0, 0, 'northern', 0, %(stamp)s, %(src)s
FROM public.country c
WHERE c.name IS NOT NULL AND lower(c.name) NOT IN (SELECT lower(name) FROM world."Places");
'''

# element names are stored lowercase in public.element -> initcap for display ("hydrogen" -> "Hydrogen");
# routing/joins are lower()-normalized so case never matters for matching.
POP_ELEMENTS = '''
INSERT INTO world."Elements" (name, symbol, atomic_number, mass, updated_at, source)
SELECT initcap(name), symbol, atomic_number, mass, %(stamp)s, %(src)s
FROM public.element WHERE name IS NOT NULL;
'''
POP_CONTINENTS = '''
INSERT INTO world."Continents" (name, updated_at, source)
SELECT name, %(stamp)s, %(src)s FROM public.continent WHERE name IS NOT NULL;
'''
POP_STATES = '''
INSERT INTO world."States" (name, country, population, level, updated_at, source)
SELECT name, COALESCE(country, ''), population, COALESCE(level, ''), %(stamp)s, %(src)s
FROM public.admin WHERE name IS NOT NULL;
'''

# NORMALIZATION TABLE (legacy): alias/variant -> canonical country name. Deliberately NOT ISO2 codes —
# 2-letter codes (IN=India, IT=Italy, AT=Austria) collide with English words.
POP_ALIASES_SELF = '''
INSERT INTO world."Country Aliases" (alias, name)
SELECT DISTINCT lower(name), name FROM world."Countries" WHERE name IS NOT NULL;
'''
COUNTRY_ALIASES = [
    ("us", "United States"), ("u.s.", "United States"), ("u.s.a.", "United States"),
    ("usa", "United States"), ("america", "United States"),
    ("uk", "United Kingdom"), ("u.k.", "United Kingdom"), ("gb", "United Kingdom"),
    ("britain", "United Kingdom"), ("great britain", "United Kingdom"),
    ("uae", "United Arab Emirates"), ("u.a.e.", "United Arab Emirates"),
    ("holland", "Netherlands"),
]


def main():
    cn = connect(); cur = cn.cursor()
    p = {"stamp": date.today().isoformat(), "src": SRC}
    cur.execute(TRUNCATE); cn.commit()
    cur.execute(POP_CITIES, p)
    cur.execute(POP_COUNTRIES, p)
    cur.execute(POP_PLACES_CITIES, p)
    cur.execute(POP_PLACES_COUNTRIES, p)
    cur.execute(POP_ELEMENTS, p)
    cur.execute(POP_CONTINENTS, p)
    cur.execute(POP_STATES, p)
    cur.execute(POP_ALIASES_SELF)
    cur.execute('SELECT name FROM world."Countries"')
    have = {r[0] for r in cur.fetchall()}
    cur.executemany('INSERT INTO world."Country Aliases" (alias, name) VALUES (%s, %s)',
                    [(a, n) for a, n in COUNTRY_ALIASES if n in have])   # only aliases whose canonical PK exists
    cn.commit()
    for t in ("Cities", "Countries", "Places", "Elements", "Continents", "States", "Country Aliases"):
        cur.execute(f'SELECT count(*) FROM world."{t}"')
        print(f'  world."{t}": {cur.fetchone()[0]}')
    cn.close()
    print('world schema populated (tables + "... in the World" views).')


if __name__ == "__main__":
    main()
