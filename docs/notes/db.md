# db/ — reverse-engineered database contract

Source of truth: `C:\work\prereasoner-flat-data\runtime20` (plus runtime13–16 setup
scripts that the live database was actually built with — runtime20 inherited the
tables without recreating them). This note records what the code proves, what was
inferred, and what only the live database could confirm.

## 1. Extensions

| Extension | Why | Evidence |
|---|---|---|
| `vector` (pgvector) | `knowledgebase."words".embedding vector(384)` + HNSW `<=>` cosine search; per-user `"<t> unconnected to wikipedia".embedding vector(896)` | `runtime16/scripts/setup_words16.py` DDL; `query16._nn` / `_cell_bridge_sql` (LATERAL `<=>`); `world17._persist_main_unconn` (`vector({self.hdim})`, hdim=896 for the unified Qwen encoder) |
| `pg_trgm` | GIN trigram index on `public.entity_label(lower(label))` | `runtime20/schema.sql` line 6 + `ix_label_trgm` |
| ~~postgis / earthdistance / cube~~ | **NOT used.** Geo NEARBY (`world19._nearby`) computes haversine in plain SQL (`acos/radians/cos/sin` over `public.settlement.lat/lng`) | grep for `earthdistance|postgis|ll_to_earth|cube` = zero code hits |

## 2. Schemas

- `public` — raw Wikidata import (`runtime20/schema.sql`, executed unqualified ⇒
  default search_path ⇒ public). Read directly by the engine in two places:
  `world19` NEARBY (`public.settlement`) and `key_words16`-style backfills.
- `knowledgebase` — THE shared serving schema (named `knowledgebase`, **not** `world`,
  because "world model" means a learned dynamics model in ML — this is a lookup KB).
  Holds `"words"` (resolution index), `"types"` (taxonomy), the qid-keyed faithful
  Wikidata tables (exact-Wikidata-label names — `knowledge_sync.ensure_table`,
  `build_wikipedia.py`), AND the friendly name-keyed tables + `"... in the World"` views
  (`"Cities"`/`"Countries"`/`"Elements"`/… read by `knowledge_compose._enrich`). This
  schema was consolidated from the former `world` + `wikipedia` schemas.
- `"<google-sub>"` — per-user, created at request time by
  `pg._load_user_schema` (`CREATE SCHEMA IF NOT EXISTS "<sub>"`); `<sub>` is
  the server-verified Firebase/Google `sub` (long numeric id). Queries run with
  `SET search_path TO "<sub>", knowledgebase, public` (knowledgebase so bare
  `city`/`country` resolve to the qid-keyed tables; public last so the `vector`
  type + `<=>` operator resolve).
- `capped` — **training-only** (capped1/setup.py: `capped.type`, `capped.entity`,
  `capped.entity_type`, `capped.load_counts`). Read by `build20.py` /
  `build_from_entity.py` to produce training CSVs. Not required for serving, so
  deliberately **not** included in `db/init.sql`.

## 3. Static tables (created by `db/init.sql`)

### public (from `runtime20/schema.sql` + importer)
`continent(qid PK, name)`, `country(qid PK, name, iso2, iso3, continent_qid,
continent, capital_qid, currency_code, currency_name, population bigint,
area_km2 float8, official_language)`, `admin(qid PK, name, country_qid, country,
parent_qid, level, population, capital_qid)`, `settlement(qid PK, name,
country_qid, country, admin_qid, admin, population, lat, lng, timezone,
is_capital bool)`, `currency(code PK, qid, name, symbol)`, `element(symbol PK,
qid, name, atomic_number, mass)`, `timezone(qid PK, name, utc_offset)`,
`entity_label(qid, label, lang, is_alias, kind)`, `import_ckpt(chunk PK)`
(checkpoint table created ad hoc by `build_world_pg.ckpt_done`).
Indexes exactly as in schema.sql (incl. the `gin_trgm_ops` one).

### knowledgebase."words" — the resolution index
Base DDL from `runtime16/scripts/setup_words16.py`:
`(id bigserial PK, surface text, canonical text, type text, props jsonb, norm
text, embedding vector(384))`; then `runtime16/scripts/key_words16.py` ALTERs in
`qid text, canon_country text, is_primary boolean`. init.sql creates the union
up front. Indexes:
- `ix_words_type_norm (type, norm)` — exact normalized match
- `ix_words_hnsw USING hnsw (embedding vector_cosine_ops)` — **no explicit
  parameters in source ⇒ pgvector defaults m=16, ef_construction=64 are the
  contract**
- `ix_words_type_qid (type, qid)`, `ix_words_city_norm (norm) WHERE type='city'`
  (key_words16.IDX)

`words.type` values: `city|country|state|element|continent` (legacy resolver
strings), `type` (taxonomy labels, from sync_world_types), and the **exact
Wikidata label** for lazily-synced non-geo entities (ensure_entity inserts
`type=<knowledgebase."types".label>`, e.g. `academic journal` — note: NOT snake_cased).

### knowledgebase."types" — taxonomy DAG
`sync_world_types.py`: `(qid PK, label, parent_qid, is_leaf bool, world_table
text, depth int)` + `unify_words_qid.py` adds `resolver_type text`
('city'→Q515, 'country'→Q6256, 'state'→Q35657). Indexes on parent_qid,
is_leaf, resolver_type.

### world friendly tables (+ `"... in the World"` views)
From `runtime14/scripts/setup_world_schema.py` + `runtime15/scripts/setup_world15.py`
+ `runtime16/scripts/add_city_qid.py`:
- `"Cities"(name, country, population bigint, is_primary int, updated_at, source, qid)`
- `"Countries"(name, currency, currency_name, continent, valid_from, valid_to, updated_at, source)`
- `"Places"(name, kind, lat, lng, hemisphere, population, updated_at, source)`
- `"Elements"(name, symbol, atomic_number int, mass float8, updated_at, source)`
- `"Continents"(name, updated_at, source)`
- `"States"(name, country, population bigint, level, updated_at, source)`
- `"Country Aliases"(alias, name)` — legacy, superseded by words but still built
- views `"Cities in the World"` … `"States in the World"`; lower(name) indexes.
Consumers: the planner's world joins (query14/15 `words[wt]` configs), world18
`ENTITY_ATTRS` (joins `Cities` by **qid**, others by name).

## 4. Dynamically / lazily created objects

| Object | Naming | Creator code path |
|---|---|---|
| `knowledgebase."<exact Wikidata label>"` | e.g. `knowledgebase."city"`, `knowledgebase."hospital"`, `knowledgebase."academic journal"` — label from `knowledgebase."types".label`, truncated to 63 chars; shared labels suffixed `" (Qxxx)"` by build_wikipedia | `sync_wikidata_world.ensure_table` (via `ensure_entity`/`lazy_resolve`, called from `query16._city_bridge_sql`, `world17._resolve_world_qid`/`_serve_world_type`); pre-creatable by `mirror_world_schema.py` (world.* mirror) + `build_wikipedia_schema.py` (qid-PK copies) |
| rows in wikipedia tables | one row per entity, qid PK, all-TEXT property columns, item-valued props store the related entity's **qid** (FK) | `ensure_entity` → `fetch_one` (WDQS), `ON CONFLICT (qid) DO NOTHING`; each also INSERTs a `knowledgebase."words"` row |
| `"<sub>"` schema | verified Google `sub` (≥15-digit numeric) | `query14._load_user_schema` |
| `"<sub>"."<upload>"` | CSV table name | same (DROP + CREATE on re-upload; INTEGER→BIGINT, REAL→double precision) |
| `"<sub>"."<t> connected to wikipedia"` | `('column' text, value text, world_type text, world_key text, country text, world_qid text)` | `world17._persist_connected` (CONN_DDL; migration guard for the 5→6-col upgrade) |
| `"<sub>"."<t> unconnected to wikipedia"` | `(__pk bigint, 'column' text, value text, embedding vector(896))` | `world17._persist_main_unconn` (hdim = unified Qwen encoder hidden size 896) |

## 5. Lazy-fill vs pre-sync (confirmed from code)

Lazy at query time (never needs pre-sync): the `wikipedia` schema's tables AND
rows (`ensure_entity` creates table + fetches one entity from Wikidata per miss;
`query16._city_bridge_sql` lazily syncs resolved city qids + their countries so
2-hop joins hit). Per-user schemas/bridges.

Pre-sync required: `knowledgebase."words"` (all resolution paths gate on it — an empty
index resolves nothing, and even `_resolve_world_qid`'s lazy branch fires only
after words/types lookups), `knowledgebase."types"` (lazy table naming reads
`types.label`), `public.settlement` (NEARBY reads it directly),
`knowledgebase."Cities"/"Countries"/…` (planner + world18 read them).

## 6. Connection contract

Old code: `host = env KB_PG_HOST default 34.123.19.176; dbname="world";
user="postgres"; password = env KB_PG_PASSWORD (required); TCP ⇒ port 5432 +
sslmode=require; host starting "/" ⇒ Cloud SQL unix socket (no port/ssl)`.
New `db/sync/_conn.py`: all five from `KB_PG_HOST/PORT/DB/USER/PASSWORD`,
`KB_PG_SSLMODE` default `prefer` (works on both no-SSL docker and Cloud SQL),
unix-socket rule preserved. **No default IP anywhere in db/.**
No roles/GRANTs exist in source — everything runs as one role that owns the DB
and can CREATE SCHEMA.

## 7. Script rename map

| db/sync (new) | source | notes |
|---|---|---|
| `_conn.py` | (new) | replaces `build_world_pg.connect` / `query14._pg` hardcoded defaults |
| `_embed.py` | `runtime20/embed16.py` | bge-small embedder + `normalize_surface` + `pgvector_literal`, verbatim minus engine imports |
| `import_dump.py` | `runtime20/build_world_pg.py` | `ensure_schema` now applies `db/init.sql`; CHUNK_DIR default = tempdir; checkpoint unchanged |
| `sync_wikidata.py` | `runtime20/build_world_wdqs.py` | imports from `import_dump` instead of `runtime20.build_world_pg` |
| `build_world.py` | `runtime14/scripts/setup_world_schema.py` + `runtime15/scripts/setup_world15.py` + `runtime16/scripts/add_city_qid.py` | merged; TRUNCATE+INSERT (init.sql owns DDL); Cities.qid selected directly instead of backfilled; STAMP = today |
| `build_words.py` | `runtime16/scripts/setup_words16.py` + `runtime16/scripts/key_words16.py` | merged; full-column table from init.sql; HNSW dropped during bulk load, rebuilt after; `requests` → shared `wdqs()` |
| `sync_types.py` | `runtime20/scripts/sync_world_types.py` | `rollup_taxonomy.lpath` + `organize_taxonomy.WD` inlined (minimal P279 walker, cache at `db/sync/data/p279_cache.json`); also sets `resolver_type` |
| `sync_entity.py` | `runtime20/scripts/sync_wikidata_world.py` | standalone; engine keeps its own copy for the runtime lazy path |
| `mirror_schema.py` | `runtime20/scripts/mirror_world_schema.py` | taxonomy.csv path → `db/sync/data/` |
| `build_wikipedia.py` | `runtime20/scripts/build_wikipedia_schema.py` | `build_review.LEAF_QID` re-derived from taxonomy.csv locally |
| `unify_words_qid.py` | `runtime20/scripts/unify_words_qid.py` | now a migration/health-check (sync_types sets resolver_type on fresh builds) |
| `data/taxonomy.csv` | `runtime20/data/taxonomy.csv` | 42 accepted leaves (copied verbatim) |
| `data/p279_cache.json` | `runtime20/data/p279_cache.json` | P279 chain cache ⇒ sync_types needs no Wikidata API on first run |
| *(not copied)* | `scripts/build20.py`, `scripts/build_from_entity.py` | training-data extraction (read `capped.*`, write CSVs); belong to training/, not db bootstrap |
| *(not copied)* | `runtime13/scripts/build_words_from_pg.py`, `runtime15/scripts/build_subdivisions.py`, `runtime16/scripts/setup_words16.py` city variants | superseded by the merged scripts above |

## 8. Open questions / risks (things only the live DB could confirm)

1. **HNSW index parameters** — the source creates `ix_words_hnsw` with no
   parameters, so init.sql standardizes on pgvector defaults (m=16,
   ef_construction=64). If the live index was ever rebuilt manually with other
   parameters, that tuning isn't in code and is lost.
2. **`knowledgebase."types"` row count drift** — unify_words_qid's docstring says 202
   nodes, sync_world_types was later rebuilt from the 42-leaf runtime20
   taxonomy.csv (~180 nodes after path expansion). Rebuilding from the copied
   taxonomy.csv reproduces the runtime20 state, not any hand-edits on the live DB.
3. **`words.props` shape** — `row_to_json` of whatever the source table looked
   like at build time. `key_words16.UPD_CITY` matches on `props->>'country'` and
   `props->>'population'`, which works only when words was built from a
   `knowledgebase."Cities"` that carried those columns; the merged `build_words.py`
   preserves this (canonical_rows reads Cities incl. country/population/is_primary).
4. **Legacy `word_*.json` configs** — the planner's world-table metadata
   (`runtime20/data/word_*.json`, key/columns/links per friendly table) lives in
   the model deploy dir, not the DB; db/ doesn't own it but the friendly-table
   columns here must stay in sync with it.
5. **`entity_label` usage** — created and populated (trigram index and all), but
   no runtime20 serving path reads it anymore (words superseded it). Kept because
   schema.sql/sync scripts still write it; could be dropped later.
6. **Live-DB-only artifacts** — anything created manually on Cloud SQL (extra
   indexes, VACUUM settings, the actual per-user schemas, the lazily-accreted
   wikipedia tables and their discovered column sets) is not reproducible from
   code; a fresh instance re-discovers wikipedia schemas from *current* Wikidata,
   so property columns may differ from the live DB's older discoveries.
7. **is_primary type mismatch** — `knowledgebase."Cities".is_primary` is `int` (0/1)
   while `knowledgebase."words".is_primary` is `boolean`; both faithful to source
   (setup_world_schema vs key_words16). Engine code handles each accordingly
   (`is_primary = 1` filters vs boolean casts); do not "clean this up" without
   touching the engine.
8. **WDQS etiquette** — sync scripts hit query.wikidata.org with a UA string;
   the full sync issues hundreds of banded queries. Reviewers on shared IPs may
   see 429s; the retry/bisect logic handles it but wall-clock time varies a lot.
