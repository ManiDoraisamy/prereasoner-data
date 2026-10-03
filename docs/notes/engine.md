# engine/ — module map and serving contract

> Supplementary maintainer map. The current request and service boundaries are canonical in
> [../ARCHITECTURE.md](../ARCHITECTURE.md), configuration defaults in
> [../../.env.example](../../.env.example), and validation commands in
> [../TESTING.md](../TESTING.md). Update this note when module history changes, but do not use it
> to override those contracts.

The engine is ONE server, ONE package (`engine/`), run as `python -m engine.server`. It exposes
`POST /api/reason`, `POST /api/knowledge`, `POST /api/dimension`, `POST /api/converse`, and
`GET /healthz` (see docs/ARCHITECTURE.md for the full design). This note is the maintainer's map of
the modules, the env-var contract, and the data files the serving path opens.

## Module map (what each file does)

| module | classes / responsibility |
|---|---|
| `server.py` | ONE ThreadingHTTPServer. `POST /api/reason`, `POST /api/knowledge` (Firebase auth + RTDB streaming, shared `KnowledgeReasoner` + shared `WORLD_LOCK`), `POST /api/dimension` (authenticated stateless readout, own `DIM_LOCK`), `POST /api/converse` (Gemini fallback/present), `GET /healthz`. Exact-origin CORS, body/rate/row limits. |
| `auth.py` | `_verify_principal`, `_bearer` (security-critical). Test bypass via `AUTH_TEST_SUB`. |
| `config.py` | the ONE env-var reader (see contract below). |
| `tables.py` | `TableQuery`; canonical `table_name`, `csv_table` / `table_from_rows`, SQL quoting and table parsing. `TableQuery.__init__` defers encoder loading (the encoder overlay supplies the model at serve time). |
| `knowledge_tables.py` | `KnowledgeTableQuery` (implicit world `word_*` tables, meaning graph, freshness). |
| `pg.py` | `PgQuery`, `_TableQueryPg`; `_pg()` is fully config-driven (host/port/db/user/sslmode/password). |
| `resolve_base.py` | `RoutedQuery` (generalized routing + country aliases + States/Elements configs). |
| `entities.py` | `EntityQuery` (bge embedding NN entity resolution, pgvector `words`, and cell bridges). |
| `encoder_overlay.py` | `EncoderQuery`; `load_encoder` — the single loader for `encoder_meta.pt`/`encoder.pt`/`qwen_lora`. |
| `compose.py` | `ComposeEngine`. |
| `knowledge_query.py` | `KnowledgeQuery`; model-driven column routing (`KB_MODEL_ROUTE`) and non-geo world joins over pre-synchronized facts. |
| `knowledge_compose.py` | `ComposedKnowledgeQuery`; also the conversational detectors (`_has_data_signal`, `_human_tone`). |
| `knowledge.py` | `KnowledgeReasoner` (geo NEARBY wrapper; the shared serving entry point for reason/knowledge; the conversational coverage pre-gate + present-tagging). |
| `dimension.py` | `DimensionModel`. |
| `encoder.py` | `LiveQwen`; base model id from `BASE_MODEL_ID`. |
| `router.py` | `Router` — schema.org-property superposition-decode column typing (see docs/TRAINING.md); device from `DEVICE`. |
| `trace.py` | RTDB streaming. `RTDB_URL` is OPTIONAL — unset ⇒ `emitter()` is a no-op and `ensure_app()` initializes firebase-admin without a databaseURL (auth still works). |
| `bridge.py` | `Bridge` + the shared `STOP` predicate stopwords. |
| `fk_edges.py` | `edges()`; `N_EDGE`, `fam_dims_map`. |
| `encoder_model.py` | `RelationalModel` (the relational-content readout; state_dict-loaded). |
| `relations.py` | `relate`, `discover_fks`, `dedup`. |
| `embeddings.py` | `Embedder` (bge-small), `normalize_surface`, `pgvector_literal`. |
| `primitives.py` | pure SQL view builders. |
| `primitive_head.py` | `PrimitiveReader` (head at `DATA_DIR/primitives.npz`; default encoder = `EncoderQuery`). |
| `joins.py` | compose's join assembly: `join_plan` (fact selection + flatten-safe keep-lists). FK discovery delegates to `relations.discover_fks` (one shared detector). |
| `taxonomy.py` | `snake`, `name_like`, and the taxonomy constants (`TAX`/`LEAF_PATH`/`LEAF_QID`/`LEAF_TABLES`) loaded from `taxonomy.csv`. |
| `converse.py` | `reply()` — the optional in-chat Gemini fallback/presentation (see §"Conversational layer"). |
| `conversations.py` | the `chat` schema (conversation identity + ownership; IDOR-safe). |
| `master.py` | Per-user reference persistence, validation, and direct/multi-hop request selection through `relations.discover_fks`. |

Tests live in `tests/` (`test_geo.py`, `test_world.py`, `test_nongeo.py`, `test_world_joins.py`,
`test_route_wired.py`); most need a live seeded PostgreSQL database. See
[../TESTING.md](../TESTING.md).

The engine is a proper package run from the repo root (no `sys.path` hacks).

## Env-var contract (all read in `engine/config.py`)

| var | default | notes |
|---|---|---|
| `HOST` | `0.0.0.0` | HTTP bind address |
| `PORT` | `8080` | HTTP port |
| `KB_PG_HOST` | `localhost` | Postgres host, or a unix-socket dir path (Cloud Run) |
| `KB_PG_PORT` | `5432` | |
| `KB_PG_DB` | `world` | |
| `KB_PG_USER` | `postgres` | |
| `KB_PG_PASSWORD` | *(none)* | REQUIRED at connect time; clear RuntimeError if missing |
| `KB_PG_SSLMODE` | `prefer` | `prefer` works with both local docker Postgres and Cloud SQL. Set `require` in production. |
| `RTDB_URL` | *(unset)* | OPTIONAL. Unset ⇒ trace streaming cleanly no-ops (frontend falls back to full-JSON responses). |
| `AUTH_TEST_SUB` | *(unset)* | TEST-ONLY auth bypass (also names the per-request test schema). |
| `PREREASONER_DATA_DIR` | `engine/data` (package-relative) | where the model weights + data files live. |
| `DEVICE` | `cpu` | torch device for the router/encoder (`cuda` honored only if available). |
| `BASE_MODEL_ID` | `Qwen/Qwen2.5-0.5B` | the Qwen base the LoRA adapter attaches to; matches the training/ package. |
| `KB_MODEL_ROUTE` | `1` | `0` disables model-driven column routing (value-membership fallback). |
| `EXTERNAL_LLM_ENABLED` | `false` | Authoritative switch for every Gemini call (`engine/llm.py`). |
| `GOOGLE_CLOUD_PROJECT` | *(unset)* | OPTIONAL. The Vertex AI project for Gemini (ADC credentials, no key): `/api/master/generate` and stateless wording rewriting. Factual presentation does not need Gemini. |
| `GEMINI_MODEL` / `GEMINI_LOCATION` | `gemini-3.8-flash` / `global` | the Vertex AI model and location. |
| `GEO_TEST_SUB` | `geotest` | tests only. |

## Data files (engine/data — see its README for the full table)

The serving path opens: `qwen_lora/` (PEFT adapter for the Qwen base), `encoder.pt`
(the relational-content readout state_dict) + `encoder_meta.pt` (`{"alloc", "cfg"}`),
`taxonomy.csv`, `anchor_assignment.npz`, `dim_thresholds.json`, `primitives.npz`, and the
`word_*.json` world-table metadata. `alloc.json`, `families.json`, and `props_thr.json` are training
outputs kept beside the bundle; no `engine/` module reads them (see engine/data/README.md). The `*.pt`, `*.npz`, and `qwen_lora/` weights are gitignored (a fresh clone
has none) — the entrypoint gate reports missing weights at startup (see
`infra/README.md`).

## Model-loading format

`encoder_meta.pt` = `{"alloc": dict, "cfg": dict}` (tensors and primitive containers, loaded with
`weights_only=True`); `encoder.pt` = a bare `state_dict` (172 tensors, `proj.*` +
`blocks.*`) loaded into `RelationalModel(**cfg)`. `qwen_lora/` is a standard PEFT
adapter (`base_model_name_or_path: Qwen/Qwen2.5-0.5B`). The `.pt` files carry no
class references (only dicts / state_dicts), so they load independent of module
layout.

## Serving contract notes

1. `TableQuery.__init__` defers encoder loading; the encoder overlay supplies the
   model at serve time. A clear RuntimeError fires if `TableQuery` is used standalone
   without the overlay.
2. `EncoderQuery()` standalone loads the shipped unified model
   (`encoder_meta.pt`/`encoder.pt`). The serving paths (`KnowledgeQuery`,
   `DimensionModel`) use the same loads.
3. `_pg()` sslmode is `prefer` by default; set `KB_PG_SSLMODE=require` for Cloud SQL
   public-IP deployments.
4. RTDB streaming is a no-op unless `RTDB_URL` is set.
5. Server host default is `0.0.0.0:8080` (container-friendly).
6. `/api/reason` and `/api/knowledge` share ONE `KnowledgeReasoner` and ONE lock
   (`WORLD_LOCK`), preserving the one-request-per-model/set_ctx invariant.
   `/api/dimension` has its own model + lock.
7. `Router._load` device comes from `DEVICE` (default cpu).

## Open risks / follow-ups

- **Live-path verification:** the world/reason/dimension paths need Postgres + the
  full dependency set (peft, firebase-admin, spacy) — run `tests/` against the
  docker-compose DB after `pip install -r requirements.txt` and a `db/sync` run.
- **Two Router copies:** `training/` vendors a copy of the router as `lib/router.py`
  (calibration needs the served readout). Keep `engine/router.py` and the training
  copy in sync (flagged in docs/notes/training.md too).
- `knowledgebase."Country Aliases"`, `knowledgebase."types"`, `public.settlement`,
  the qid-keyed Wikidata tables, etc. must exist in the DB the engine points at — the
  db/ pipeline owns that schema; drift between db/sync and engine SQL is the main
  integration risk.
- `resolve_base.RoutedQuery.__init__` re-globs the same `word_*.json` dir as
  `knowledge_tables.load_word_tables` (harmless idempotent re-registration; worth
  simplifying someday).
- The engine initializes firebase-admin via ADC for token verification even when
  `RTDB_URL` is unset; serving `/api/reason`/`/api/knowledge` therefore needs Google
  credentials unless `AUTH_TEST_SUB` is set.
- `tests/test_nongeo.py` and `tests/test_world_joins.py` read the seeded projections through the
  serving role. Source synchronization is a separate offline job; request tests do not call WDQS.

## Behavior note: clarify never surfaces a raw QID

- `knowledge_query._clarify` never surfaces a raw Wikidata QID as the proposed entity
  in the human-facing rephrase (the "cities in Q734" bug). Some live
  `knowledgebase."words"` rows carry a bare QID as `canonical`; the rephrase drops the
  entity instead (the degenerate-query fallback applies). The words-table data gap
  itself is a db/sync concern (see docs/notes/db.md).

## Conversational layer

The engine side of stateless wording assistance and factual answer presentation. Full design
in docs/ARCHITECTURE.md §10; this note records the engine surface for maintainers.

- **`engine/answer_presentation.py`**: the shared deterministic renderer for engine and orchestrator
  answers. Structured values, nulls, currency and percentage evidence determine factual text;
  clarification and retry messages come from the engine envelope. No computed results are sent to Gemini.
- **`engine/server.py`**: `POST /api/converse` (`_post_converse`) — Firebase-auth'd like the reason
  routes; uses the shared deterministic renderer. The Gemini switch does not disable factual rendering.
- **`engine/question_rewrite.py`**: optional stateless schema/question wording assistance. It receives
  field names and types, never source values or computed answers; the engine builds and validates SQL.
  **`engine/reference_generation.py`** is the separate explicit reference-fill feature. Both external
  paths use the single `engine/llm.py` client and honor the deployment switch.
- **`engine/knowledge.py`** (`KnowledgeReasoner.serve`): the COVERAGE PRE-GATE (`_has_data_signal` → return
  `low_confidence` before reasoning) and `_tag_present` (apply the `present` flag when a real answer
  is human-toned), applied across both the geo and composed paths. `KnowledgeReasoner` also lives here.
- **`engine/knowledge_compose.py`**: the three cheap, schema-aware detectors — `_has_data_signal`
  (fails OPEN), `_human_tone` (fails CLOSED), and the shared `_schema_tokens` (a cue word that names
  a column is data, not tone). Lexicons: `_DATA_INTENT`, `_META_STOP`, `_HUMAN_CUE`, `_HUMAN_RE`.
- **`engine/trace.py`** (`stream_final`): streams `low_confidence` and `present` alongside the
  existing terminal nodes.
- **`engine/config.py`**: `GOOGLE_CLOUD_PROJECT` (with `EXTERNAL_LLM_ENABLED`) is OPTIONAL for the
  engine — it powers /api/converse; unset ⇒ graceful 503 degrade.
