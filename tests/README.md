# Test Suites

Run commands from the repository root.

## Hermetic Suites

These need no PostgreSQL, model weights, or network. Install `requirements-ci.txt`.

| Command | Covers |
|---|---|
| `python -m tests.test_sql_ast` | Typed AST validation/rendering, search, ranking, execution checks, and failure profiles |
| `python -m tests.test_calculations` | Typed calculation intent, operands, joins, grain, evidence, and abstention |
| `python -m tests.test_routing` | The one shared serving/evaluation route decision |
| `python -m tests.test_router_evidence` | Property-family consensus and inspectable route evidence |
| `python -m tests.test_schema_decode` | Schema.org property/class evidence and deterministic abstention |
| `python -m tests.test_schema_coverage` | Ontology/corpus coverage, split discipline, and bundle provenance |
| `python -m tests.test_compose` | Composition DAG and explicit world-dependency records |
| `python -m tests.test_converse` | Reference autofill/presentation parsing with a mocked model stream |
| `python -m tests.test_master_ingest` | Reference validation, direct/multi-hop selection, caps, and failure disclosure |
| `python -m tests.test_enrichment` | Source policy, intent, bounded materialization, and replay provenance |
| `python -m tests.test_source_sync` | Source parser, release, rights, and rejection invariants |
| `python -m tests.test_app_migrations` | Application migrations and least-privilege database grants |
| `python -m tests.test_mcp` | MCP adapter contract |

The frontend state regression is separate because it runs under Node:

```powershell
node web/tests/workbook_reference.test.js
```

`python -m tests.test_orchestrator` is an external integration, not a hermetic suite. It requires
`ANTHROPIC_API_KEY` and may call Anthropic.

## Live Engine Suites

These require the manifest-pinned runtime artifacts and a seeded PostgreSQL knowledgebase configured through
`KB_PG_*` variables:

| Command | Covers |
|---|---|
| `python -m tests.test_world` | Type hierarchy, routing, aggregates, population, and nearby queries |
| `python -m tests.test_nongeo` | Non-geo world joins and lazy Wikidata fill; requires outbound network |
| `python -m tests.test_world_joins` | Join coverage for routable world tables |
| `python -m tests.test_route_wired` | Trained model driving routing end to end |
| `python -m tests.test_geo` | Geo SQL oracles and concurrency regression |
| `python -m tests.test_schema_probes` | Live property/class generalization and deterministic evidence |

First runs can be slow because Qwen, PEFT, sentence-transformers, and spaCy load on CPU. `test_geo` intentionally
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
python -m ruff check engine db training tests orchestrator mcp_server regress --select F,E9
python -m compileall -q engine db training tests orchestrator mcp_server regress
node --check web/public/lib/workbook.js
git diff --check
```

Spider is an evaluation, not a unit suite. Follow [../docs/SQL_AST.md](../docs/SQL_AST.md) and write results only to a
new provenance-bearing output directory. Do not overwrite committed aggregates without their matching per-example
records.
