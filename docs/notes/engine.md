# engine/ migration notes (runtime20 → open-source release)

Source of truth: `C:\work\prereasoner-flat-data\runtime20` (root `*.py` files only; the `svc_dim/`,
`svc_reason/`, `svc_world/` directories are stale copies and were ignored). Three Cloud Run services
(server18 `/infer-runtime20reason`, server17world `/infer-runtime20w`, server19 `/infer-runtime20`) were
consolidated into ONE server, ONE package (`engine/`), functional names, run as `python -m engine.server`.

Verified: `python -m py_compile` on every engine/ + tests/ file; smoke imports of every module; grep of
engine/, tests/ and requirements.txt for `runtime20|runtime1|34.123.19.176|prereasoner-inference-default-rtdb|
infer-runtime` (case-insensitive, docstrings included) → **zero hits**. `encoder.pt` was confirmed to load as a
plain `state_dict` into the reconstructed model class.

## Serving closure (what moved and why)

Traced transitively from server18.py / server17world.py / server19.py. Everything below is reachable at
serving time; nothing else was copied.

## Module map (old → new)

| old (runtime20) | new (engine) | classes / notes |
|---|---|---|
| server18.py + server17world.py + server19.py | `server.py` | ONE ThreadingHTTPServer. `POST /api/reason`, `POST /api/knowledge` (Firebase auth + RTDB streaming, shared KnowledgeReasoner + shared WORLD_LOCK), `POST /api/dimension` (stateless, own DIM_LOCK), `GET /healthz`. NO legacy route aliases. CORS + MAX_BODY(10MB)/MAX_SHEETS(8)/MAX_ROWS(5000) preserved exactly. |
| server16.py (auth part) | `auth.py` | `_verify_principal`, `_bearer`, `_slug` extracted **verbatim** (security-critical); env bypass `RUNTIME16/15/14_TEST_SUB` → single `AUTH_TEST_SUB`. Rest of server16 deleted. |
| — (new) | `config.py` | the ONE env-var reader (see contract below). |
| query11.py | `tables.py` | `Query11` → `TableQuery`; `csv_table`, `qident/qlit/qual`, `name_words/wmatch`. **Absorbed `infer6.parse_rows/dim_label/LABEL`** (the only parts of infer6 in the closure). `TableQuery.__init__` no longer loads the runtime11 checkpoint (see "runtime11 artifacts" below). |
| query12.py | `knowledge_tables.py` | `Query12` → `KnowledgeTableQuery` (implicit world word_* tables, meaning graph, freshness). |
| query14.py | `pg.py` | `Query14` → `PgQuery`, `_Query11Pg` → `_TableQueryPg`; `_pg()` now fully config-driven (host/port/db/user/sslmode/password) — the hardcoded Cloud SQL IP is gone. |
| query15.py | `resolve_base.py` | `Query15` → `RoutedQuery` (generalized routing + country aliases + States/Elements configs). |
| query16.py | `entities.py` | `Query16` → `EntityQuery` (bge embedding NN entity resolution, pgvector words, cell bridges, lazy city fill). |
| query17.py | `encoder_overlay.py` | `Query17` → `EncoderQuery`; `load_runtime20_encoder` (from world17) moved here as `load_encoder` — the single loader for `encoder_meta.pt`/`encoder.pt`/`qwen_lora`. The old `load_unified_encoder` (runtime17 artifacts, retired in production) was dropped; `EncoderQuery.__init__` now loads the ONE shipped model. |
| reason18.py | `compose.py` | `Reason18` → `ComposeEngine`. |
| world17.py | `knowledge_query.py` | `Query17World` → `KnowledgeQuery`. `KB_MODEL_ROUTE` read via config. |
| world18.py | `knowledge_compose.py` | `Query18World` → `ComposedKnowledgeQuery`. |
| world19.py | `world.py` | `Query19World` → `KnowledgeReasoner` (geo NEARBY wrapper). |
| dim19.py | `dimension.py` | `Query19` → `DimensionModel`. |
| encoder19.py | `encoder.py` | `LiveQwen`; base model id from `BASE_MODEL_ID`. |
| route19.py | `router.py` | `Router`; device from `DEVICE`; taxonomy constants from `engine.taxonomy`. |
| rtdb19.py | `trace.py` | restructured: `RTDB_URL` is OPTIONAL — unset ⇒ `emitter()` returns a no-op and `ensure_app()` initializes firebase-admin without a databaseURL (auth still works). The hardcoded RTDB url default is gone. |
| bridge17.py | `bridge.py` | `Bridge` + the shared `STOP` predicate stopwords (the serving import). Demo `_demo` dropped. |
| walker7.py | `graph_walk.py` | `build_from_units`, `edges_from_meta`. |
| edges11.py | `fk_edges.py` | `edges11()` renamed `edges()`; `N_EDGE`, `fam_dims_map`. |
| model11.py | `encoder_model.py` | `Runtime11Model` → `RelationalModel` (state_dict-compatible: only the class name changed, weights unaffected). |
| relate11.py | `relations.py` | `relate`, `discover_fks`, `dedup`. |
| embed16.py | `embeddings.py` | `Embedder` (bge-small), `normalize_surface`, `pgvector_literal`. |
| primitives18.py | `primitives.py` | pure SQL view builders. |
| prims18.py | `primitive_head.py` | `PrimitiveReader` (head at `DATA_DIR/primitives.npz`; default encoder = `EncoderQuery`). |
| join18.py | `joins.py` | offline FK discovery for the compose engine. |
| scripts/build_review.py (constants only) | `taxonomy.py` | NEW extraction: `snake`, `name_like` (from scripts/discover_csv_types), `TAX`/`LEAF_PATH`/`LEAF_QID`/`LEAF_TABLES` loaded from `taxonomy.csv`. The rest of build_review (training-data building) did NOT move. |
| scripts/sync_wikidata_world.py (runtime part) + build_world_wdqs.py (client part) | `knowledge_sync.py` | NEW extraction: `lazy_resolve`, `ensure_entity`, `find_entity`, `discover`, `fetch_one`, `wlabel`, `ensure_table` + the WDQS client (`wdqs`, `V`, `qid_of`, `wbsearch`, `ask`, `ENDPOINT`, `UA`). These are genuinely reachable at serving time (lazy Wikidata fill in entities.py / knowledge_query.py). The bulk-sync CLI (`fetch`, `main`) did not move — that lives in `db/sync`. |
| test_geo19 / test_world19 / test_nongeo19 / test_world_joins19 / test_route_wired | `tests/test_geo.py` / `test_world.py` / `test_nongeo.py` / `test_world_joins.py` / `test_route_wired.py` | imports → engine.*, env vars → AUTH_TEST_SUB; still need live Postgres (see tests/README.md). test_route_wired's bridge-table name was fixed from the stale "customers connected to the world" to the actual `_conn_bridge_name()` ("… connected to wikipedia") — in the source that assertion could never pass. |

All `sys.path` ROOT hacks removed everywhere; engine is a proper package run from the repo root.

### Class renames (safe: state_dict loading confirmed)

`torch.save` in the training pipeline writes `{"alloc": …, "cfg": …}` (plain dicts) to `runtime20.pt` and
`model.state_dict()` to `runtime20_model.pt` (verified in train17.py / scripts/train19.py /
scripts/reanchor20.py, and re-verified by loading the shipped files). **No full-module pickles** ⇒ class
renames cannot break loading. Renamed: `Query19World→KnowledgeReasoner`, `Query19→DimensionModel`,
`Query18World→ComposedKnowledgeQuery`, `Query17World→KnowledgeQuery`, `Query17→EncoderQuery`, `Query16→EntityQuery`,
`Query15→RoutedQuery`, `Query14→PgQuery`, `Query12→KnowledgeTableQuery`, `Query11→TableQuery`,
`Runtime11Model→RelationalModel`, `Reason18→ComposeEngine`.

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
| `KB_PG_SSLMODE` | `prefer` | NEW: source hardcoded `require` (Cloud SQL public IP); `prefer` also works with local docker Postgres. Set `require` in production. |
| `RTDB_URL` | *(unset)* | OPTIONAL. Unset ⇒ trace streaming cleanly no-ops (frontend falls back to full-JSON responses). Replaces the hardcoded firebaseio url. |
| `AUTH_TEST_SUB` | *(unset)* | TEST-ONLY auth bypass. Replaces `RUNTIME16_TEST_SUB`/`RUNTIME15_TEST_SUB`/`RUNTIME14_TEST_SUB` (auth) and `RUNTIME17/18_TEST_SUB` (test schemas). |
| `PREREASONER_DATA_DIR` | `engine/data` (package-relative) | Replaces `RUNTIME20_DEPLOY_DIR` / `RUNTIME11_DEPLOY_DIR`. |
| `DEVICE` | `cpu` | torch device for the router/encoder (`cuda` honored only if available). |
| `BASE_MODEL_ID` | `Qwen/Qwen2.5-0.5B` | Replaces `RUNTIME10_MODEL_ID`; name matches the training/ package. |
| `KB_MODEL_ROUTE` | `1` | `0` disables model-driven column routing (value-membership fallback). Unchanged name (no version). |
| `GEO_TEST_SUB` | `geotest` | tests only. |

## Data files (engine/data — see its README for the full table)

Copied + renamed version-free: `qwen_lora/`, `runtime20_model.pt`→`encoder.pt`, `runtime20.pt`→
`encoder_meta.pt` (name aligned with the training/ package; both .pt files re-saved so even the torch zip's
internal archive name is version-free), `alloc20.json`→`alloc.json`, and unchanged names:
`anchor_assignment.npz`, `assignment.csv`, `taxonomy.csv`, `dim_thresholds.json`, `route_thresholds.json`,
`primitives.npz`, `word_{city,country,state,element}.json`. This list was verified against every file-open in
the closure.

## Dropped (with reasons)

| dropped | reason |
|---|---|
| `infer6.py`, `infer11.py`, `infer12.py` | infer11/12 exist only as fallbacks for build_svc19.py (deleted bundler) — NOT in the serving closure. infer6's `parse_rows`/`dim_label` WERE in the closure (query11 imports them) and were absorbed into `tables.py`; the `Runtime6Model` class itself is unreachable. |
| `build_svc19.py` | the per-service bundling script; obsolete with one package. |
| `server16.py` (serving part) | legacy service; only its auth helpers survive in `auth.py`. |
| legacy routes `/infer-runtime20*`, `/query`, `/infer` | intentionally dead; the web frontend already calls `/api/reason` / `/api/knowledge`. |
| `runtime11.pt`, `runtime11_model.pt`, `runtime11_thresh.json`, `alloc11.json` | **Determination:** the source DID load runtime11.pt/_model.pt/_thresh.json at startup (`Query11.__init__` via the Query16→…→Query12 chain), but every one of those attributes is immediately OVERWRITTEN by the unified-encoder overlay (`world17.load_runtime20_encoder` shares alloc/nc/dims/sid/thr/model/nL/tok/qwen/hdim onto q11) — the loaded weights were pure startup waste. `TableQuery.__init__` is now a deferred shell (attrs None; `_encode` raises clearly if used un-overlaid), so the runtime11 artifacts are NOT copied. `alloc11.json` was never opened by the closure at all. |
| `runtime17.pt`, `runtime17_model.pt`, `runtime17_thresh.json` + `load_unified_encoder` | retired in production ("runtime17 DATA retired" — world17 used methods only); `EncoderQuery.__init__` now loads the shipped unified model instead. |
| training corpora / caches (`reanchor_emb_cache.pt`, `wd_cache.json`, `clusters.json`, `mapped_columns.json`, `type_instances.json`, `sql_graphs_*.jsonl`, `units_*.jsonl`, `join_graphs_*.jsonl`, `full-taxonomy.csv`, `inference.csv`, `columns.csv`, `properties.csv`, `.bak/`, `ent_chunk_*`, `renames_*`, `cluster_*`, `dbpedia_*`, `p279_cache.json`, `taxonomy_dims.npz`, `alloc11.json`, `route_calib_scores.json`, `route_eval19.json`, `discovered_types.json`, `value_*`, `wd_class_search.json`, `nodes.csv`, `qid.csv`) | training/build-time artifacts, not opened by the serving closure (training/ and db/ have their own copies where needed). |
| `words.db` | referenced only by the SQLite fallback path (`KnowledgeTableQuery._connect`/`ambiguities`/`_world_rows`), and `PgQuery` overrides all three on every live route (verified against the MRO), so the 34 MB file is dead weight. Existed in the source data dir; not shipped. |
| `train11.py`, `train17.py`, `scripts/*` (except the constants/functions extracted into `taxonomy.py`/`knowledge_sync.py`), `build_world_pg.py`, `build_world_wdqs.py` (bulk part), `route_model_test.py` | training / offline DB-build / experiment scripts — live in `training/` and `db/sync` per their own migration notes. |

## Model-loading format finding

`encoder_meta.pt` = `{"alloc": dict, "cfg": dict}` (plain pickled dicts, needs `weights_only=False`);
`encoder.pt` = a bare `state_dict` (172 tensors, `proj.*` + `blocks.*`). Loaded and round-tripped
successfully into the renamed `RelationalModel(**cfg)` under torch 2.12 → class renames are safe, and the
files were re-saved so their zip-internal names carry no version strings. `qwen_lora/` is a standard PEFT
adapter (`base_model_name_or_path: Qwen/Qwen2.5-0.5B`).

## Behavior deltas (intentional, small)

1. `TableQuery.__init__` defers encoder loading (no runtime11 weights at startup) — startup is lighter; a
   clear RuntimeError fires if anyone uses TableQuery standalone without the overlay.
2. `EncoderQuery()` standalone now loads the shipped unified model (`encoder_meta.pt`/`encoder.pt`) instead
   of the retired runtime17 artifacts. Serving paths (`KnowledgeQuery`, `DimensionModel`) are byte-for-byte the
   same loads as before.
3. `_pg()` sslmode is `prefer` by default (was hardcoded `require`); set `KB_PG_SSLMODE=require` for
   Cloud SQL public-IP deployments.
4. RTDB streaming is a no-op unless `RTDB_URL` is set (was: hardcoded production RTDB url as the default).
5. Server host default is `0.0.0.0:8080` (container-friendly; source dev default was `127.0.0.1:8000`).
6. `/api/reason` and `/api/knowledge` share ONE `KnowledgeReasoner` and ONE lock (in the source they were separate
   processes, each single-model + single-lock; sharing the lock preserves the one-request-per-model/set_ctx
   invariant). `/api/dimension` has its own model + lock, as before.
7. `Router._load` device comes from `DEVICE` (default cpu) instead of auto-cuda.
8. tests/test_route_wired.py: fixed the stale bridge-table name (see module map).

## Open risks / follow-ups

- **Live-path verification pending:** the world/reason/dimension paths need Postgres + the full dependency
  set (peft, firebase-admin, spacy were not installed on this machine) — run `tests/` against the
  docker-compose DB after `pip install -r requirements.txt` and a `db/sync` run. Everything compile- and
  import-verified here; model artifacts load-verified.
- **Two Router copies:** training/ vendored route19 as `lib/router.py` (calibration needs the served
  readout). Keep `engine/router.py` and the training copy in sync (flagged in docs/notes/training.md too).
- `knowledgebase."Country Aliases"`, `knowledgebase."types"`, `public.settlement`, `wikipedia.*` etc. must exist in the DB
  the engine points at — the db/ migration owns that schema; drift between db/sync and engine SQL is the
  main integration risk.
- `resolve_base.RoutedQuery.__init__` re-globs the same `word_*.json` dir as `world_tables.load_word_tables`
  (inherited quirk from the source, where the two layers had separate data dirs that later became one).
  Harmless (idempotent re-registration) but worth simplifying someday.
- The engine still initializes firebase-admin via ADC for token verification even when `RTDB_URL` is unset;
  serving `/api/reason`/`/api/knowledge` therefore needs Google credentials unless `AUTH_TEST_SUB` is set.
- `tests/test_nongeo.py` / lazy fill hit live Wikidata (WDQS + API) — network-dependent and slow by design.

## Post-E2E fix (2026-07-04)

- `knowledge_query._clarify`: never surface a raw Wikidata QID as the proposed entity in the
  human-facing rephrase ("cities in Q734" bug, found in real-Chrome E2E). Root cause: some
  live `knowledgebase."words"` rows carry a bare QID as `canonical`; the rephrase now drops the
  entity instead (the degenerate-query fallback applies). The words-table data gap itself
  is a db/sync concern (see docs/notes/db.md).

## Conversational layer (2026-07-17)

New subsystem — the engine side of the in-chat Sonnet fallback + answer presentation. Full design
in docs/ARCHITECTURE.md §10; this note records the engine surface for maintainers.

- **`engine/converse.py`** (new): `reply(question, clarify=, error=, tables=, answer=, sql=)` — one
  short Sonnet message. Two modes selected by whether `answer` is supplied: PRESENT (wrap a computed
  `{columns,rows}` verbatim; empty result renders an explicit "no rows" sentinel) and FALLBACK
  (offer a clarify rephrasing / explain a meta question; never state an unseen number). Uses
  `config.anthropic_api_key()` + `ANTHROPIC_MODEL`.
- **`engine/server.py`**: `POST /api/converse` (`_post_converse`) — Firebase-auth'd like the reason
  routes; forwards `answer`/`sql`; a missing key / SDK / upstream error is caught → **503** (logged)
  so the browser degrades to its built-in fallback. This is the ONLY generative-LLM call in the
  engine, and it is optional.
- **`engine/knowledge.py`** (`KnowledgeReasoner.serve`): the COVERAGE PRE-GATE (`_has_data_signal` → return
  `low_confidence` before reasoning) and `_tag_present` (apply the `present` flag when a real answer
  is human-toned), applied across both the geo and composed paths.
- **`engine/knowledge_compose.py`**: the three cheap, schema-aware detectors — `_has_data_signal`
  (fails OPEN), `_human_tone` (fails CLOSED), and the shared `_schema_tokens` (a cue word that names
  a column is data, not tone). Lexicons: `_DATA_INTENT`, `_META_STOP`, `_HUMAN_CUE`, `_HUMAN_RE`.
- **`engine/trace.py`** (`stream_final`): streams `low_confidence` and `present` alongside the
  existing terminal nodes.
- **`engine/config.py`**: `ANTHROPIC_API_KEY` is now OPTIONAL for the engine (was documented as
  orchestrator-only) — it powers /api/converse; unset ⇒ graceful 503 degrade.
