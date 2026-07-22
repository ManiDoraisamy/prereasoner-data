# db/ — the PreReasoner world database

Everything needed to bootstrap the Postgres database (`world`) the engine serves
against, on a **fresh instance** — local Docker or Cloud SQL.

The database holds four kinds of state:

| Schema | Contents | Created by | Filled by |
|---|---|---|---|
| `public` | raw Wikidata geo/type import (`settlement`, `country`, `admin`, `continent`, `currency`, `element`, `timezone`, `entity_label`) | `init.sql` | `sync/sync_wikidata.py` (bulk) |
| `world` | serving tables: `"words"` (pgvector entity-resolution index, HNSW), `"types"` (type taxonomy), friendly tables `"Cities"`/`"Countries"`/`"Places"`/`"Elements"`/`"Continents"`/`"States"` + `"... in the World"` views | `init.sql` | `sync/build_world.py`, `sync/build_words.py`, `sync/sync_types.py` |
| `wikipedia` | qid-keyed faithful Wikidata tables, one per type, named by the **exact Wikidata label** (`knowledgebase."city"`, `knowledgebase."hospital"`, ...) | lazily by the engine (or `sync/mirror_schema.py` + `sync/build_wikipedia.py` up front) | **lazily at query time**, one entity per miss |
| `"<google-sub>"` | per-user schemas: uploaded CSV tables + the bridge tables `"<t> connected to wikipedia"` / `"<t> unconnected to wikipedia"` | **by the engine at request time** | by the engine |

Connection config is env-var only (no hardcoded hosts): `KB_PG_HOST`, `KB_PG_PORT`,
`KB_PG_DB`, `KB_PG_USER`, `KB_PG_PASSWORD` (+ optional `KB_PG_SSLMODE`,
default `prefer`). See `sync/_conn.py`. The engine's role needs `CREATE` on the
database (it creates per-user schemas); the scripts assume the default `postgres` role.

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
- per-user schemas + upload tables + bridge tables — created per request.

**Must be pre-synced (the engine cannot answer without them):**

- `knowledgebase."words"` — the pgvector resolution index. *Every* entity resolution
  ("cities in US", value-membership column routing, the cell bridge) does an exact
  `norm` match and/or an HNSW `<=>` search here. Empty index ⇒ nothing resolves,
  and even the lazy path is gated on words/types lookups.
- `knowledgebase."types"` — the taxonomy; the lazy sync derives the `wikipedia` table
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
`wikipedia` fill for anything else.

### Full sync (complete settlement long tail + aliases; several hours of WDQS)

```bash
python db/sync/sync_wikidata.py --reset              # settlements down to pop>=1000 (~174k rows)
python db/sync/build_world.py
python db/sync/build_words.py --cities --city-aliases   # ~213k word rows incl. Wikidata aliases ("Bombay"->Mumbai)
python db/sync/sync_types.py
python db/sync/mirror_schema.py                      # optional: pre-create knowledgebase."<leaf>" mirror schemas
python db/sync/build_wikipedia.py                    # optional: pre-create empty qid-PK wikipedia tables
```

`import_dump.py` is a legacy alternative bulk import from the
`philippesaade/wikidata` HF parquet dump (resumable, `CHUNK_DIR` env controls the
chunk cache dir) — the WDQS path above has better city coverage.

### Per-type / single-entity sync

```bash
python db/sync/sync_entity.py --qid Q6256 --label country --max 1000   # bulk one type
python db/sync/sync_entity.py --qid Q515 --label city --lazy "Kyoto"   # one entity, like the engine does
```

## 4. Storage expectations (rough)

| State | Estimate |
|---|---|
| minimal seed (`--high-only`, words `--cities`) | ~10k settlements, ~40–60k word rows ⇒ **well under 1 GB** incl. HNSW |
| full sync | ~174k settlements, ~213k word rows (384-dim vectors ≈ 1.5 KB each) ⇒ words table + HNSW index ≈ 1 GB; **~2–3 GB total** |
| lazy growth | one `wikipedia` row + one `words` row per newly-seen entity; per-user schemas grow with uploads (896-dim vectors in the unconnected bridge) |

A 20 GB disk is comfortable.
