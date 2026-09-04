# Prereasoner

Prereasoner is an interpretable AI system built around named dimensions. It maps language, data,
and source facts to explicit semantic properties and classes, then composes those dimensions into an
inspectable derivation.

[Website](https://prereasoner.com/) | [Try it](https://chat.prereasoner.com/)

Today, Prereasoner compiles those dimensions into typed SQL over tables. SQL gives each derivation a
precise execution path: the dimensions can be combined, checked against real rows, and rerun by a
reviewer. The same semantic model extends to public knowledge, source-grounded enrichment,
structured retrieval, and domain-specific calculations without hiding the decision in generated text.

The long-term objective is an interpretable AI substrate: a shared representation of objects,
attributes, relationships, and calculations that can be grounded in different sources and applied
across domains. SQL is the first concrete execution target because it makes each derivation
inspectable, testable, and useful today.

For a table question, Prereasoner identifies the columns and relationships it needs, searches a
bounded set of valid SQL queries, and returns the result with the query and supporting rows.

The learned model helps identify intent, column roles, and schema relationships. It does not generate
SQL text or numeric answers. A typed planner composes the named dimensions, checks the resulting
query, and executes it against the database. For fixed input data, configuration, database state,
and model files, the same request produces the same plan and result.

When the data does not support an answer, the engine reports that instead of filling the gap with a
guess. This is useful when a reviewer needs to reproduce a calculation, check the source rows, or
understand why a request was declined.

## Look Up Rows By Relationship

Your sheet has a city column holding Paris and Strasbourg. You ask for total sales in France.

Text-to-SQL may stop because the table has no country column.

Vector search embeds the cities and returns what looks like French. Strasbourg comes back. So does
Kehl, a German town near Strasbourg. Similarity is not membership.

Prereasoner can link the city column to a reference table, resolve each city to its entity, and join
that entity to its country. Kehl is in Germany, so it is left out. The query and the join are
visible in the result.

One request can use:

- uploaded tables owned by a conversation;
- reusable private reference tables owned by a user;
- shared public facts from source-owned releases, with Wikidata used for public entity identity and
  publisher datasets used for source-specific facts.

## What Is Deterministic

The answer is computed by SQL, not written by a decoder. The frozen Qwen model is used as an
encoder for intent and schema signals; it does not call `generate()` to write a query or a number.

Schema.org supplies the semantic vocabulary: classes, properties, domains, ranges, and inheritance.
It is not the source of mutable facts. Wikidata provides public entity identity and mapped
observations. Publisher-owned sources such as IANA, CLDR, GeoNames, ECB, CDC, and NLM provide
source-specific facts. Those facts remain in versioned database releases and are selected at
runtime; they are not treated as facts memorized by the model. `knowledgebase.schedule` records
source refresh expectations and the last successful refresh, so stale data can be reported rather
than silently treated as current.

The engine is Apache-2.0 with open weights and runs on your CPU, so regulated data stays on your own
hardware. Determinism removes sampling variance. It does not remove ambiguity or missing data, and
[Accuracy Boundary](#accuracy-boundary) says what is still open.

## Start Here

The [documentation map](docs/README.md) shows what is running, what is opt-in, and what is still
planned. It also points each kind of change to its owner.

The core path for a new contributor is:

1. [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) - install, run the public-checkout tests, and find the code.
2. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - follow a request from upload to result.
3. [docs/PROMPT_TO_SQL.md](docs/PROMPT_TO_SQL.md) - walk through one question and its typed query.
4. [docs/TESTING.md](docs/TESTING.md) - choose the right test for the change.
5. [CONTRIBUTING.md](CONTRIBUTING.md) - change discipline and pull-request evidence.

The planner, source-data, training, deployment, release, privacy, and roadmap paths are indexed in
[docs/README.md](docs/README.md). The enrichment roadmap is not required reading for a planner
change; it contains both shipped foundations and clearly marked future work.

## Deploy To Google Cloud

The supported Community Edition path opens a guided tutorial in Google Cloud Shell. It builds the
public source and weights in your project, applies a cost-reduced Terraform profile, initializes the
minimal Wikidata and ECB data, and removes its temporary bootstrap identity. A billing-enabled project and
Google authorization are required; the marketing website never receives those credentials.

[![Open in Cloud Shell](https://gstatic.com/cloudssh/images/open-btn.svg)](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https%3A%2F%2Fgithub.com%2FManiDoraisamy%2Fprereasoner-data&cloudshell_git_branch=v0.1.0&cloudshell_tutorial=deploy%2Fgcp%2Fcloudshell-tutorial.md&cloudshell_workspace=.&show=terminal)

Read the [deployment contract](deploy/gcp/README.md), including cost, state, browser-client, and
teardown boundaries, before presenting the button as a public install path.

## How A Request Works

```text
browser or MCP client
        |
        | tables + question + authenticated conversation
        v
engine/server.py                 HTTP/auth/request adapter
        |
        +--> engine/master.py    validates and selects relevant private references
        |
        v
engine/knowledge.py              one serving entry point
        |
        +--> own-data typed AST search and deterministic ranking
        |       engine/sql_search.py, engine/sql_ast.py, engine/sql_rank.py
        |
        +--> world grounding when a public entity relation is required
                engine/knowledge_query.py, engine/knowledge_compose.py
        |
        v
Postgres execution + inspectable trace
```

The AST planner receives uploaded tables and any selected reference tables in the same typed table format.
`engine.relations` owns the planner relationship graph: `discover_fks` infers scalar edges from request data, while
`relate(..., explicit_fks=...)` validates trusted internal tuple edges from reference enrichment. Client table payloads
cannot declare trusted edges. Public world joins are separate because they require entity-to-QID grounding.

## Data And Isolation

One PostgreSQL database contains:

| Scope | Schema | Purpose |
|---|---|---|
| Curated shared serving projections | `knowledgebase`; `public.settlement` | Internal resolver index, taxonomy, Wikidata-derived QID/geo projections, and the release-labelled ECB daily exchange-rate projection. These are legacy derived runtime schemas, not source owners; the coordinated Wikidata target is `wikidata` |
| Synchronized reference sources | `iana`, `cldr`, `google_libphonenumber`, `geonames`, `ecb`, `ec_tedb`, `nager_date`, `cdc`, `nlm_cde` | Immutable or bounded source snapshots. IANA country-name lookup is code-approved; raw Terraform is empty by default and the guided Community deploy enables it. See `docs/SOURCE_DATA.md` |
| Conversation | `c_<32hex>` | Uploaded tables and world-resolution bridges for one authorized conversation |
| User | `m_<md5(sub)>` | Private reference dimensions such as product-to-category or SKU-to-region |

The authenticated Google subject is verified server-side. Conversation ids are ownership-checked before they can
name a schema. A saved reference is not placed blindly on `search_path`: `engine.master.relevant_tables` loads
only references connected to the request by the production FK graph and materializes those bounded rows into the
working planner table set. This keeps unrelated cross-conversation data out of the query schema.

Reference tables have a deliberate contract: the first column is a non-empty, unique key; remaining columns are
attributes. The browser auto-saves changed references before a query. If that save fails, the query is stopped
instead of silently running against an older copy.

## Local Quickstart

Requirements:

- Python 3.11 (the supported local, CI, and container runtime)
- PostgreSQL 16 with `vector` and `pg_trgm`, or Docker
- runtime model artifacts in `engine/data/`

For planner, routing, enrichment, sync-parser, and migration work, install only the public CI dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements-ci-windows.lock.txt
$env:RUN_ENGINE_TESTS = "0"
$env:RUN_ORCHESTRATOR_TESTS = "0"
python -m tests.run_all
```

The full engine requires the model stack and manifest-pinned runtime artifacts. Provision them before
building the engine image, then seed the database before making a world-dependent request:

```powershell
Copy-Item .env.example .env
python -m engine.fetch_weights
docker compose up -d db
docker compose --profile seed run --rm seed
docker compose up --build engine
```

Chat orchestration is optional and is excluded from the default Compose stack. Start it only with an Anthropic key:

```powershell
docker compose --profile chat up --build
```

The default [weight repository](https://huggingface.co/prereasoner/prereasoner-weights) is public;
`engine.fetch_weights` needs no Hugging Face account or token and verifies the complete bundle against
the immutable revision and hashes in `engine/data/weights_manifest.json`. See
[engine/data/README.md](engine/data/README.md).

The home-page workbook contains orders with dates, ISO currency codes, and amounts, but no
rate sheet. Currency conversion is grounded in the synchronized ECB release through the
daily `knowledgebase.exchange_rate` projection. The projection expands each published rate
through the next applicable calendar days, retains the true ECB business date, joins each
dated fact row on `(currency, date)`, verifies `SUM(amount * rate_to_target)`, and reports the
pinned source release. Uploaded rate tables still take precedence as the user's own data.

Docker Compose exposes the engine on `http://localhost:8080` with the test-only principal `localdev`. After the
engine is healthy:

```powershell
$body = @{
  tables = @(@{name="sales.csv"; data="product,amount`nHat,20`nCoat,80"})
  question = "total amount"
} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://localhost:8080/api/reason -Method Post `
  -Headers @{Authorization="Bearer dev"} -ContentType application/json -Body $body
```

For native Python, Firebase emulator, and database-seeding instructions, use
[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

## Tests

Fast checks that need neither weights nor Postgres:

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

The repository-wide runner executes hermetic suites and then live suites when their prerequisites are available:

```powershell
python -m tests.run_all
```

Skipped live suites are reported as skips and are not evidence of a full integration pass. Spider metrics and exact
evaluation provenance live only in [spider/results/RESULTS.md](spider/results/RESULTS.md); `whole_db` is the
gold-blind headline configuration, while `gold_tables` is an oracle ablation.

## Repository Map

| Path | Owner |
|---|---|
| `engine/` | Runtime typing, planning, grounding, execution, auth, conversations, and references |
| `engine/enrichment/` | Deterministic intent, source policy, request-local reference materialization, and replay manifests |
| `web/` | Static workbook UI, Firebase Hosting configuration, and browser tests |
| `orchestrator/` | Optional conversational tool loop; it presents engine results but does not invent numbers |
| `mcp_server/` | MCP adapter over the same engine API |
| `db/` | Reproducible PostgreSQL schema and Wikidata synchronization |
| `training/` | Property/intent training and calibration; not imported as a second serving path |
| `tests/` | Hermetic and live integration suites |
| `spider/` | Serving-faithful Spider evaluation and recorded results |
| `docs/` | Architecture, developer guides, testing, research, and training |

## Accuracy Boundary

Determinism removes sampling variance; it does not remove ambiguity, incomplete schema linking, candidate-search
limits, ranking errors, missing world data, or incorrect relationship inference. Accuracy work is therefore split
into measurable stages: routing, table selection, candidate-pool recall, top-1 ranking, execution, and evaluation.
See [docs/SQL_AST.md](docs/SQL_AST.md) and [spider/results/RESULTS.md](spider/results/RESULTS.md).

## License

[Apache 2.0](LICENSE). Models, knowledge data, and dependencies retain their upstream terms; see
[THIRD_PARTY.md](THIRD_PARTY.md). Citation metadata is in [CITATION.cff](CITATION.cff).
