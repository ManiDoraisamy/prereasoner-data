-- ============================================================================
-- PreReasoner world database — bootstrap for a FRESH Postgres instance.
--
--   psql "$WORLD_PG_URL" -f db/init.sql        (or via docker exec, see db/README.md)
--
-- Creates every extension, schema, static table and index the engine expects.
-- Idempotent: safe to re-run. Population is done by the scripts in db/sync/
-- (bulk) and by the engine's lazy sync at query time (see db/README.md).
--
-- The engine connects as a single role (typically `postgres`) named by
-- WORLD_PG_USER; it CREATEs per-user schemas at request time, so the role
-- needs CREATE on the database. No other roles/grants are assumed.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 0. Extensions
--    vector  : pgvector — world."words".embedding vector(384) + HNSW <=> search
--              (engine: query16 entity resolution, world17 hybrid semantic rank)
--    pg_trgm : trigram GIN index on public.entity_label for fuzzy value match
--              (legacy value matcher; kept because init creates entity_label)
--    NOTE: no PostGIS / earthdistance / cube — geo NEARBY (world19) computes
--    haversine in plain SQL (acos/radians over settlement.lat/lng).
-- ----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ----------------------------------------------------------------------------
-- 1. Schemas
--    public    : raw Wikidata import (geo hierarchy + small clean types)
--    world     : shared serving schema — words index, type taxonomy, friendly
--                world tables + "... in the World" views
--    wikipedia : qid-keyed faithful Wikidata tables (one per taxonomy leaf,
--                exact Wikidata label as table name; lazily filled)
--    "<google-sub>" per-user schemas are created BY THE ENGINE at request time
--    (query14._load_user_schema): uploads + the two bridge tables
--    "<t> connected to wikipedia" / "<t> unconnected to wikipedia".
-- ----------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS world;
CREATE SCHEMA IF NOT EXISTS wikipedia;

-- ----------------------------------------------------------------------------
-- 2. public — raw Wikidata world model
--    Populated by db/sync/sync_wikidata.py (live WDQS, recommended) or
--    db/sync/import_dump.py (HF parquet dump, legacy).
--    Read by: db/sync/build_world.py + build_words.py (transforms into world.*),
--    and directly by the engine's geo NEARBY (world19 -> public.settlement).
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
  lat          double precision,      -- world19 geo NEARBY reads lat/lng
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
-- 3. world."words" — THE entity-resolution index (pgvector).
--    One row per SURFACE form (label or alias) -> canonical entity + qid.
--    embedding = bge-small-en-v1.5 [CLS], L2-normalized, 384-dim; cosine via <=>.
--    Populated by db/sync/build_words.py; single rows appended by the engine's
--    lazy sync (sync_entity.ensure_entity) and by sync_types.py (type='type').
--    Read by: query16 (_nn/_resolve/value-membership routing, cell bridges),
--    world17 (grounding, _resolve_world_qid, qid->label), world18 (lookup).
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS world."words" (
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

CREATE INDEX IF NOT EXISTS ix_words_type_norm  ON world."words"(type, norm);
CREATE INDEX IF NOT EXISTS ix_words_type_qid   ON world."words"(type, qid);
CREATE INDEX IF NOT EXISTS ix_words_city_norm  ON world."words"(norm) WHERE type = 'city';
-- HNSW cosine index (pgvector defaults: m=16, ef_construction=64) — the source
-- created it with no explicit parameters, so defaults are the contract.
CREATE INDEX IF NOT EXISTS ix_words_hnsw       ON world."words" USING hnsw (embedding vector_cosine_ops);

-- ----------------------------------------------------------------------------
-- 4. world."types" — the type taxonomy DAG (one row per node, qid-keyed).
--    Populated by db/sync/sync_types.py from db/sync/data/taxonomy.csv.
--    resolver_type links legacy words.type strings ('city') to the node qid
--    (Q515) — set by db/sync/unify_words_qid.py (or sync_types.py).
--    Read by: sync_entity.wlabel (wikipedia table naming), world17
--    (_resolve_world_qid), route19 taxonomy walk.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS world."types" (
  qid           text PRIMARY KEY,
  label         text,
  parent_qid    text,
  is_leaf       boolean,
  world_table   text,                 -- Cities/Countries/Places/Continents/Elements or NULL
  depth         int,
  resolver_type text                  -- legacy words.type string this node resolves ('city' -> Q515)
);

CREATE INDEX IF NOT EXISTS ix_types_parent   ON world."types"(parent_qid);
CREATE INDEX IF NOT EXISTS ix_types_leaf     ON world."types"(is_leaf);
CREATE INDEX IF NOT EXISTS ix_types_resolver ON world."types"("resolver_type");

-- ----------------------------------------------------------------------------
-- 5. world friendly tables + "... in the World" views.
--    Denormalized copies of public.* the SQL planner references by friendly
--    name (query14/15 planner, world18 ENTITY_ATTRS). Populated by
--    db/sync/build_world.py. The views exist because generated SQL uses the
--    "<X> in the World" spelling.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS world."Cities" (
  name        text,
  country     text,
  population  bigint,
  is_primary  int,                    -- 1 = most populous settlement with this name
  updated_at  text,
  source      text,
  qid         text                    -- stable key (world18 joins Cities by qid)
);

CREATE TABLE IF NOT EXISTS world."Countries" (
  name           text,
  currency       text,
  currency_name  text,
  continent      text,
  valid_from     text,                -- planner supports as-of temporal filters
  valid_to       text,
  updated_at     text,
  source         text
);

CREATE TABLE IF NOT EXISTS world."Places" (
  name        text,
  kind        text,                   -- 'city' | 'country'
  lat         double precision,
  lng         double precision,
  hemisphere  text,
  population  bigint,
  updated_at  text,
  source      text
);

CREATE TABLE IF NOT EXISTS world."Elements" (
  name           text,
  symbol         text,
  atomic_number  int,
  mass           double precision,
  updated_at     text,
  source         text
);

CREATE TABLE IF NOT EXISTS world."Continents" (
  name        text,
  updated_at  text,
  source      text
);

CREATE TABLE IF NOT EXISTS world."States" (
  name        text,
  country     text,
  population  bigint,
  level       text,                   -- state / province / county / ...
  updated_at  text,
  source      text
);

-- curated alias -> canonical country name (legacy normalization table;
-- superseded by world."words" but still created by the build scripts)
CREATE TABLE IF NOT EXISTS world."Country Aliases" (
  alias  text,
  name   text
);

CREATE INDEX IF NOT EXISTS ix_world_cities_lname     ON world."Cities"(lower(name));
CREATE INDEX IF NOT EXISTS ix_cities_qid             ON world."Cities"(qid);
CREATE INDEX IF NOT EXISTS ix_world_countries_lname  ON world."Countries"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_places_lname     ON world."Places"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_elements_lname   ON world."Elements"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_elements_lsym    ON world."Elements"(lower(symbol));
CREATE INDEX IF NOT EXISTS ix_world_continents_lname ON world."Continents"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_states_lname     ON world."States"(lower(name));
CREATE INDEX IF NOT EXISTS ix_world_calias           ON world."Country Aliases"(alias);

CREATE OR REPLACE VIEW world."Cities in the World"     AS SELECT * FROM world."Cities";
CREATE OR REPLACE VIEW world."Countries in the World"  AS SELECT * FROM world."Countries";
CREATE OR REPLACE VIEW world."Places in the World"     AS SELECT * FROM world."Places";
CREATE OR REPLACE VIEW world."Elements in the World"   AS SELECT * FROM world."Elements";
CREATE OR REPLACE VIEW world."Continents in the World" AS SELECT * FROM world."Continents";
CREATE OR REPLACE VIEW world."States in the World"     AS SELECT * FROM world."States";

-- ----------------------------------------------------------------------------
-- 6. wikipedia — qid-keyed faithful Wikidata tables. INTENTIONALLY EMPTY HERE.
--    Tables are named by the EXACT Wikidata type label (e.g. wikipedia."city",
--    wikipedia."hospital") with columns = the type's discovered Wikidata
--    properties (all text) + qid PRIMARY KEY + name. They are created:
--      * lazily at query time by the engine (sync_entity.ensure_table via
--        ensure_entity/lazy_resolve — the normal path), or
--      * up front by db/sync/build_wikipedia.py / mirror_schema.py (optional).
--    Rows are lazily fetched one entity at a time from Wikidata when a CSV
--    cell resolves to a qid that is not in the table yet.
-- ----------------------------------------------------------------------------

-- ----------------------------------------------------------------------------
-- 7. Per-user schemas (created BY THE ENGINE, documented for completeness):
--    CREATE SCHEMA IF NOT EXISTS "<verified-google-sub>";
--      "<sub>"."<upload-table>"                       -- the uploaded CSV rows
--      "<sub>"."<t> connected to wikipedia"           -- resolved FKs:
--          ("column" text, "value" text, "world_type" text,
--           "world_key" text, "country" text, "world_qid" text)
--      "<sub>"."<t> unconnected to wikipedia"         -- free-text vectors:
--          ("__pk" bigint, "column" text, "value" text,
--           "embedding" vector(896))                  -- unified-encoder dim
--    Queries run with:
--      SET search_path TO "<sub>", wikipedia, world, public
-- ----------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- chat: conversation identity + ownership (engine/conversations.py).
-- The working Postgres schema for a run is the CONVERSATION id (self-contained,
-- archivable). Authorization is by the verified user via user_conversation — a
-- client cannot use a conversation id it does not own (no IDOR). The engine also
-- creates these idempotently at runtime, so applying this file is optional.
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS "chat";

CREATE TABLE IF NOT EXISTS "chat"."user_profile" (
  user_id     text PRIMARY KEY,                    -- the verified Google sub (stable across devices)
  created_at  timestamptz NOT NULL DEFAULT now(),
  last_seen   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "chat"."conversation" (
  conversation_id text PRIMARY KEY,                -- also the name of this conversation's data schema (c_<32 hex>)
  initial_prompt  text,                            -- the opening question (drawer label)
  tables          jsonb,                           -- the uploaded CSVs [{name,data}] so a conversation re-opens self-contained
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "chat"."user_conversation" (
  user_id         text NOT NULL REFERENCES "chat"."user_profile"(user_id),
  conversation_id text NOT NULL REFERENCES "chat"."conversation"(conversation_id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS ix_user_conv ON "chat"."user_conversation" (user_id, created_at DESC);
