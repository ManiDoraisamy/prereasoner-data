-- ============================================================================
-- PreReasoner world database — bootstrap for a FRESH Postgres instance.
--
--   psql "$KB_PG_URL" -f db/init.sql        (or via docker exec, see db/README.md)
--
-- Creates every extension, schema, static table and index the engine expects.
-- Idempotent: safe to re-run. Population is done by the scripts in db/sync/
-- (bulk) and by the engine's lazy sync at query time (see db/README.md).
--
-- The engine connects as a single role (typically `postgres`) named by
-- KB_PG_USER; it CREATEs conversation and private-reference schemas, so the role
-- needs CREATE on the database. No other roles/grants are assumed.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 0. Extensions
--    vector  : pgvector — knowledgebase."words".embedding vector(384) + HNSW <=> search
--              (engine entity resolution and hybrid semantic ranking)
--    pg_trgm : trigram GIN index on public.entity_label for fuzzy value match
--              (legacy value matcher; kept because init creates entity_label)
--    NOTE: no PostGIS / earthdistance / cube — geo NEARBY computes
--    haversine in plain SQL (acos/radians over settlement.lat/lng).
-- ----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ----------------------------------------------------------------------------
-- 1. Schemas
--    public        : raw Wikidata import (geo hierarchy + small clean types)
--    knowledgebase : THE shared serving schema — the "words" resolution index,
--                    the "types" taxonomy, the qid-keyed faithful Wikidata tables
--                    (one per taxonomy leaf, exact Wikidata label as table name,
--                    lazily filled), and the friendly name-keyed tables + views.
--                    (Named "knowledgebase" — NOT "world" — because "world model"
--                    means a learned dynamics model in ML; this is a lookup KB.)
--    c_<32hex> conversation schemas hold uploads, selected reference copies, and
--    the two bridge tables. m_<md5(sub)> schemas hold persistent private references.
--    Both are created by the engine from authorized server-side identities.
-- ----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS knowledgebase;

-- ----------------------------------------------------------------------------
-- 2. public — raw Wikidata world model
--    Populated by db/sync/sync_wikidata.py (live WDQS, recommended) or
--    db/sync/import_dump.py (HF parquet dump, legacy).
--    Read by: db/sync/build_world.py + build_words.py (transforms into knowledgebase.*),
--    and directly by the engine's geo NEARBY path.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.continent (
  qid   text PRIMARY KEY,
  name  text NOT NULL
);

CREATE TABLE IF NOT EXISTS public.country (
  qid               text PRIMARY KEY,
  name              text NOT NULL,
  iso2              text,
  iso3              text,
  continent_qid     text,
  continent         text,            -- denormalized continent name
  capital_qid       text,
  currency_code     text,
  currency_name     text,
  population        bigint,
  area_km2          double precision,
  official_language text
);

CREATE TABLE IF NOT EXISTS public.admin (
  qid          text PRIMARY KEY,
  name         text NOT NULL,
  country_qid  text,
  country      text,                  -- denormalized country name
  parent_qid   text,
  level        text,                  -- state / province / county / ... (from P31)
  population   bigint,
  capital_qid  text
);

CREATE TABLE IF NOT EXISTS public.settlement (
  qid          text PRIMARY KEY,
  name         text NOT NULL,
  country_qid  text,
  country      text,                  -- denormalized country name
  admin_qid    text,
  admin        text,
  population   bigint,
  lat          double precision,      -- geo NEARBY reads lat/lng
  lng          double precision,
  timezone     text,
  is_capital   boolean DEFAULT false
);

CREATE TABLE IF NOT EXISTS public.currency (
  code         text PRIMARY KEY,      -- ISO 4217 (falls back to qid)
  qid          text,
  name         text NOT NULL,
  symbol       text
);

CREATE TABLE IF NOT EXISTS public.element (
  symbol         text PRIMARY KEY,
  qid            text,
  name           text NOT NULL,
  atomic_number  integer,
  mass           double precision
);

CREATE TABLE IF NOT EXISTS public.timezone (
  qid         text PRIMARY KEY,
  name        text NOT NULL,
  utc_offset  text
);

-- value-matching label index: every entity's label + aliases, tagged by table
CREATE TABLE IF NOT EXISTS public.entity_label (
  qid       text NOT NULL,
  label     text NOT NULL,
  lang      text,
  is_alias  boolean DEFAULT false,
  kind      text NOT NULL            -- settlement|country|admin|continent|currency|element|timezone
);

-- resumable import checkpoint (import_dump.py marks parquet chunks done here)
CREATE TABLE IF NOT EXISTS public.import_ckpt (
  chunk int PRIMARY KEY
);

CREATE INDEX IF NOT EXISTS ix_settlement_country  ON public.settlement(country_qid);
CREATE INDEX IF NOT EXISTS ix_settlement_namelow  ON public.settlement(lower(name));
CREATE INDEX IF NOT EXISTS ix_admin_country       ON public.admin(country_qid);
CREATE INDEX IF NOT EXISTS ix_country_namelow     ON public.country(lower(name));
CREATE INDEX IF NOT EXISTS ix_country_iso2        ON public.country(iso2);
CREATE INDEX IF NOT EXISTS ix_country_continent   ON public.country(continent);
CREATE INDEX IF NOT EXISTS ix_label_namelow       ON public.entity_label(lower(label));
CREATE INDEX IF NOT EXISTS ix_label_kind          ON public.entity_label(kind);
CREATE INDEX IF NOT EXISTS ix_label_trgm          ON public.entity_label USING gin (lower(label) gin_trgm_ops);

-- ----------------------------------------------------------------------------
-- 3. knowledgebase."words" — THE entity-resolution index (pgvector).
--    One row per SURFACE form (label or alias) -> canonical entity + qid.
--    embedding = bge-small-en-v1.5 [CLS], L2-normalized, 384-dim; cosine via <=>.
--    Populated by db/sync/build_words.py; single rows appended by the engine's
--    lazy sync (sync_entity.ensure_entity) and by sync_types.py (type='type').
--    Read by the entity resolver, cell bridges, world grounding, and lookup paths.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledgebase."words" (
  id            bigserial PRIMARY KEY,
  surface       text,                 -- what someone types ("US", "Bombay")
  canonical     text,                 -- the canonical entity label ("United States")
  type          text,                 -- city|country|state|element|continent|type|<exact wikidata label>
  props         jsonb,                -- source row (population, country, alias_of, ...)
  norm          text,                 -- normalize_surface(surface): exact-match key
  embedding     vector(384),          -- bge-small-en-v1.5, L2-normalized
  qid           text,                 -- Wikidata id (stable key; join target)
  canon_country text,                 -- entity's country, for same-name disambiguation
  is_primary    boolean               -- global most-populous-per-name flag
);

CREATE INDEX IF NOT EXISTS ix_words_type_norm  ON knowledgebase."words"(type, norm);
CREATE INDEX IF NOT EXISTS ix_words_type_qid   ON knowledgebase."words"(type, qid);
CREATE INDEX IF NOT EXISTS ix_words_city_norm  ON knowledgebase."words"(norm) WHERE type = 'city';
-- HNSW cosine index (pgvector defaults: m=16, ef_construction=64) — the source
-- created it with no explicit parameters, so defaults are the contract.
CREATE INDEX IF NOT EXISTS ix_words_hnsw       ON knowledgebase."words" USING hnsw (embedding vector_cosine_ops);

-- ----------------------------------------------------------------------------
-- 4. knowledgebase."types" — the type taxonomy DAG (one row per node, qid-keyed).
--    Populated by db/sync/sync_types.py from db/sync/data/taxonomy.csv.
--    resolver_type links legacy words.type strings ('city') to the node qid
--    (Q515) — set by db/sync/unify_words_qid.py (or sync_types.py).
--    Read by lazy entity table naming, world-QID resolution, and taxonomy routing.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledgebase."types" (
  qid           text PRIMARY KEY,
  label         text,
  parent_qid    text,
  is_leaf       boolean,
  world_table   text,                 -- Cities/Countries/Places/Continents/Elements or NULL
  depth         int,
  resolver_type text                  -- legacy words.type string this node resolves ('city' -> Q515)
);

CREATE INDEX IF NOT EXISTS ix_types_parent   ON knowledgebase."types"(parent_qid);
CREATE INDEX IF NOT EXISTS ix_types_leaf     ON knowledgebase."types"(is_leaf);
CREATE INDEX IF NOT EXISTS ix_types_resolver ON knowledgebase."types"("resolver_type");

-- ----------------------------------------------------------------------------
-- 5. Friendly world tables + "... in the World" views.
--    Denormalized copies of public.* the SQL planner references by friendly
--    name (typed planner and world-attribute enrichment). Populated by
--    db/sync/build_world.py. The views exist because generated SQL uses the
--    "<X> in the World" spelling.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledgebase."Cities" (
  name        text,
  country     text,
  population  bigint,
  is_primary  int,                    -- 1 = most populous settlement with this name
  updated_at  text,
  source      text,
  qid         text                    -- stable key for city joins
);

CREATE TABLE IF NOT EXISTS knowledgebase."Countries" (
  name           text,
  currency       text,
  currency_name  text,
  continent      text,
  valid_from     text,                -- planner supports as-of temporal filters
  valid_to       text,
  updated_at     text,
  source         text
);

CREATE TABLE IF NOT EXISTS knowledgebase."Places" (
  name        text,
  kind        text,                   -- 'city' | 'country'
  lat         double precision,
  lng         double precision,
  hemisphere  text,
  population  bigint,
  updated_at  text,
  source      text
);

CREATE TABLE IF NOT EXISTS knowledgebase."Elements" (
  name           text,
  symbol         text,
  atomic_number  int,
  mass           double precision,
  updated_at     text,
  source         text
);

CREATE TABLE IF NOT EXISTS knowledgebase."Continents" (
  name        text,
  updated_at  text,
  source      text
);

CREATE TABLE IF NOT EXISTS knowledgebase."States" (
  name        text,
  country     text,
  population  bigint,
  level       text,                   -- state / province / county / ...
  updated_at  text,
  source      text
);

-- curated alias -> canonical country name (legacy normalization table;
-- superseded by knowledgebase."words" but still created by the build scripts)
CREATE TABLE IF NOT EXISTS knowledgebase."Country Aliases" (
  alias  text,
  name   text
);

-- Daily cross-rate world table (joined like city/country: conversation + tenant + knowledgebase).
-- Populated by db/sync/build_exchange_rate.py from the ECB history (db/sync/sources/ecb); the
-- builder adds one rate_to_<code> double precision column per ECB series + EUR at build time,
-- so this seed declares only the invariant spine. One row per (ISO code, CALENDAR day);
-- updated_at keeps the true source business date for the freshness machinery.
CREATE TABLE IF NOT EXISTS knowledgebase."exchange_rate" (
  currency_code text NOT NULL,
  "date"        date NOT NULL,
  updated_at    date NOT NULL,
  source        text NOT NULL,
  source_release_id text NOT NULL,
  PRIMARY KEY (currency_code, "date")
);

-- Maintenance catalog: which world tables are actually kept up to date, how often each is expected
-- to refresh, and when it last did. Rows are declared by db/sync/schedule.py (the ONE writer) and
-- read at serving time by the freshness guard, so a table with no per-row updated_at can still be
-- judged stale instead of silently passing. The "... in the World" VIEWS below and the lazy-fill
-- entity tables are deliberately excluded — see that module's docstring.
CREATE TABLE IF NOT EXISTS knowledgebase."schedule" (
  table_name        text PRIMARY KEY,
  source            text NOT NULL,
  source_schema     text,
  cadence_hours     integer,
  note              text,
  last_refreshed_at timestamptz,
  last_release_id   text,
  row_count         bigint,
  recorded_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT schedule_cadence_positive CHECK (cadence_hours IS NULL OR cadence_hours > 0)
);

CREATE INDEX IF NOT EXISTS ix_world_cities_lname     ON knowledgebase."Cities"(lower(name));
CREATE INDEX IF NOT EXISTS ix_cities_qid             ON knowledgebase."Cities"(qid);
CREATE INDEX IF NOT EXISTS ix_world_countries_lname  ON knowledgebase."Countries"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_places_lname     ON knowledgebase."Places"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_elements_lname   ON knowledgebase."Elements"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_elements_lsym    ON knowledgebase."Elements"(lower(symbol));
CREATE INDEX IF NOT EXISTS ix_world_continents_lname ON knowledgebase."Continents"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_states_lname     ON knowledgebase."States"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_calias           ON knowledgebase."Country Aliases"(alias);

CREATE OR REPLACE VIEW knowledgebase."Cities in the World"     AS SELECT * FROM knowledgebase."Cities";
CREATE OR REPLACE VIEW knowledgebase."Countries in the World"  AS SELECT * FROM knowledgebase."Countries";
CREATE OR REPLACE VIEW knowledgebase."Places in the World"     AS SELECT * FROM knowledgebase."Places";
CREATE OR REPLACE VIEW knowledgebase."Elements in the World"   AS SELECT * FROM knowledgebase."Elements";
CREATE OR REPLACE VIEW knowledgebase."Continents in the World" AS SELECT * FROM knowledgebase."Continents";
CREATE OR REPLACE VIEW knowledgebase."States in the World"     AS SELECT * FROM knowledgebase."States";

-- ----------------------------------------------------------------------------
-- 6. Qid-keyed faithful Wikidata tables. INTENTIONALLY EMPTY HERE.
--    Tables are named by the EXACT Wikidata type label (e.g. knowledgebase."city",
--    knowledgebase."hospital") with columns = the type's discovered Wikidata
--    properties (all text) + qid PRIMARY KEY + name. They are created:
--      * lazily at query time by the engine (sync_entity.ensure_table via
--        ensure_entity/lazy_resolve — the normal path), or
--      * up front by db/sync/build_wikipedia.py / mirror_schema.py (optional).
--    Rows are lazily fetched one entity at a time from Wikidata when a CSV
--    cell resolves to a qid that is not in the table yet.
-- ----------------------------------------------------------------------------

-- ----------------------------------------------------------------------------
-- 7. Conversation and private-reference schemas (created by the engine):
--    c_<32hex> is ownership-checked before use and contains:
--      "<conversation>"."<upload-or-selected-reference>" -- typed request rows
--      "<conversation>"."<t> connected to wikipedia"     -- resolved FKs:
--          ("column" text, "value" text, "world_type" text,
--           "world_key" text, "country" text, "world_qid" text)
--      "<conversation>"."<t> unconnected to wikipedia"   -- free-text vectors:
--          ("__pk" bigint, "column" text, "value" text,
--           "embedding" vector(896))                  -- unified-encoder dim
--    Queries run with:
--      SET search_path TO "<conversation>", knowledgebase, public
--    m_<md5(verified-sub)> contains persistent private reference dimensions;
--    its first column is the validated primary key. Relevant tables are copied
--    into the request table set rather than adding this schema to search_path.
-- ----------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- chat: conversation identity + ownership (engine/conversations.py).
-- The working Postgres schema for a run is the CONVERSATION id (self-contained,
-- archivable). Authorization is by the verified user via user_conversation — a
-- client cannot use a conversation id it does not own (no IDOR).
-- Request handling performs DML only. Apply db.sync.app_migrations as the privileged
-- admin before a non-superuser serving role is enabled.
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS "chat";

CREATE TABLE IF NOT EXISTS "chat"."schema_migration" (
  version integer PRIMARY KEY,
  name text NOT NULL UNIQUE,
  checksum text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "chat"."user_profile" (
  user_id     text PRIMARY KEY,                    -- the verified Google sub (stable across devices)
  created_at  timestamptz NOT NULL DEFAULT now(),
  last_seen   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "chat"."conversation" (
  conversation_id text PRIMARY KEY,                -- also the name of this conversation's data schema (c_<32 hex>)
  initial_prompt  text,                            -- the opening question (drawer label)
  tables          jsonb,                           -- the uploaded CSVs [{name,data}] so a conversation re-opens self-contained
  state           jsonb,                           -- renderable client snapshot
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "chat"."user_conversation" (
  user_id         text NOT NULL REFERENCES "chat"."user_profile"(user_id),
  conversation_id text NOT NULL REFERENCES "chat"."conversation"(conversation_id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS ix_user_conv ON "chat"."user_conversation" (user_id, created_at DESC);
