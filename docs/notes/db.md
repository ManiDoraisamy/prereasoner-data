# db/ — the database contract

> Supplementary implementation notes for the legacy world layout. Use
> [../../db/README.md](../../db/README.md) for bootstrap and sync commands,
> [../SOURCE_DATA.md](../SOURCE_DATA.md) for publisher releases, and
> [../ARCHITECTURE.md](../ARCHITECTURE.md) for the current-versus-target storage boundary.

What the engine expects in Postgres: the extensions, schemas, static tables and
indexes created by `db/init.sql`, and the objects that the `db/sync/` pipeline
(bulk) and the engine (lazy, at query time) populate on top of it. This note
records the contract precisely — column types, index parameters, and the split
between what is pre-synced vs lazily filled — so a fresh instance can be
reproduced faithfully.

## 1. Extensions

| Extension | Why | Where used |
|---|---|---|
| `vector` (pgvector) | `knowledgebase."words".embedding vector(384)` + HNSW `<=>` cosine search; per-conversation `"<t> unconnected to wikipedia".embedding vector(896)` | `entities._nn` / `_cell_bridge_sql` (LATERAL `<=>`); `knowledge_query._persist_main_unconn` (`vector(hdim)`, hdim=896 for the unified Qwen encoder) |
| `pg_trgm` | GIN trigram index on `public.entity_label(lower(label))` | `init.sql` `ix_label_trgm` (legacy value matcher) |
| ~~postgis / earthdistance / cube~~ | **NOT used.** Geo NEARBY computes haversine in plain SQL (`acos/radians/cos/sin` over `public.settlement.lat/lng`) | grep for `earthdistance|postgis|ll_to_earth|cube` = zero code hits |

## 2. Schemas

- `public` — raw Wikidata import (the geo hierarchy + small clean types, created
  unqualified ⇒ default search_path ⇒ public). Read directly by the engine's geo
  NEARBY (`public.settlement`) and by `db/sync/build_words.py`/`build_world.py`
  backfills.
- `knowledgebase` — THE shared serving schema (named `knowledgebase`, **not** `world`,
  because "world model" means a learned dynamics model in ML — this is a lookup KB).
  Holds `"words"` (resolution index), `"types"` (taxonomy), the qid-keyed faithful
  Wikidata tables (exact-Wikidata-label names — `"city"`/`"country"` rebuilt offline by
  `db/sync/build_qid_world.py`, the long tail pre-created by `db/sync/build_wikipedia.py`;
  serving never creates or fills them),
  AND the friendly name-keyed tables + `"... in the World"` views
  (`"Cities"`/`"Countries"`/`"Elements"`/… read by `engine/knowledge_compose`).
- `chat` — conversation identity + ownership (`user_profile` / `conversation` /
  `user_conversation`; `engine/conversations.py`, also in `init.sql`). The working
  Postgres schema for a run is the conversation id (`c_<32 hex>`); authorization is
  by verified user via `user_conversation` (no IDOR).
- `c_<32hex>` — one authorized conversation's uploaded tables, selected reference
  copies, and world-resolution bridges. Queries run with
  `SET search_path TO "<conversation>", knowledgebase, public`.
- `m_<md5(sub)>` — persistent private reference dimensions for one verified user.
  It is never added wholesale to `search_path`; `engine.master.relevant_tables`
  selects bounded FK-connected tables and materializes them into the request table set.

## 3. Static tables (created by `db/init.sql`)

### public — raw Wikidata world model
`continent(qid PK, name)`, `country(qid PK, name, iso2, iso3, continent_qid,
continent, capital_qid, currency_code, currency_name, population bigint,
area_km2 float8, official_language)`, `admin(qid PK, name, country_qid, country,
parent_qid, level, population, capital_qid)`, `settlement(qid PK, name,
country_qid, country, admin_qid, admin, population, lat, lng, timezone,
is_capital bool)`, `currency(code PK, qid, name, symbol)`, `element(symbol PK,
qid, name, atomic_number, mass)`, `timezone(qid PK, name, utc_offset)`,
`entity_label(qid, label, lang, is_alias, kind)`, `import_ckpt(chunk PK)`
(resumable-import checkpoint; `db/sync/import_dump.py` marks parquet chunks done here).
Indexes exactly as in `init.sql` (incl. the `gin_trgm_ops` one).

### knowledgebase."words" — the resolution index
`(id bigserial PK, surface text, canonical text, type text, props jsonb, norm
text, embedding vector(384), qid text, canon_country text, is_primary boolean)`.
One row per SURFACE form (label or alias) → canonical entity + qid; the embedding
is bge-small-en-v1.5 [CLS], L2-normalized, 384-dim (cosine via `<=>`). Populated
by `db/sync/build_words.py`; single rows appended by the engine's lazy sync
(`sync_entity.ensure_entity`) and by `sync_types.py` (`type='type'`). Indexes:
- `ix_words_type_norm (type, norm)` — exact normalized match
- `ix_words_hnsw USING hnsw (embedding vector_cosine_ops)` — **created with no
  explicit parameters ⇒ pgvector defaults m=16, ef_construction=64 are the
  contract**
- `ix_words_type_qid (type, qid)`, `ix_words_city_norm (norm) WHERE type='city'`

`words.type` values: `city|country|state|element|continent` (legacy resolver
strings), `type` (taxonomy labels, from `sync_types.py`), and the **exact
Wikidata label** for lazily-synced non-geo entities (`ensure_entity` inserts
`type=<knowledgebase."types".label>`, e.g. `academic journal` — note: NOT snake_cased).

### knowledgebase."types" — taxonomy DAG
`(qid PK, label, parent_qid, is_leaf bool, world_table text, depth int,
resolver_type text)`. Populated by `db/sync/sync_types.py` from
`db/sync/data/taxonomy.csv`; `resolver_type` links legacy `words.type` strings to
the node qid ('city'→Q515, 'country'→Q6256, 'state'→Q35657), set by `sync_types.py`
(or the `unify_words_qid.py` migration/health-check). Indexes on parent_qid,
is_leaf, resolver_type.

### world friendly tables (+ `"... in the World"` views)
Denormalized copies of `public.*` the SQL planner references by friendly name;
populated by `db/sync/build_world.py`. The views exist because generated SQL uses
the `"<X> in the World"` spelling.
- `"Cities"(name, country, population bigint, is_primary int, updated_at, source, qid)`
- `"Countries"(name, currency, currency_name, continent, valid_from, valid_to, updated_at, source)`
- `"Places"(name, kind, lat, lng, hemisphere, population, updated_at, source)`
- `"Elements"(name, symbol, atomic_number int, mass float8, updated_at, source)`
- `"Continents"(name, updated_at, source)`
- `"States"(name, country, population bigint, level, updated_at, source)`
- `"Country Aliases"(alias, name)` — legacy, superseded by words but still built
- views `"Cities in the World"` … `"States in the World"`; lower(name) indexes.
Consumers: the planner's world joins (`engine/knowledge_tables` + `resolve_base`
`words[wt]` configs), `engine/knowledge_compose` (joins `Cities` by **qid**, others
by name).

## 4. Dynamically / lazily created objects

| Object | Naming | Creator code path |
|---|---|---|
| `knowledgebase."<exact Wikidata label>"` | e.g. `knowledgebase."city"`, `knowledgebase."hospital"`, `knowledgebase."academic journal"` — label from `knowledgebase."types".label`, truncated to 63 chars; shared labels suffixed `" (Qxxx)"` by `build_wikipedia.py` | offline sync only: `db/sync/build_qid_world.py` rebuilds `"city"`/`"country"`; `db/sync/build_wikipedia.py` (qid-PK copies) or `mirror_schema.py` pre-create the long tail. Serving reads and abstains on a miss |
| rows in the qid-keyed tables | one row per entity, qid PK, all-TEXT property columns, item-valued props store the related entity's **qid** (FK) | offline sync (`build_qid_world.py` from `public.settlement`/`public.country`; per-type syncs for the long tail); serving never inserts rows or `"words"` entries |
| `c_<32hex>` schema | authorized conversation id | `engine.conversations.resolve_conversation` + `engine.pg._load_user_schema` |
| `c_<32hex>."<upload-or-selected-reference>"` | typed request table | `engine.pg._load_user_schema` (DROP + CREATE on each serve; INTEGER→BIGINT, REAL→exact NUMERIC(58,20)) |
| `c_<32hex>."<t> connected to wikipedia"` | `('column' text, value text, world_type text, world_key text, country text, world_qid text)` | `engine.knowledge_query._persist_connected` |
| `c_<32hex>."<t> unconnected to wikipedia"` | `(__pk bigint, 'column' text, value text, embedding vector(896))` | `engine.knowledge_query._persist_main_unconn` |
| `m_<md5(sub)>."<reference>"` | all-text dimension; first column is a primary/unique key | `engine.master.save_master` |

## 5. Lazy-fill vs pre-sync (confirmed from code)

Lazy at query time (never needs pre-sync): the qid-keyed Wikidata tables AND
rows (`ensure_entity` creates the table + fetches one entity from Wikidata per
miss; the `entities` cell bridge lazily syncs resolved city qids + their countries
so 2-hop joins hit). Per-user schemas/bridges.

Pre-sync required: `knowledgebase."words"` (all resolution paths gate on it — an empty
index resolves nothing, and even `_resolve_world_qid`'s lazy branch fires only
after words/types lookups), `knowledgebase."types"` (lazy table naming reads
`types.label`), `public.settlement` (NEARBY reads it directly),
`knowledgebase."Cities"/"Countries"/…` (planner + `knowledge_compose` read them).

## 6. Connection contract

`db/sync/_conn.py` reads all five connection params from
`KB_PG_HOST/PORT/DB/USER/PASSWORD` (`KB_PG_PASSWORD` required), with
`KB_PG_SSLMODE` default `prefer` (works on both no-SSL docker and Cloud SQL). A
`KB_PG_HOST` that starts with `/` is treated as a Cloud SQL unix socket (no
port/ssl). The logical database is named `world` (the Cloud SQL database name;
`KB_PG_DB` default). **No default IP anywhere in db/.** No roles/GRANTs are
assumed — everything runs as one role that owns the DB and can CREATE SCHEMA.

## 7. db/sync scripts

The bulk-population pipeline. Run order and per-script detail are in
`db/README.md`; the essentials:

| script | what it does |
|---|---|
| `_conn.py` | the ONE Postgres connection helper (env-driven; see §6) |
| `_embed.py` | bge-small embedder + `normalize_surface` + `pgvector_literal` (shared by the sync scripts) |
| `import_dump.py` | imports the raw Wikidata world into `public.*` from the HF parquet dump (legacy); `ensure_schema` applies `db/init.sql`; resumable via `import_ckpt` |
| `sync_wikidata.py` | imports the raw Wikidata world into `public.*` from live WDQS (recommended); `--reset` applies `db/init.sql` first |
| `build_world.py` | transforms `public.*` → the friendly `knowledgebase."Cities"/"Countries"/…` tables (TRUNCATE+INSERT; `init.sql` owns the DDL) |
| `build_words.py` | builds the `knowledgebase."words"` resolution index (HNSW dropped during bulk load, rebuilt after) |
| `build_u_s_state.py` | builds the aggregate qid-keyed `knowledgebase."u_s_state"` from `"States"` + `words` (no WDQS calls; see docs/notes/naming.md) |
| `sync_types.py` | builds `knowledgebase."types"` from `db/sync/data/taxonomy.csv` (minimal P279 walker, cache at `db/sync/data/p279_cache.json`); sets `resolver_type` |
| `sync_entity.py` | standalone Wikidata entity sync (the engine keeps its own copy for the runtime lazy path) |
| `mirror_schema.py` / `build_wikipedia.py` | optionally pre-create the qid-keyed Wikidata tables up front (otherwise created lazily at query time) |
| `unify_words_qid.py` | migration/health-check (sets `resolver_type`; `sync_types.py` already does this on fresh builds) |
| `archive_conversation.py` | serializes/restores a conversation's data schema to/from GCS (pg_dump→gzip→GCS) |
| `data/taxonomy.csv` | 42 accepted taxonomy leaves |
| `data/p279_cache.json` | P279 chain cache ⇒ `sync_types.py` needs no Wikidata API on the first run |

Training-data extraction (reads training-only tables, writes CSVs) is **not** part
of db bootstrap — it lives in `training/` (see docs/notes/training.md).

## 8. Notes / risks

1. **HNSW index parameters** — `init.sql` creates `ix_words_hnsw` with no explicit
   parameters, standardizing on pgvector defaults (m=16, ef_construction=64). If a
   live index is ever rebuilt manually with other parameters, that tuning is not in
   code.
2. **`words.props` shape** — a JSON snapshot of the source row at build time.
   `build_words.py` populates it from `knowledgebase."Cities"` including
   country/population/is_primary, so `props->>'country'` / `props->>'population'`
   lookups resolve.
3. **Legacy `word_*.json` configs** — the planner's world-table metadata
   (`engine/data/word_*.json`, key/columns/links per friendly table) ships with the
   engine, not in the DB; db/ doesn't own it, but the friendly-table columns here
   must stay in sync with it.
4. **`entity_label` usage** — created and populated (trigram index and all), but no
   serving path reads it anymore (`words` superseded it). Kept because the sync
   scripts still write it; could be dropped later.
5. **Live-DB-only artifacts** — anything created manually on Cloud SQL (extra
   indexes, VACUUM settings, the actual conversation/master schemas, the lazily-accreted
   qid-keyed Wikidata tables and their discovered column sets) is not reproducible
   from code; a fresh instance re-discovers those schemas from *current* Wikidata,
   so property columns may differ from an older live DB's discoveries.
6. **is_primary type mismatch** — `knowledgebase."Cities".is_primary` is `int` (0/1)
   while `knowledgebase."words".is_primary` is `boolean` (both intentional). Engine
   code handles each accordingly (`is_primary = 1` filters vs boolean casts); do not
   "clean this up" without touching the engine.
7. **WDQS etiquette** — the sync scripts hit query.wikidata.org with a UA string;
   a full sync issues hundreds of banded queries. On shared IPs you may see 429s;
   the retry/bisect logic handles it but wall-clock time varies a lot.
