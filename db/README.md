# Database Bootstrap

> **Current naming debt:** the running code still stores Wikidata staging in PostgreSQL
> `public` and Wikidata serving projections in `knowledgebase`. These are legacy names, not
> the target open-source contract. New enrichment work must follow
> [`docs/KNOWLEDGE_ENRICHMENT_ROADMAP.md`](../docs/KNOWLEDGE_ENRICHMENT_ROADMAP.md):
> `wikidata` for Wikidata-owned data and publisher schemas listed in
> [`docs/SOURCE_DATA.md`](../docs/SOURCE_DATA.md) for new
> synchronized references. Domain meaning belongs in registry metadata, not duplicate
> `geo`/`finance` schemas, and `public` contains no application tables. Until the coordinated
> migration lands, the remainder of this file documents the
> schema names the current executable actually uses.

Everything needed to bootstrap the Postgres database (`world`) the engine serves
against, on a **fresh instance** — local Docker or Cloud SQL.

The database holds source, application, and tenant schema families:

| Schema | Contents | Created and filled by |
|---|---|---|
| `public` | raw Wikidata geo/type import (`settlement`, `country`, `admin`, `continent`, `currency`, `element`, `timezone`, `entity_label`) | `init.sql`, then `sync/sync_wikidata.py` |
| `knowledgebase` | Resolution index, taxonomy, friendly world tables/views, and QID-keyed faithful Wikidata tables named by exact type label | `init.sql` plus Wikidata sync scripts; entity rows also fill lazily |
| `iana` | Pinned IANA country codes, canonical zones, aliases, representative locations, and country-zone mappings | `python -m db.sync.sources.iana.sync` |
| `cldr` | Pinned CLDR territory/currency code data, localized names/symbols, temporal currency usage, and unit metadata | `python -m db.sync.sources.cldr.sync` |
| `google_libphonenumber` | Numbering-region patterns and formatting metadata | `python -m db.sync.sources.google_libphonenumber.sync` |
| `geonames` | Worldwide postal rows and the scoped `cities5000` place extract | `python -m db.sync.sources.geonames.sync` |
| `ecb` | Historical EUR reference exchange rates | `python -m db.sync.sources.ecb.sync` |
| `ec_tedb` | Dated EU VAT responses with category and CN/CPA codes | `python -m db.sync.sources.ec_tedb.sync` |
| `nager_date` | Bounded community public-holiday snapshots | `python -m db.sync.sources.nager_date.sync` |
| `cdc` | Effective CDC/NCHS ICD-10-CM tabular hierarchy | `python -m db.sync.sources.cdc.sync` |
| `nlm_cde` | Public NIH/NLM CDEs, forms, assessment structure, and rights flags | `python -m db.sync.sources.nlm_cde.sync` |
| `chat` | Conversation metadata and verified user-to-conversation ownership | `init.sql` and `engine/conversations.py` |
| `c_<32hex>` | One authorized conversation's uploads, selected private-reference copies, and world bridges | engine request path |
| `m_<md5(sub)>` | One verified user's persistent private reference dimensions | `engine/master.py` |

Connection config is env-var only (no hardcoded hosts). Serving uses `KB_PG_*`. Sync,
migration, release, and grant commands prefer `SYNC_PG_*` and fall back to `KB_PG_*` for
local development. In production the engine must use a non-superuser role; privileged sync
credentials belong only on offline jobs. See `sync/_conn.py`. The engine role still needs
the narrowly scoped database/schema privileges required to create conversation and private-reference schemas.

**Extensions required: `vector` (pgvector) and `pg_trgm`.** Nothing else — geo
"NEARBY" queries compute haversine distance in plain SQL over `settlement.lat/lng`,
so **PostGIS is NOT needed**.

## 1. Bootstrap a fresh Postgres

### a) Local Docker

The stock [`pgvector/pgvector`](https://hub.docker.com/r/pgvector/pgvector) image has
everything (`pg_trgm` ships in contrib):

```bash
docker run -d --name prereasoner-pg \
  -e POSTGRES_PASSWORD=devpassword -e POSTGRES_DB=world \
  -p 5432:5432 -v prereasoner-pg:/var/lib/postgresql/data \
  pgvector/pgvector:pg16

# apply the schema (idempotent)
docker exec -i prereasoner-pg psql -U postgres -d world < db/init.sql

export KB_PG_HOST=localhost KB_PG_PORT=5432 KB_PG_DB=world \
       KB_PG_USER=postgres KB_PG_PASSWORD=devpassword
```

### b) Cloud SQL (GCP)

Cloud SQL for PostgreSQL supports both extensions (`vector` since PG 15+ images,
`pg_trgm` always; PostGIS is also available but unused):

```bash
gcloud sql instances create prereasoner-world \
  --database-version=POSTGRES_16 --tier=db-custom-2-8192 \
  --region=us-central1 --storage-size=20GB --storage-auto-increase
gcloud sql users set-password postgres --instance=prereasoner-world --password="$PW"
gcloud sql databases create world --instance=prereasoner-world

# connect (public IP + SSL, or Cloud SQL Auth Proxy) and apply the schema
psql "host=<INSTANCE_IP> dbname=world user=postgres sslmode=require" -f db/init.sql

export KB_PG_HOST=<INSTANCE_IP> KB_PG_SSLMODE=require KB_PG_PASSWORD="$PW"
# On Cloud Run, use the unix socket instead: KB_PG_HOST=/cloudsql/<PROJECT>:<REGION>:prereasoner-world
```

`CREATE EXTENSION vector / pg_trgm` in `init.sql` works as the `postgres` user on
Cloud SQL (it is granted `cloudsqlsuperuser`).

## 2. What a fresh deployment gets automatically (lazy) vs what must be pre-synced

**Lazy (automatic, nothing to run):**

- `knowledgebase."<type>"` tables — created **on demand** by the engine
  (`ensure_entity`/`ensure_table` in the lazy sync) and filled **one entity at a
  time** from live Wikidata whenever an uploaded CSV cell resolves to a qid that
  isn't stored yet. This covers the entire non-geo world (hospitals, software,
  films, ...) and the qid-keyed city/country joins.
- conversation schemas, upload/selected-reference tables, and bridge tables — created per request.
- per-user master schemas and tables — created when authenticated users save references.

**Must be pre-synced (the engine cannot answer without them):**

- `knowledgebase."words"` — the pgvector resolution index. *Every* entity resolution
  ("cities in US", value-membership column routing, the cell bridge) does an exact
  `norm` match and/or an HNSW `<=>` search here. Empty index ⇒ nothing resolves,
  and even the lazy path is gated on words/types lookups.
- `knowledgebase."types"` — the taxonomy; the lazy sync derives the qid-keyed table
  name for a type from `types.label`, and qid taxonomy walks read it.
- `public.settlement` (with lat/lng) — the geo NEARBY primitive
  ("big cities near Paris") reads it directly.
- `knowledgebase."Cities"/"Countries"/...` — the planner's friendly world tables/views.

## 3. Sync workflow

Install script deps first: `pip install -r db/sync/requirements.txt`
(bge-small-en-v1.5, ~130MB, downloads on first embed; CPU is fine).

### Minimal seed (enough for the demo questions; ~15–45 min, mostly WDQS)

```bash
psql ... -f db/init.sql                              # schema (idempotent)
python db/sync/sync_wikidata.py --reset --high-only  # countries/currencies/elements + cities pop>=100k
python db/sync/build_world.py                        # friendly world tables from public.*
python db/sync/build_words.py --cities               # the pgvector words index (+HNSW)
python db/sync/sync_types.py                         # taxonomy -> knowledgebase."types" + type words
python db/sync/unify_words_qid.py                    # verify the qid walk (optional health check)
```

That makes the demo paths work: entity resolution ("US"→United States), world joins
(city→country→continent), aggregates, geo NEARBY (for cities ≥100k), and the lazy
qid-keyed entity fill for anything else.

### Full sync (complete settlement long tail + aliases; several hours of WDQS)

```bash
python db/sync/sync_wikidata.py --reset              # settlements down to pop>=1000 (~174k rows)
python db/sync/build_world.py
python db/sync/build_words.py --cities --city-aliases   # ~213k word rows incl. Wikidata aliases ("Bombay"->Mumbai)
python db/sync/sync_types.py
python db/sync/mirror_schema.py                      # optional: pre-create knowledgebase."<leaf>" mirror schemas
python db/sync/build_wikipedia.py                    # optional: pre-create empty qid-PK entity tables
```

`import_dump.py` is a legacy alternative bulk import from the
`philippesaade/wikidata` HF parquet dump (resumable, `CHUNK_DIR` env controls the
chunk cache dir). The importer pins an immutable dataset revision; the WDQS path above has
better city coverage.

### Per-type / single-entity sync

```bash
python db/sync/sync_entity.py --qid Q6256 --label country --max 1000   # bulk one type
python db/sync/sync_entity.py --qid Q515 --label city --lazy "Kyoto"   # one entity, like the engine does
```

### Source-owned reference sync

These synchronizers create their schema and tables only when invoked. They download a
pinned release, validate the complete declared input, insert an immutable content-addressed
release, verify table counts, and activate it in one transaction:

```bash
python -m db.sync.sources.iana.sync --dry-run
python -m db.sync.sources.iana.sync

python -m db.sync.sources.cldr.sync --dry-run
python -m db.sync.sources.cldr.sync
python -m db.sync.sources.google_libphonenumber.sync
python -m db.sync.sources.geonames.sync
python -m db.sync.sources.ecb.sync
python -m db.sync.build_exchange_rate       # project the active ECB release into knowledgebase.exchange_rate
python -m db.sync.sources.ec_tedb.sync --situation-on 2026-08-17
python -m db.sync.sources.nager_date.sync --year-start 2025 --year-end 2027
python -m db.sync.sources.cdc.sync
python -m db.sync.sources.nlm_cde.sync --version 2026-08-17

# Upgrade source schemas that already exist; no source download is performed.
python -m db.sync.migrations

# Apply application-schema migrations as the privileged admin, before serving flips roles.
python -m db.sync.app_migrations

# Grant chat DML to an existing non-superuser role; add --datasets when activating references.
python -m db.reference_grants --role prereasoner_runtime

# Atomically reactivate a previously validated immutable release.
python -m db.sync.releases --schema iana --release-id <retired-release-id>
```

Use `--archive <path>` to validate and load an already-downloaded release. Rerunning the
same active content is idempotent. The IANA sync fully materializes `iso3166.tab`,
`zone1970.tab`, and default-release `Zone`/`Link` records into 5 data tables; it deliberately
does not claim compiled timezone transitions. The CLDR sync fully materializes its declared
territory, currency, unit, and localized territory/currency display structures into 14 data
tables across every locale file; it does not mirror unrelated CLDR calendars, collation,
annotations, or numbering data.

The ECB synchronizer owns the immutable source history in `ecb.exchange_rate`; the projection
builder is a separate idempotent step because `knowledgebase."exchange_rate"` is also created
by `db/init.sql`. It adds missing `rate_to_<currency>` columns before each rebuild, so a fresh
bootstrap and a rerun use the same migration-safe path.

Source schema migrations are checksummed in each source's `schema_migration` table and run
independently of source downloads. The runner skips absent source schemas, takes a
transaction-scoped advisory lock, and rolls back a source migration on failure. A
synchronizer refuses to reuse an active release whose materialized schema version is older
than the version required by its checked-in source contract.

See [`docs/SOURCE_DATA.md`](../docs/SOURCE_DATA.md) for exact table counts, source scope,
licenses, quality limits, and the credential-gated WHO/LOINC commands. Run
`python -m tests.test_source_sync` for the hermetic parser/validation suite. These tables
are materialized foundations and do not become planner-visible merely because their physical
release is active. `iana_country` is code-approved, but it is selected only when the serving
deployment explicitly sets `ENRICHMENT_ACTIVE_DATASETS=iana_country`; the default is empty.

## 4. Storage expectations (rough)

| State | Estimate |
|---|---|
| minimal seed (`--high-only`, words `--cities`) | ~10k settlements, ~40–60k word rows ⇒ **well under 1 GB** incl. HNSW |
| full sync | ~174k settlements, ~213k word rows (384-dim vectors ≈ 1.5 KB each) ⇒ words table + HNSW index ≈ 1 GB; **~2–3 GB total** |
| nine active publisher-source schemas (2026-08-17) | about **985 MB** total: GeoNames 650 MB, NLM CDE 175 MB, ECB 75 MB, CLDR 61 MB, and smaller sources |
| lazy growth | one qid-keyed entity row + one `words` row per newly seen entity; conversation bridges and private reference schemas grow with use |

The verified Cloud SQL database was 3,229 MB after these imports. A 20 GB disk remains
comfortable for the active set, but a future full Open Food Facts or GLEIF import requires a
separate measured storage budget.
