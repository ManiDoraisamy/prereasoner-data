# Developer getting started

Use this guide to get a clean checkout running. The first path needs neither model files nor
PostgreSQL. Add the larger runtime only when the part you are changing needs it. The
[documentation map](README.md) labels current, opt-in, external, and planned behavior.

Read [ARCHITECTURE.md](ARCHITECTURE.md) after the first successful test run. Contributors changing
shared source data should use [SOURCE_DATA.md](SOURCE_DATA.md) for the exact publisher-owned schema
inventory and then consult [KNOWLEDGE_ENRICHMENT_ROADMAP.md](KNOWLEDGE_ENRICHMENT_ROADMAP.md) for
implemented and planned activation work. Never infer a data owner from a domain label such as
healthcare, tax, or retail.

## 1. Choose a development level

You do not need the entire production stack for every change. Start with the smallest environment
that can exercise the code you are touching.

| Work | Required |
|---|---|
| Typed AST, routing, reference selection | `requirements-ci-windows.lock.txt`; no model weights or Postgres |
| Source parser or synchronizer | `db/sync/requirements.lock.txt` on Linux; PostgreSQL only for an actual load |
| Browser state and static UI | Node 20 and a static/Firebase server |
| Full engine request | Runtime weights and PostgreSQL |
| World grounding | Runtime weights and a seeded knowledgebase with the required offline projections |
| Conversational presentation | Everything above plus `ANTHROPIC_API_KEY` |

Start with hermetic tests. Add infrastructure only when the owner you are changing needs it.

## 2. Install

Use Python 3.11, which is the shared development, CI, and container target. Newer
interpreters are not a substitute for the 3.11 compatibility gate.

```powershell
git clone <repository-url>
Set-Location prereasoner-data
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
```

Install the hash-locked Windows test environment for deterministic development. GitHub Actions uses
the corresponding Linux lock. The matching `.txt` file without `.lock` is the maintained input used
to regenerate both locks:

```powershell
pip install --require-hashes -r requirements-ci-windows.lock.txt
```

`requirements.lock.txt` is the Linux serving-image lock and is installed by `Dockerfile`. Use the
container for the full model-backed runtime; do not install that Linux lock into a Windows virtual
environment.

Runtime artifacts are deliberately not committed. Fetch the manifest-pinned bundle:

```powershell
python -m engine.fetch_weights
```

The default [weight repository](https://huggingface.co/prereasoner/prereasoner-weights) is public and
requires no account or token. The fetch command pins an immutable repository commit and validates every
file hash before installation. `HF_TOKEN` is only needed for an explicitly configured private replacement.

## 3. Run fast tests first

```powershell
python -m tests.test_sql_ast
python -m tests.test_calculations
python -m tests.test_master_ingest
python -m tests.test_routing
python -m tests.test_router_evidence
python -m tests.test_schema_decode
python -m tests.test_schema_coverage
python -m tests.test_enrichment
python -m tests.test_source_sync
python -m tests.test_app_migrations
node web/tests/workbook_reference.test.js
python -m ruff check engine db training tests orchestrator mcp_server regress --select F,E9
python -m compileall -q engine db training tests orchestrator mcp_server regress
```

These establish the planner, routing, reference-data, frontend-state, and syntax baseline without a
live database.

## 4. Start PostgreSQL and the engine

Provision the runtime weights before building the engine image. Then start PostgreSQL, run
the one-time seed, and start the engine:

```powershell
docker compose up -d db
docker compose --profile seed run --rm seed
docker compose up --build engine
```

The separate conversational orchestrator is not part of the default stack. Set
`ANTHROPIC_API_KEY` and run `docker compose --profile chat up --build` only when testing it.

The seed is a one-time operation. It builds the resolution index, taxonomy, and world tables described in
[../db/README.md](../db/README.md). The engine listens on `http://localhost:8080` and Compose sets the development-
only `AUTH_TEST_SUB=localdev` identity.

For native execution, point the `KB_PG_*` variables in `.env` at a PostgreSQL 16 instance, apply `db/init.sql`, seed
it, then run:

```powershell
$env:AUTH_TEST_SUB = "localdev"
python -m engine.server
```

Never set `AUTH_TEST_SUB` in production.

Reference enrichment is off unless both code and deployment approve a dataset. For a local
IANA country-name exercise, sync IANA, grant only the registered tables to the existing
non-superuser serving role, then enable the dataset:

```powershell
$env:SYNC_PG_USER = "postgres"
$env:SYNC_PG_PASSWORD = "<admin password>"
python -m db.sync.sources.iana.sync
python -m db.reference_grants --role prereasoner_runtime --datasets iana_country
$env:ENRICHMENT_ACTIVE_DATASETS = "iana_country"
```

Do not use the sync role to run `engine.server`. Roll a source back only to a validated
retired release with `python -m db.sync.releases --schema iana --release-id <release-id>`.

## 5. Make a first request

```powershell
$request = @{
  tables = @(
    @{name="customers.csv"; data="customer_id,name,city`n1,Ada,Paris`n2,Lin,Lyon"},
    @{name="orders.csv"; data="order_id,customer_id,amount`n101,1,50`n102,2,80"}
  )
  question = "total amount"
} | ConvertTo-Json -Depth 6

Invoke-RestMethod http://localhost:8080/api/reason -Method Post `
  -Headers @{Authorization="Bearer dev"} -ContentType application/json -Body $request
```

Response fields vary by planner path. A successful data query contains a result and inspectable SQL;
it also carries the conversation id and can include route evidence, typing, intermediate views,
calculation evidence, provenance, or warnings when those mechanisms participated. Treat absent
optional evidence as path-specific, not as a different API version.

## 6. Understand private references

A reference table is a user-owned dimension whose first column is its unique join key. For example:

```text
product             category
Deerstalker Cap     Apparel
Calabash Pipe       Detective Accessory
```

The lifecycle is:

1. The workbook discovers or creates the reference sheet.
2. `POST /api/master` validates and atomically replaces the authenticated user's saved table.
3. Before each query, the browser auto-saves dirty references. Failure stops the query.
4. `engine.master.relevant_tables` loads saved references through one database connection.
5. The production FK detector selects direct and multi-hop references connected to the upload.
6. Selected rows become typed planner tables; the normal AST search plans and executes the join.

Reference selection does not scan question words, use gold SQL, or create a second planner. It uses the same
relationship graph that will be handed to AST search.

## 7. Run the frontend

```powershell
npm install --global firebase-tools
Set-Location web
firebase serve --only hosting --project <firebase-project> --port 5057
```

Open `http://localhost:5057`. For localhost-only engine testing, set these once in browser developer tools:

```js
localStorage.setItem('pr_api_base', 'http://localhost:8080');
sessionStorage.setItem('pr_test_auth', '1');
```

The frontend has no build step. `reason.html` and `knowledge.html` load the shared classic script
`web/public/lib/workbook.js`.

## 8. Find the right owner

| Change | Start here | Primary test |
|---|---|---|
| AST nodes/rendering | `engine/sql_ast.py` | `tests.test_sql_ast` |
| Candidate expansion/search | `engine/sql_search.py` | `tests.test_sql_ast` |
| Candidate scoring | `engine/sql_rank.py` | `tests.test_sql_ast` |
| FK inference | `engine/relations.py` | `tests.test_sql_ast`, `tests.test_master_ingest` |
| Saved references | `engine/master.py` | `tests.test_master_ingest` |
| Routing | `engine/routing.py` | `tests.test_routing` |
| Shared enrichment policy | `engine/enrichment/registry.py` | `tests.test_enrichment` |
| Typed calculation registry, expansion, and verification | `engine/calculations/` | `tests.test_calculations`, `tests.test_sql_ast` |
| Currency syntax and rate-column convention | `engine/currency_intent.py` | `tests.test_calculations`, `tests.test_enrichment` |
| Requested enrichment attributes | `engine/enrichment/intents.py` | `tests.test_enrichment` |
| Source lookup policy and ambiguity | `engine/enrichment/adapters.py` | `tests.test_enrichment` |
| Domain profiles and role evidence | `engine/domain_profiles.py`, `engine/domain_typing.py` | `tests.test_enrichment` |
| Public and opted-in metadata evaluation | `regress/product_templates.py` | `tests.test_enrichment` |
| Request-local enrichment and manifests | `engine/enrichment/runtime.py` | `tests.test_enrichment` |
| Source activation, grants, and rollback | `engine/enrichment/registry.py`, `db/reference_grants.py`, `db/sync/releases.py` | `tests.test_enrichment`, `tests.test_source_sync` |
| IANA/CLDR source ingestion | `db/sync/sources/<source>/sync.py` | `tests.test_source_sync` |
| World grounding | `engine/knowledge_query.py` | live world suites |
| HTTP/auth adaptation | `engine/server.py` | focused suite plus live request |
| Workbook lifecycle | `web/public/lib/workbook.js` | `web/tests/workbook_reference.test.js` |

`CLAUDE.md` is the repository's machine-agent change discipline and ownership map. Human contributors should follow
the same principle: extend the current owner, migrate every caller, and delete the displaced implementation.

## 9. Validate before a pull request

```powershell
$env:RUN_ENGINE_TESTS = "0"
$env:RUN_ORCHESTRATOR_TESTS = "0"
python -m tests.run_all
node web/tests/workbook_reference.test.js
python -m ruff check engine db training tests orchestrator mcp_server regress --select F,E9
python -m compileall -q engine db training tests orchestrator mcp_server regress
git diff --check
git status --short
```

Read the `tests.run_all` summary carefully. Suites that lack weights, database access, or network may skip; list
those skips in the pull request. Planner behavior changes also require a fresh serving-faithful Spider evaluation,
while frontend or reference plumbing changes do not unless they alter planner inputs used by Spider.

## 10. Common failure modes

- **Engine does not start:** run `python -m engine.fetch_weights` and verify `engine/data/weights_manifest.json`.
- **World tests skip:** set `KB_PG_PASSWORD` and seed the knowledgebase.
- **Reference is saved but not joined:** verify a unique first-column key, at least 90% FK inclusion, compatible
  column names, and exact text case; SQL equality joins are case-sensitive.
- **Query stops before running:** inspect the reference save error. The browser intentionally refuses to use a stale
  saved copy.
- **A benchmark appears better with `gold_tables`:** that is an oracle table-selection ablation, not the standard
  gold-blind Spider result.
