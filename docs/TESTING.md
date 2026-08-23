# Testing

PreReasoner has hermetic tests, browser-state tests, live database integrations, a deployment regression gate, and
Spider accuracy evaluation. They answer different questions and should not be collapsed into one green badge.

## Quick Local Checks

Run these before involving models, PostgreSQL, or network services:

```powershell
pip install -r requirements-ci.txt
python -m ruff check engine db training tests orchestrator mcp_server regress --select F,E9
python -m tests.test_sql_ast
python -m tests.test_calculations
python -m tests.test_master_ingest
python -m tests.test_routing
python -m tests.test_enrichment
python -m tests.test_source_sync
python -m regress.product_templates
python -m regress.source_activation
node --check web/public/lib/workbook.js
node web/tests/home_demo.test.js
node web/tests/workbook_reference.test.js
python -m compileall -q engine db training tests orchestrator mcp_server regress
git diff --check
```

These cover typed AST behavior, deterministic routing, private-reference selection and validation, workbook
reference state, JavaScript syntax, and Python syntax. `requirements-ci.txt` is intentionally independent of the
model stack. Live engine suites still require `requirements.txt`, model artifacts, and PostgreSQL.

`regress.product_templates` runs 35 source-cited public-template development cases, five for
each domain profile. These fixtures measure deterministic recognition but are not customer
held-out evidence. An opted-in held-out corpus stays under ignored `regress/private/` and can
be run with:

```powershell
python -m regress.product_templates --private-corpus regress/private/<corpus>.json
```

The loader accepts only schema version 1, explicit metadata-only consent, table and column
names, expected profile/roles, question, and gold AST shape. It rejects row values, unknown
fields, duplicate columns, unknown roles, and partial profile coverage at evaluation time;
the command prints a canonical SHA-256 replay identity. `regress.source_activation` is the
separate 25-positive/100-negative IANA selection and candidate-pool gate.

## Repository Runner

```powershell
python -m tests.run_all
```

For the CI-equivalent public-checkout run:

```powershell
$env:RUN_ENGINE_TESTS = "0"
$env:RUN_ORCHESTRATOR_TESTS = "0"
python -m tests.run_all
```

The runner executes the canonical suites in this order:

| Suite | Primary boundary |
|---|---|
| `tests.test_sql_ast` | Typed planning, ranking, recursion, constraints, extrema, evaluation contract |
| `tests.test_calculations` | Typed arithmetic, operand eligibility, complete joins, all-branch proof, abstention, and clarify transport |
| `tests.test_routing` | Shared route authority and cross-process determinism |
| `tests.test_compose` | Composed operations and relationship discovery |
| `tests.test_converse` | Optional presentation/fill behavior |
| `tests.test_master_ingest` | Private-reference validation, storage, and fixed-point selection |
| `tests.test_enrichment` | M0 profile/role contracts, intent contrastives, value typing, bounded adapters, domain gates, request-local materialization, tuple edges, replay manifests, and serving-shaped benchmarks |
| `tests.test_source_sync` | Hermetic fixtures for every public and credential-gated source parser, including hierarchy, composite-key, rights, and rejection invariants |
| `tests.test_mcp` | MCP response shape and engine adapter |
| `tests.test_orchestrator` | External Anthropic tool-use integration and HTTP envelope; requires a key |
| `tests.test_world` | Grounding, geo basics, and aggregate delegation |
| `tests.test_nongeo` | Non-geographic world resolution and lazy fill |
| `tests.test_world_joins` | Country, continent, and state world-table joins |
| `tests.test_route_wired` | Model-driven route to SQL end to end |
| `tests.test_geo` | Haversine, population, composition, delegation, and concurrency |

The live suites need runtime weights and a seeded PostgreSQL knowledgebase. The orchestrator suite is also
external and can be excluded with `RUN_ORCHESTRATOR_TESTS=0`. Some live suites need network access for an
uncached Wikidata entity. The runner reports unavailable suites as skipped so local development can continue, but a
skip is not a passing integration test. Record exact skips and prerequisites in a pull request.

Run live suites sequentially. They create and replace shared test fixtures; concurrently launching two aggregate
runs against one database can make one suite observe the other's fixture state.

## Deployment Regression Gate

After a serving, routing, world, or database change, run:

```powershell
python -m regress.run_regression --require-world
```

`--require-world` makes missing live prerequisites fail rather than silently reducing the gate to offline tests.
The gate covers core FK invariants, representative own-data SQL, canonical world answers, world-table joins, route
wiring, and non-geographic grounding. Its own-data tier uses the same post-ranking calculation admissibility selector
as live `TableQuery` serving; the gate must not execute raw rank 1 through a parallel policy path.

## Browser State Tests

The workbook reference tests run in a Node VM with a minimal browser/Firebase harness:

```powershell
node web/tests/workbook_reference.test.js
```

They cover dirty-state autosave, failed-save blocking, delete behavior, zero values, and snapshot restoration. Run
`node --check` on every changed JavaScript file as well.

For a manual browser pass, start Firebase Hosting from `web/`:

```powershell
npm install --global firebase-tools
firebase serve --only hosting --project <firebase-project> --port 5057
```

Point the browser at a local engine only for development:

```js
localStorage.setItem('pr_api_base', 'http://localhost:8080');
sessionStorage.setItem('pr_test_auth', '1');
```

Check reference edits and deletion, a direct own-data answer, a world-dependent answer, clarification, trace
rendering, and reload restoration. Never enable the test-auth bypass in a production deployment.

## Start A Local Engine

See [GETTING_STARTED.md](GETTING_STARTED.md) for full setup. The Docker path is:

```powershell
Copy-Item .env.example .env
docker compose up --build
docker compose --profile seed run --rm seed
```

The seed operation is required once for world tests. Native execution requires PostgreSQL 16 with `vector` and
`pg_trgm`, `db/init.sql`, the synchronized knowledge data, runtime weights, and:

```powershell
$env:AUTH_TEST_SUB = "localdev"
python -m engine.server
```

`AUTH_TEST_SUB` is a local test bypass and must not exist in production configuration.

## Spider Accuracy

Planner behavior changes require a fresh serving-faithful `whole_db` run:

```powershell
python spider/probe/fetch_data.py --include-train
python spider/probe/full_eval.py `
  --dbs spider/data/dbs `
  --config whole_db `
  --selection serving_top1 `
  --max-candidates 25 `
  --tag <unique-tag> `
  --out spider/results/<unique-tag>/whole_db/full_eval_whole_db
```

`whole_db` is the gold-blind headline. `gold_tables` is an oracle table-selection ablation and must not be reported
as standard Spider accuracy. Compare per-example records as well as aggregate strict, lenient, and scalar metrics;
an aggregate gain can hide regressions in an important query class.

The result JSON records source and artifact provenance. Do not reuse a checkpoint after planner, evaluator, or model
artifacts change. Canonical accepted measurements belong in [../spider/results/RESULTS.md](../spider/results/RESULTS.md),
not in source comments or duplicate result documents.

## Infrastructure Validation

When deployment files change, also run these where the CLIs are installed:

```powershell
docker compose config
docker build -t prereasoner-engine:test .
docker build -f Dockerfile.orchestrator -t prereasoner-orchestrator:test .
terraform -chdir=infra fmt -check
terraform -chdir=infra init -backend=false -input=false
terraform -chdir=infra validate
```

CI also runs credential-free, no-refresh plans to prove that the default creates no chat
resources and that `enable_orchestrator=true` fails unless `anthropic_api_key` is supplied.

If Docker or Terraform is unavailable, say so explicitly. Static parsing and unit tests do not replace an image
build or Terraform validation.

## Pull Request Evidence

Report:

1. focused tests and counts;
2. repository runner suites and skips;
3. deployment-gate status when applicable;
4. browser checks for frontend changes;
5. Spider aggregate and per-example deltas for planner changes;
6. infrastructure checks or unavailable tools.

Do not describe a flaky external lookup as a product regression without rerunning against stable fixtures. Do not
describe an execution-successful SQL candidate as semantically correct unless its expected result or structure was
asserted.
