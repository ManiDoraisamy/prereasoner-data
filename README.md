# Prereasoner

Prereasoner answers questions about your tables, and shows how it got each answer.

Large language models store what they know in thousands of unnamed dimensions. Nobody can say which
of them holds "France". Prereasoner's model works the other way: the dimensions it reads have names,
taken from [Schema.org](https://schema.org/) properties, a small set of column types, and a small set
of query intents. Facts come from [Wikidata](https://www.wikidata.org/) and other publisher releases
stored in a database, not from model weights. The engine turns a question into a typed SQL query the
database runs, so each step of an answer can be checked.

[Website](https://prereasoner.com/) | [Try it](https://chat.prereasoner.com/)

## A Question The Sheet Cannot Answer On Its Own

The home-page workbook is an `orders.csv` file with a `city` column and an amount in each order's own
currency. It has no `country` column. The question is:

> total amount in France in US dollars

Plain SQL cannot filter by country, because the sheet has no country column. An LLM could guess that
Paris is in France, but it reads that from unnamed dimensions you cannot inspect. Prereasoner does
it as a join:

1. Each `city` cell is resolved to a Wikidata entity (Paris → `Q90`) by exact lookup, with an
   embedding search as a fallback. The matches are stored in a bridge table for the conversation.
2. The bridge joins the `knowledgebase."city"` table on that ID, and the query keeps the rows whose
   city is in France.
3. Each amount joins the European Central Bank's published rate for its `(currency, date)` pair.
4. The database computes `SUM(amount * rate_to_usd)`.

The answer comes with the SQL, the resolved entities, every intermediate table, and the source
release the rates came from. If a city does not resolve, or a rate is missing for a date, the answer
says so instead of guessing.

## Named Dimensions

Think of colour in CSS. `rgb(165, 42, 42)` describes brown with three dimensions that everyone has
agreed to name. Prereasoner applies the same idea to the words people use about business data. A
Schema.org class such as `Product` is a table, its properties (`brand`, `color`, `weight`) are named
dimensions, and Wikidata items are the rows.

What runs today:

| Component | What it is | What it decides |
|---|---|---|
| Encoder | `Qwen/Qwen2.5-0.5B` used without its decoder, adapted with a LoRA and a 10-block relational readout trained to fill **90 named coordinates**: 9 column types (`is_num`, `is_time`, `currency`, ...), 71 property names (`addressCountry`, `brand`, `birthDate`, ...), and 10 query intents (`intent_agg_sum`, `intent_filter_gt`, `intent_sort_desc`, ...) | What each column is and what the question asks for |
| Schema.org head | A linear layer reading 80 Schema.org properties. A class score is a weighted sum of those property scores. 56 properties and 11 classes pass the release gates; the others abstain | Which world table a column may join. Joins also need exact source-key matches |
| Typed SQL search | A bounded search that builds queries as a typed syntax tree from those named readings, with hand-written ranking rules whose every adjustment is a named feature | The candidate queries, and which one is served: the best-ranked one that runs and matches the data |
| Execution | PostgreSQL, plus a generated Python program that mirrors the SQL for supported shapes | The numbers |

No model writes the SQL. The search builds every query from the encoder's named readings, runs each
candidate on a copy of your tables, drops the ones that fail or test a value the column never holds,
and serves the best-ranked one that is left. The answer shows the query, its evidence, and the rule
that chose it.

**Gemini can clarify wording for the search.** When the operator has switched on Gemini (Vertex AI),
it may reword a question once if no candidate runs or the selected plan leaves request wording
unresolved. Gemini receives the table and column names, types, and relationships, and the question with
the values it states quoted, but no other cell values and no conversation history. It does not write SQL
or select a query. The deterministic typed search builds and checks SQL from the rewrite; the response
labels when that rewrite supplied the wording.
The rewrite is not cached between requests.

The optional chat service uses Gemini to make follow-up wording explicit and request engine operations.
It sends schema metadata, bounded recent user questions and saved analysis questions. It sends no source
rows, computed answer values or earlier assistant replies. One deterministic renderer presents engine
results on both chat and direct-query surfaces. Explicit reference-table generation is a separate Gemini action.

### The Cost Of Naming

Named dimensions give up some of what a model could represent with unnamed ones. The current
version pays that cost in three visible ways:

- **Schema.org is lossy.** It names much less than an LLM represents. Only 56 of the 80 trained
  properties and 11 of 926 classes pass the release gates; every other class abstains.
- **Schema.org is mostly about business data.** It has few scientific types, and many of its types
  have no public data source yet. Facts come from Wikidata, the ECB, GeoNames, IANA, CLDR, CDC, NLM
  and the other releases listed in [docs/SOURCE_DATA.md](docs/SOURCE_DATA.md).
- **Nouns, not actions.** Properties name things. Actions are covered only by the ten fixed query
  intents, so Prereasoner handles questions over structured data such as CSV and spreadsheet files,
  and nothing broader.

The project is open source so that this vocabulary can grow the way the periodic table did, one
named dimension at a time. [docs/RESEARCH.md](docs/RESEARCH.md) gives the research position and its
limits. [docs/MODEL_CARD.md](docs/MODEL_CARD.md) gives the exact models, artifacts, and metrics.

## What Is Deterministic

For fixed input data, configuration, database state, and model files, the same request produces the
same plan and the same result. Determinism removes sampling variance. It does not remove ambiguous
wording or missing data. [Accuracy Boundary](#accuracy-boundary) says what is still open.

The answer is computed by a deterministic program. The supported subset of query shapes compiles one
immutable plan into both SQL and readable SQLAlchemy/Python, made of named `View` stages and operator
calls. Small inputs run the Python; larger inputs run the SQL; a verification mode runs both and
compares every stage. The workbook URL can choose with `?use=sql`, `?use=py`, or `?use=both`. The
default `auto` policy uses Python up to 10,000 estimated input rows and SQL above that. Shapes outside
the subset run as SQL; an explicit `py` or `both` request for them returns an error rather than a
silent fallback. See [the execution contract](docs/DETERMINISTIC_EMITTERS.md).

A conversation can keep several named analysis workbooks over the same tables. A refinement such as
"in US dollars" adds a revision to the current workbook; a different question such as "top selling
products" starts another. A follow-up that drops a filter ("now for Lyon" after "orders in Paris")
also drops that word from the workbook's name. Each saved revision restores its exact SQL, rows,
sources, and, where the shape is supported, the generated Python and its hashes.

## Start Here

The [documentation map](docs/README.md) shows what runs today, what is opt-in, and what is planned.
The core path for a new contributor:

1. [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) - install, run the public-checkout tests, find the code.
2. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - follow a request from upload to result.
3. [docs/PROMPT_TO_SQL.md](docs/PROMPT_TO_SQL.md) - follow one question to its typed query.
4. [docs/DETERMINISTIC_EMITTERS.md](docs/DETERMINISTIC_EMITTERS.md) - the matching SQL and Python programs.
5. [docs/TESTING.md](docs/TESTING.md) - choose the right test for a change.
6. [CONTRIBUTING.md](CONTRIBUTING.md) - change discipline and pull-request evidence.

## Deploy To Google Cloud

One click, into your own Google Cloud project. It deploys the engine and chat services to Cloud Run,
publishes the UI to Firebase Hosting, and loads the world database from a versioned seed.

[![Open in Cloud Shell](https://gstatic.com/cloudssh/images/open-btn.svg)](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https%3A%2F%2Fgithub.com%2FManiDoraisamy%2Fprereasoner-data&cloudshell_git_branch=v0.2.23&cloudshell_tutorial=deploy%2Fgcp%2Fcloudshell-tutorial.md&cloudshell_workspace=.&show=terminal)

**Nothing to configure afterwards.** Sign-in is set up for you, and chat runs on Vertex AI Gemini
through the service account: no API keys, no console steps. You supply a Google login, a project,
and a billing account.

It takes about 30 minutes and costs roughly **$90/month** while it runs, almost all of it Cloud SQL.

The button installs the last release whose guided install passed its launch test
([docs/COMMUNITY_LAUNCH_TEST.md](docs/COMMUNITY_LAUNCH_TEST.md)), `v0.2.23`. That release predates the
design this README describes: its own README at the tag says what it runs. The button moves to the
current release once that release passes the same test.

To remove it:

```bash
bash deploy/gcp/deploy.sh --project <PROJECT_ID> --destroy
```

[Deployment contract](deploy/gcp/README.md) (cost, teardown, how sign-in works) ·
[Release gate](docs/COMMUNITY_LAUNCH_TEST.md) ·
[Local dev setup](#install-on-a-local-machine) (contributors; Docker Compose and a Google Cloud project)

## How A Request Works

```text
browser, chat service, or MCP client
        |
        | tables + question + authenticated conversation
        v
engine/server.py                 HTTP/auth/request adapter
        |
        +--> engine/request_validation.py
        |                       canonical table names and bounded request shapes
        |
        +--> engine/master.py    validates and selects relevant private references
        |
        v
engine/knowledge.py              one serving entry point
        |
        +--> own-data typed AST planner: deterministic search; candidates run and
        |       grounded; the best-ranked one served; labelled Gemini fallback only
        |       when nothing runs
        |       engine/tables.py (select_query), engine/sql_search.py, engine/sql_rank.py,
        |       engine/sql_ast.py, engine/question_rewrite.py
        |
        +--> world grounding when a public entity relation is required
                engine/knowledge_query.py, engine/knowledge_compose.py (engine/routing.py decides)
        |
        v
supported shared-plan subset: bounded Python/SQL execution and optional parity
other shapes: SQL execution; explicit Python/verification rejects the fallback
        |
        v
engine/provenance.py + inspectable trace
```

The planner sees uploaded tables and selected reference tables in the same typed format.
`engine.relations` owns the relationship graph: `discover_fks` infers edges from request data, and
`relate(..., explicit_fks=...)` validates trusted edges from reference enrichment. Client payloads
cannot declare trusted edges. Public world joins are separate because they need entity-to-QID
grounding.

## Data And Isolation

One PostgreSQL database contains:

| Scope | Schema | Purpose |
|---|---|---|
| Curated shared serving projections | `knowledgebase`; `public.settlement` | Resolver index, taxonomy, Wikidata-derived entity and place tables, and the release-labelled ECB daily exchange-rate projection. These are derived runtime schemas, not source owners |
| Synchronized reference sources | `iana`, `cldr`, `google_libphonenumber`, `geonames`, `ecb`, `ec_tedb`, `nager_date`, `cdc`, `nlm_cde` | Immutable or bounded source snapshots. See `docs/SOURCE_DATA.md` |
| Conversation | `c_<32hex>` | Uploaded tables and world-resolution bridges for one authorized conversation |
| Application | `chat` | Conversation ownership, working-table manifests, named analyses, and immutable workbook revisions |
| User | `m_<md5(sub)>` | Private reference tables such as product-to-category or SKU-to-region |

The Google subject is verified server-side, and a conversation id is ownership-checked before it can
name a schema. A saved reference is never placed blindly on `search_path`: `engine.master.relevant_tables`
loads only references connected to the request by the foreign-key graph, and copies those bounded
rows into the planner's table set.

A reference table's first column is a non-empty, unique key; the other columns are attributes. The
browser saves changed references before a query, and the query stops if that save fails.

Storage is bounded and expires by inactivity: by default 1,000 conversations and 256 MiB of saved
state per user, 1 MiB per browser snapshot, 32 MiB per analysis snapshot, and 90 days of inactivity. A daily job
removes expired conversation schemas and traces.

The browser does not guess provenance from column names. The server returns one
`column_provenance` record per result column, with its operands and the publisher release IDs used.

## Install On A Local Machine

The full local installation runs the engine, the chat service, and PostgreSQL through Docker
Compose. Chat and the engine's Gemini fallback need a Google Cloud project with Vertex AI and your
application-default credentials (`gcloud auth application-default login`); set `GOOGLE_CLOUD_PROJECT`
and `EXTERNAL_LLM_ENABLED=true` in the local `.env` file. The engine alone needs neither.

Requirements:

- Python 3.11 (the supported local, CI, and container runtime)
- PostgreSQL 16 with `vector` and `pg_trgm`, or Docker
- runtime model artifacts in `engine/data/`

For planner, routing, enrichment, sync-parser, and migration work, the public CI dependencies are
enough:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --require-hashes -r requirements-ci-windows.lock.txt
$env:RUN_ENGINE_TESTS = "0"
$env:RUN_ORCHESTRATOR_TESTS = "0"
python -m tests.run_all
```

The full engine needs the model files. Fetch them, then seed the database before asking a question
that needs world facts:

```powershell
Copy-Item .env.example .env
# Edit .env and set: GOOGLE_CLOUD_PROJECT=... and EXTERNAL_LLM_ENABLED=true
python -m engine.fetch_weights
docker compose up -d db
docker compose --profile seed run --rm seed
docker compose up --build
```

Open the local UI at `http://localhost:8090`.

`engine.fetch_weights` downloads the public
[weight bundle](https://huggingface.co/prereasoner/prereasoner-weights) without a token and checks
every file against the revision and hashes in `engine/data/weights_manifest.json`. See
[engine/data/README.md](engine/data/README.md).

The guided Google Cloud deployment uses Vertex AI Gemini through its service account and does not ask
for a key.

Currency conversion uses the synchronized ECB release through the daily
`knowledgebase.exchange_rate` projection. Each published rate covers the following calendar days
until the next one, keeps its true ECB business date, joins each dated row on `(currency, date)`, and
reports the source release. A total or an average is converted row by row
(`SUM` or `AVG` of `amount * rate_to_target`). An uploaded rate table takes precedence, as the
user's own data.

Docker Compose exposes the engine on `http://localhost:8080` with the test-only principal
`localdev`. Once it is healthy:

```powershell
$body = @{
  tables = @(@{name="sales.csv"; data="product,amount`nHat,20`nCoat,80"})
  question = "total amount"
} | ConvertTo-Json -Depth 5
Invoke-RestMethod http://localhost:8080/api/reason -Method Post `
  -Headers @{Authorization="Bearer dev"} -ContentType application/json -Body $body
```

For native Python, the Firebase emulator, and database seeding, see
[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

## Tests

Fast checks that need neither weights nor Postgres:

```powershell
python -m tests.test_sql_ast
python -m tests.test_deterministic_emitters
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
node web/tests/workbook_reference.test.js
npm ci
npm run test:browser
python -m ruff check engine db deploy training tests orchestrator mcp_server regress spider world_eval --select F,E9
python -m compileall -q engine db deploy training tests orchestrator mcp_server regress spider world_eval
```

The repository-wide runner runs the hermetic suites, then the live suites whose prerequisites are
present:

```powershell
python -m tests.run_all
```

A skipped live suite is reported as a skip, not as a pass. Every demo workbook under
`web/public/dataset/` ships a `prompt.txt` and an `eval.txt` of follow-up questions with expected
answers derived from the CSV files; `python -m tests.test_datasets` runs them against a live database.

## Repository Map

| Path | Owner |
|---|---|
| `engine/` | Runtime typing, planning, grounding, execution, auth, conversations, and references |
| `engine/enrichment/` | Deterministic intent, source policy, request-local reference materialization, and replay manifests |
| `web/` | Workbook UI, Firebase Hosting configuration, demo datasets, and browser tests |
| `orchestrator/` | Optional chat service; it presents engine results and does not write numbers |
| `mcp_server/` | MCP adapter over the same engine API |
| `sheets-addon/`, `excel-addon/` | Google Sheets and Excel add-ons; they call the hosted chat service |
| `db/` | PostgreSQL schema, source synchronization, migrations, and grants |
| `training/` | Encoder and Schema.org head training and calibration; never imported by serving |
| `tests/` | Hermetic and live integration suites |
| `spider/` | Serving-faithful Spider evaluation and recorded results |
| `docs/` | Architecture, developer guides, testing, research, and training |

## Accuracy Boundary

Determinism does not remove ambiguity, incomplete schema linking, candidate-search limits, ranking
errors, missing world data, or wrong relationship inference. Accuracy work is split into measured
stages: routing, table selection, candidate-pool recall, top-1 ranking, execution, and evaluation.

With no model writing SQL and the Gemini fallback off, the engine alone answers **408 of 1,034** Spider DEV
questions (`whole_db`, gold-blind) and gets **243 strict (23.5%)** right, 60% of those it answers, measured
2026-10-04 at `4aa6ca6`. It refuses a question whose wording it cannot read rather than answer a different
one. Before that completeness check, at `60a55a3`, it answered 1,025 and got 497 right (48.1%). With the
operator's Gemini switch on, as production runs, a question the search cannot read is reworded once and
searched again: 609 answered and **338 strict (32.7%)**, with no answer lost to the rewording. With a 7B
SQL-writing model the engine scored 866. That gap is the cost of
interpretability the project chose: every query is built by the search, and accuracy grows by search and
ranking rules. Records and history are in
[spider/results/RESULTS.md](spider/results/RESULTS.md). `gold_tables` results are an oracle ablation, not a Spider comparison. See
[docs/SQL_AST.md](docs/SQL_AST.md).

## License

[Apache 2.0](LICENSE). Models, knowledge data, and dependencies keep their upstream terms; see
[THIRD_PARTY.md](THIRD_PARTY.md). Citation metadata is in [CITATION.cff](CITATION.cff).
