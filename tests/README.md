# Test Suites

Run commands from the repository root.

## Hermetic Suites

These need no PostgreSQL or network, and run in this order in `tests/run_all.py`. Install
`requirements-ci.txt`. Without the runtime bundle, `tests.test_complex_datasets` reports its
model-backed cases as SKIP, which is not a pass.

| Command | Covers |
|---|---|
| `python -m tests.test_sql_ast` | Typed AST validation/rendering, search, ranking, execution checks, and failure profiles |
| `python -m tests.test_calculations` | Typed calculation intent, operands, joins, grain, evidence, and abstention |
| `python -m tests.test_analysis` | Slug/identity validation, effective table/relationship/semantic hashing, bounded unique view names, executed-SQL preservation, and HTTP/live-stream parity |
| `python -m tests.test_deterministic_emitters` | Shared-plan validation, generated ORM execution, composite/missing-reference parity, mode policy, identity coercion, and full-result/preview separation |
| `python -m tests.test_decomposition` | Compound-question proposal validation, merge keys, leaf naming, and fused-plan contracts at the decomposition owner and serving boundary |
| `python -m tests.test_complex_datasets` | Promoted-planner leaves, fused complex DAGs, and SQL/Python stage verification against independent gold rows; its model-backed cases report SKIP without the runtime bundle |
| `python -m tests.test_routing` | The one shared serving/evaluation route decision |
| `python -m tests.test_router_evidence` | Property-family consensus and inspectable route evidence |
| `python -m tests.test_schema_decode` | Schema.org property/class evidence and deterministic abstention |
| `python -m tests.test_schema_coverage` | Ontology/corpus coverage, split discipline, and bundle provenance |
| `python -m tests.test_compose` | Composition DAG and explicit world-dependency records |
| `python -m tests.test_converse` | Reference autofill/presentation parsing and the presentation prompt with a faked Gemini call |
| `python -m tests.test_master_ingest` | Reference validation, direct/multi-hop selection, caps, and failure disclosure |
| `python -m tests.test_enrichment` | Source policy, intent, bounded materialization, and replay provenance |
| `python -m tests.test_source_sync` | Source parser, release, rights, and rejection invariants |
| `python -m tests.test_app_migrations` | Application migrations and least-privilege database grants |
| `python -m tests.test_request_limits` | Canonical request validation, resource bounds, auth bypass isolation, and paid-request budgets |
| `python -m tests.test_request_timing` | One `[timing]` line per request: no double-counted nested spans, emitted on failure, unchanged results, no user data in the line |
| `python -m tests.test_pg_upload` | Uploaded-sheet load issues page-bounded statements with byte-identical values, against a recording cursor |
| `python -m tests.test_dimension_model` | `/api/dimension` startup and encoder sharing |
| `python -m tests.test_kb_memo` | Request-scoped shared-knowledge memo dedupes identical lookups without changing answers |
| `python -m tests.test_encode_cache` | Encoder text cache dedupes forward passes without changing any vector |
| `python -m tests.test_stream_buffer` | Coalescing RTDB stream writer bounds writes and ends authoritatively |
| `python -m tests.test_dataset_semantics` | Dataset-semantics op validation, replay, application, and authenticated transport |
| `python -m tests.test_dataset_gold` | The browser release gate grades with the same gold as `tests.test_datasets` |
| `python -m tests.test_conversations` | Stable pagination, atomic storage accounting including analysis revisions, snapshot limits, and owned deletion |
| `python -m tests.test_sheet_sessions` | Per-Google-Sheet sidebar restoration, ownership, and bounded snapshots |
| `python -m tests.test_provenance` | Typed output lineage, source/release identity, and HTTP/stream parity |
| `python -m tests.test_release` | Public-tree invariants: artifact boundary, secure model pins, privacy route, and canonical owners |
| `python -m tests.test_community_deploy` | Community bootstrap and seed import, world projections, deployer defaults, and release smoke checks |
| `python -m tests.test_mcp` | MCP adapter contract |
| `python -m tests.test_orchestrator_unit` | Terminal tool-loop control and engine-outcome fallbacks with mocked services |
| `python -m tests.test_llm` | The one Gemini client (`engine/llm.py`) against the real SDK request types with a faked Vertex AI client |

The frontend state regression is separate because it runs under Node:

```powershell
node web/tests/workbook_reference.test.js
```

`python -m tests.test_orchestrator` is an external integration, not a hermetic suite. It requires
`GOOGLE_CLOUD_PROJECT` and Application Default Credentials, and calls Gemini on Vertex AI.

## Live Engine Suites

These require the manifest-pinned runtime artifacts and a seeded PostgreSQL knowledgebase configured through
`KB_PG_*` variables:

| Command | Covers |
|---|---|
| `python -m tests.test_world` | Type hierarchy, routing, aggregates, population, and nearby queries |
| `python -m tests.test_nongeo` | Non-geo world joins over pre-synchronized projections; requires seeded Postgres |
| `python -m tests.test_world_joins` | Join coverage for routable world tables |
| `python -m tests.test_route_wired` | Trained model driving routing end to end |
| `python -m tests.test_geo` | Geo SQL oracles and concurrency regression |
| `python -m tests.test_schema_probes` | Live property/class generalization and deterministic evidence |
| `python -m tests.test_datasets` | Every dataset directory's `prompt.txt` and non-`chat:` `eval.txt` follow-ups |
| `python -m tests.test_question_families` | Question families over the shipped datasets (output currency vs row filter, codes in the upload, place nouns, multi-word places, the sheet's own name, learned rankings) |

`test_datasets` discovers the workbooks under `web/public/dataset/` and requires one verified
expectation for each directory. It covers the current 24 dataset directories (customer-facing demos plus evaluation-only fixtures) across own-data aggregates,
joined discount and commission calculations, Wikidata-backed hospital/restaurant/bank joins,
country/continent joins, and ECB conversion.
Do not add a public demo until its source rows, routing path, expected result, and live regression
are all recorded in that test.

First runs can be slow because the Qwen encoder, PEFT, sentence-transformers, and spaCy load on CPU. `test_geo` intentionally
constructs a second model instance for its deadlock regression.

## Repository Runner

```powershell
python -m tests.run_all
```

`tests.run_all` always runs the hermetic suites. It adds live engine suites unless `RUN_ENGINE_TESTS=0`. Individual
live suites may report `SKIP` when prerequisites are absent; review the output rather than treating exit code zero as
proof that integration ran.

Use this for a deliberate hermetic-only pass:

```powershell
$env:RUN_ENGINE_TESTS = "0"
$env:RUN_ORCHESTRATOR_TESTS = "0"
python -m tests.run_all
```

## Other Gates

```powershell
python -m ruff check engine db deploy training tests orchestrator mcp_server regress spider world_eval --select F,E9
python -m compileall -q engine db deploy training tests orchestrator mcp_server regress spider world_eval
node --check web/public/lib/workbook.js
git diff --check
```

Spider is an evaluation, not a unit suite. Follow [../docs/SQL_AST.md](../docs/SQL_AST.md) and write results only to a
new provenance-bearing output directory. Do not overwrite committed aggregates without their matching per-example
records.
