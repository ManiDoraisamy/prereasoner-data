# Testing

Prereasoner has hermetic tests, browser-state tests, live database integrations, a deployment regression gate, and
Spider accuracy evaluation. They answer different questions and should not be collapsed into one green badge.

## Dual-emitter verification

The emitter suite executes real generated Python and SQL over SQLite fixtures. It covers stage
alignment, composite object relationships, missing multi-hop references, retained calculated fields,
grouped output order, full results versus 50-row previews, identity coercion, source determinism,
date/timestamp source normalization, request context isolation, explicit-mode fallback rejection,
and branch/merge parity for bounded cross and anti-join nodes.
Numeric comparison tests ensure large decimal differences are not rounded away. SQLite's native arithmetic is not a substitute for
PostgreSQL NUMERIC coverage.

`npm run test:browser` exercises the release journey plus `sql`, `py`, and `both` across direct and
orchestrated navigation and follow-up payloads. It also verifies mixed Python/SQL calls within one
orchestrated turn and signed-in conversation access on a mobile viewport. Its API and authentication
are local fixtures. It proves browser transport behavior, not actual backend execution or live authentication.

`tests.test_complex_datasets` is the hermetic semantic gate for the shipped complex fixtures. It
loads the promoted encoder, uses the production AST planner for every decomposition leaf, emits both
programs from the fused DAG, executes verification on one in-memory SQLite snapshot, and compares the
declared output columns with independently authored gold rows. The Chrome fixture separately checks
that the same plan metadata renders as a nested dependency tree and that every materialized node can
switch between its aligned SQL and Python source.

`tests.test_datasets` evaluates the public prompts and `eval.txt` expectations against the seeded
production `ComposedKnowledgeQuery` entry point. It runs `sql,python,verify,default` by default and checks
actual mode, exact stage parity in verify, cross-request answer agreement, and independent expectations.
`EVAL_EXECUTION_MODES` selects modes; `EVAL_DATASETS` narrows datasets for diagnosis, not a full release
claim. `EVAL_REPORT` saves per-case timings, source manifests, answers, and failures as JSON.
`customer-orders` and `orders-tiers` are separate fixtures; `chat:` evaluation lines require the
orchestrator's conversational context.

Before releasing a parity claim, run authenticated requests against a test PostgreSQL deployment
with the same input data and pinned references in each supported mode. Assert `execution.actual`,
`execution.verified`, every stage, and the independent expected answer. Include fractional money,
repeating division, averages, empty/null groups, duplicate facts, composite joins, and unsupported
world/compose cases. Run with the non-superuser serving role. An HTTP 200 page, a health response, or a 401 API rejection does not prove that
Python ran. Record unavailable prerequisites as untested, never as passed.

## Quick Local Checks

Run these before involving models, PostgreSQL, or network services:

```powershell
pip install --require-hashes -r requirements-ci-windows.lock.txt
python -m pip_audit -r requirements-ci-windows.lock.txt
python -m deploy.dependency_locks
python -m bandit -q -r engine db deploy training orchestrator mcp_server -x tests -lll
python -m tests.test_sql_ast
python -m tests.test_deterministic_emitters
python -m tests.test_complex_datasets
python -m tests.test_calculations
python -m tests.test_analysis
python -m tests.test_master_ingest
python -m tests.test_routing
python -m tests.test_router_evidence
python -m tests.test_schema_decode
python -m tests.test_schema_coverage
python -m tests.test_enrichment
python -m tests.test_source_sync
python -m tests.test_app_migrations
python -m tests.test_request_limits
python -m tests.test_conversations
python -m tests.test_provenance
python -m tests.test_release
python -m regress.product_templates
python -m regress.source_activation
node --check web/public/lib/workbook.js
node web/tests/home_demo.test.js
node web/tests/workbook_reference.test.js
npm ci
npm run test:browser
python -m ruff check engine db deploy training tests orchestrator mcp_server regress spider world_eval --select F,E9
python -m compileall -q engine db deploy training tests orchestrator mcp_server regress spider world_eval
git diff --check
```

These cover typed AST behavior, deterministic routing, private-reference selection and validation, workbook
reference state, JavaScript syntax, and Python syntax. The platform locks generated from
`requirements-ci.txt` are intentionally independent of the model stack. Live engine suites still
require the serving container, model artifacts, and PostgreSQL.
CI additionally scans the complete Git history with the immutable Gitleaks v3 action; a shallow local
working-tree scan is not equivalent to that gate.

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
| `tests.test_deterministic_emitters` | Plan validation, byte-stable source, ORM object relationships, stage alignment, execution policy, and SQL/Python parity |
| `tests.test_complex_datasets` | Promoted-planner leaves, fused complex DAGs, independent gold rows, and SQL/Python stage verification on shipped fixtures |
| `tests.test_calculations` | Typed arithmetic, operand eligibility, complete joins, all-branch proof, abstention, and clarify transport |
| `tests.test_routing` | Shared route authority and cross-process determinism |
| `tests.test_router_evidence` | Property-family consensus and surfaced routing evidence |
| `tests.test_schema_decode` | URI-indexed property evidence, deterministic class scoring, abstention, and artifact identity |
| `tests.test_schema_coverage` | Ontology coverage, corpus support, split grouping, and complete bundle provenance |
| `tests.test_compose` | Composed operations and relationship discovery |
| `tests.test_converse` | Optional presentation/fill behavior |
| `tests.test_master_ingest` | Private-reference validation, storage, and fixed-point selection |
| `tests.test_enrichment` | M0 profile/role contracts, intent contrastives, value typing, bounded adapters, domain gates, request-local materialization, tuple edges, replay manifests, and serving-shaped benchmarks |
| `tests.test_source_sync` | Hermetic fixtures for every public and credential-gated source parser, including hierarchy, composite-key, rights, and rejection invariants |
| `tests.test_app_migrations` | Application schema migrations, the world-table maintenance catalog, and least-privilege grants |
| `tests.test_request_limits` | Canonical request validation, resource bounds, auth bypass isolation, and paid-request budgets |
| `tests.test_analysis` | Slug/identity validation, effective table/relationship/semantic hashing, bounded unique view names, executed-SQL preservation, and HTTP/live-stream parity |
| `tests.test_conversations` | Stable pagination, atomic storage accounting including analysis revisions, snapshot limits, and owned deletion |
| `tests.test_provenance` | Typed output lineage, source/release identity, and HTTP/stream parity |
| `tests.test_release` | Public-tree invariants: artifact boundary, secure model pins, privacy route, and canonical owners |
| `tests.test_mcp` | MCP response shape and engine adapter |
| `tests.test_orchestrator_unit` | Terminal query control, tool-disabled presentation, and fallback preservation with contract fakes |
| `tests.test_orchestrator` | External Anthropic tool-use integration and HTTP envelope; requires a key |
| `tests.test_world` | Grounding, geo basics, and aggregate delegation |
| `tests.test_nongeo` | Non-geographic world resolution from pre-synchronized projections |
| `tests.test_world_joins` | Country, continent, and state world-table joins |
| `tests.test_route_wired` | Model-driven route to SQL end to end |
| `tests.test_geo` | Haversine, population, composition, delegation, and concurrency |
| `tests.test_schema_probes` | Live property/class generalization and cross-process determinism |
| `tests.test_datasets` | Every public workbook prompt and direct follow-up against the seeded serving path |

For a hosted release, set `REQUIRE_ORCHESTRATOR_TESTS=1` before running
`python -m tests.test_orchestrator`. With that flag, a missing `ANTHROPIC_API_KEY` is a failure rather
than a skip. The suite checks standalone pass-through, follow-up qualifier carry-over, and the joined
tier-discount regression at the exact question received by the engine; public pull-request CI keeps
this paid external test disabled. `orders-tiers` and `payment-commissions` provide independent
joined-rate fixtures so the calculation gate covers both discount and commission semantics.

The live suites need runtime weights and a seeded PostgreSQL knowledgebase with pre-synchronized source
projections. The orchestrator suite is also external and can be excluded with `RUN_ORCHESTRATOR_TESTS=0`.
Request tests do not fetch Wikidata. The runner reports unavailable suites as skipped so local development can
continue, but a skip is not a passing integration test. Record exact skips and prerequisites in a pull request.

Every directory under `web/public/dataset/` must contain a `prompt.txt` and an `eval.txt`. The URL manifest in
`web/public/dataset/dataset.txt` is generated from those directories and is checked by the web gate. Numeric and
clarification cases in `eval.txt` are run by `tests.test_datasets` against the direct engine. Lines prefixed with
`chat:` are conversational shorthand; they are intentionally excluded from that direct gate and belong to the
orchestrator/browser path. The `orders-tiers` fixture includes the joined `tier.csv` discount schedule and its
exact tier follow-up is covered by `tests.test_orchestrator`.

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

## Browser Tests

The workbook reference tests run in a Node VM with a minimal browser/Firebase harness:

```powershell
node web/tests/workbook_reference.test.js
```

The remaining locks target Linux containers. The `python-hermetic` GitHub Actions job audits them
on Linux; auditing them from Windows asks pip to resolve Windows-only transitive dependencies and is
not a valid check of the release image.

They cover dirty-state autosave, failed-save blocking, delete behavior, zero values, named-analysis identity,
per-call execution provenance, bounded snapshot compaction, and snapshot restoration. Run
`node --check` on every changed JavaScript file as well.

The release journey uses Playwright and a local deterministic API fixture:

```powershell
npm ci
npx playwright install chromium
npm run test:browser
```

`web/tests/browser/release-flow.spec.js` signs in through the local client contract, creates and uploads a real
XLSX workbook through the production Web Worker parser, waits for an answer, checks source and calculation
provenance, opens the source trace, distinguishes mixed per-call backends, exercises the mobile conversation
drawer, modifies one named workbook, creates a second workbook, restores two historical revisions from rail
links while retaining the input tab, renders a complex dependency tree with stage-level Python/SQL badges,
switches the final anti-join between readable Python and SQL, and deletes the conversation. The API response is a fixture so the browser
test is deterministic; Python integration suites separately cover the real engine and database.

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
python -m spider.probe.full_eval `
  --dbs spider/data/dbs `
  --config whole_db `
  --selection serving_top1 `
  --max-candidates 25 `
  --tag <unique-tag> `
  --out spider/results/<unique-tag>/whole_db/full_eval_whole_db
```

To exercise the production backend policy on the clean scalar-gold subset, keep the same runner and
comparison contract:

```powershell
python -m spider.probe.full_eval `
  --dbs spider/data/dbs `
  --config whole_db `
  --selection serving_top1 `
  --max-candidates 25 `
  --backend auto `
  --python-row-limit 10000 `
  --scalar-only `
  --tag <unique-tag>
```

`--backend python` measures generated-Python coverage without fallback. Evaluator `auto` also records
strict selected-SQL/Python equality for every Python-executed example but returns the Python result,
matching production output selection. `--backend verify` requires those denotations to agree. In every
mode, gold execution and scalar correctness continue to use the existing
`spider.probe.spider_eval.compare` functions. Do not compare only Python-lowerable examples and call the
result whole-suite accuracy; report lowering coverage, accuracy on executed Python, and accuracy over
all scalar gold examples.

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

The engine image entrypoint lets `python -m engine.retention_cleanup` run without loading model artifacts. Execute
that command only against a disposable or explicitly selected database. CI asserts the entrypoint contract without
connecting to customer storage; the live database suite covers cleanup behavior with isolated fixtures.

CI also runs credential-free, no-refresh plans to prove that the default creates no chat
resources, that `enable_orchestrator=true` fails without `anthropic_secret_id`, and that a
complete optional-chat configuration produces the expected resources. Both image inputs are
dummy immutable digests in this structural plan; no image is pulled.

If Docker or Terraform is unavailable, say so explicitly. Static parsing and unit tests do not replace an image
build or Terraform validation. On a fresh checkout, `terraform init -backend=false` is credential-free; an existing
working directory with a configured GCS backend must be isolated or initialized with its intended backend first.

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
