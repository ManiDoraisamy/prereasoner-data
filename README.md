# PreReasoner

**Interpretable question-answering for spreadsheets.** Upload a CSV, ask a plain-English
question, and get the answer *together with the reasoning that produced it* — the SQL that was
run, the view stack it was built from, and the world-knowledge joins it relied on. Nothing is a
guess: every number comes from SQL executed over your actual data, and you can read every step.

The thesis behind it: **reasoning should be inspectable, and an entity's *type* should emerge
from its schema.org *properties* — not be hard-coded.** A column of city names isn't labelled
"city" by a lookup table; it's recognized because its values fire the properties a place has
(coordinates, a country, a postal code). Type emerges from properties, and the answer emerges
from SQL you can audit.

Live at **[chat.prereasoner.com](https://chat.prereasoner.com)**.

---

## How it works

### The serving flow

Four cooperating services, each doing one job:

```
  You ── upload CSV(s) + a question ──▶  web/  (static UI on Firebase Hosting)
                                          │  Google sign-in; POST /chat
                                          ▼
                    orchestrator  "prereasoner-chat"  (Cloud Run — a Sonnet tool loop)
                                          │  spawns + drives, over stdio
                                          ▼
                    mcp_server  (MCP over stdio — the auditable engine as two tools)
                                          │  HTTP: POST /api/reason, /api/dimension …
                                          ▼
                    engine  "prereasoner-api"  (Cloud Run — a Python http.server)
                                          │
                                          ▼
                    Postgres  (one "knowledgebase" schema on Cloud SQL)
```

- **web/** — the static workbook UI (Firebase Hosting, no build step). Firebase rewrites `/chat`
  to the `prereasoner-chat` Cloud Run service and `/api/**` to `prereasoner-api`
  (`web/firebase.json`).
- **orchestrator/** (`prereasoner-chat`) — a chat backend that runs a **Sonnet tool loop**
  (`orchestrator/orchestrator.py`): it launches the MCP server as a stdio subprocess and lets
  Claude call the engine as a tool. Sonnet decides *when* to query and *how to phrase* the reply —
  but the model never produces a number; every figure comes back from the engine
  (`orchestrator/server.py`, `POST /chat`).
- **mcp_server/** — exposes the engine as two MCP tools: `prereasoner_query` (ask a question over
  tables) and `prereasoner_describe` (inspect column typing). Any MCP client inherits the same
  routing discipline (`mcp_server/server.py`).
- **engine/** (`prereasoner-api`) — the reasoning core: a single Python `http.server`
  (`engine/server.py`) that types columns, resolves values to world entities, assembles SQL, runs
  it, and streams the trace. Its endpoints:
  `POST /api/reason`, `/api/knowledge`, `/api/dimension`, `/api/converse`, `/api/master`; plus the
  conversation endpoints `GET /api/conversations`, `/api/conversation` and `GET /healthz`.
- **Postgres** — one database with the shared **`knowledgebase`** schema (the world knowledge),
  plus a per-conversation schema and a per-user master schema (see [Database](#database) below).

### Idea 1 — property-emergent column typing

When the engine sees a text column, it doesn't consult a hard-coded dictionary of type names. It
runs **one trained encoder** — a frozen Qwen2.5-0.5B with a LoRA adapter, feeding a trained
`RelationalModel` readout (`engine/encoder_model.py`) — that reads **schema.org property
dimensions** off the column's values.

The **family** is then *decoded by consensus* over that family's distinctive properties
(`engine/router.py`, `engine/data/families.json`): a place fires `GeoCoordinates`,
`addressCountry`, `postalCode`; a film fires `director`, `actor`, `genre`, `productionCompany`. A
column is assigned to a family when enough of that family's distinctive properties fire — each
gated by a per-property Youden-J threshold. There are **8 families**: `film`, `music`, `org`,
`organism`, `person`, `place`, `product`, `publication`.

A column that fires no family's distinctive properties **abstains** — it's a literal
(an amount, an id, a status). **Type emerges from properties**, so the model isn't limited to a
fixed list of hand-named types, and you can read *why* it typed a column the way it did via
`POST /api/dimension`.

### Idea 2 — inspectable SQL reasoning

The answer is never generated text. The engine turns question + tables into **SQL it executes on
your data**, and the derivation *is* the reasoning trace.

- **Own-data path** (`engine/tables.py: serve()`): for questions answered entirely from your
  uploaded tables, SQL is built either by the **compose engine** (a stack of simple, named views —
  filter, group, top-N, share, year-over-year …) or by the **deterministic typed-AST planner**.
  Production runs the AST planner: a bounded typed-AST search over the foreign-key graph with
  **hand-written, fully-inspectable ranking — no trained proposer or learned ranker**
  (`engine/tables.py: _serve_ast`). Measured in the **exact serving configuration** (top-1,
  `--max-candidates 25`) on Spider dev **gold-tables** it reaches **37.6% strict /
  57.6% scalar-gold** (`docs/SQL_AST.md` explains the measurement boundary; the whole-database config,
  which serving does not use, is lower and is pending a re-measure on this code).
- **World/knowledge path** (`engine/knowledge_query.py`, `engine/knowledge_compose.py`): when a
  question needs a fact you didn't upload — *"total amount in France"* over a sheet that only lists
  cities — text columns are typed by the property router, each value resolves through the
  `knowledgebase."words"` index to a Wikidata **QID**, and the engine **joins to a `knowledgebase`
  entity table** (`city → country`, `country → continent`) to filter on the world fact. The join
  is exact equality on the QID foreign key, not a string or similarity match.

Every step — each resolved QID and each view — is **streamed live** to the browser as it is
produced (`engine/trace.py`), so watching the reasoning happen is literal, not a reconstruction.

The full design is in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

### Database

One Postgres database holds a single shared serving schema plus per-scope schemas
(`db/init.sql`, `engine/pg.py`):

| Schema | Holds |
|---|---|
| **`knowledgebase`** | the shared world knowledge: `"words"` (surface → QID resolver, a bge-small pgvector index), `"types"` (QID → label taxonomy), and **QID-keyed entity tables** named by the exact Wikidata label (`"city"`, `"country"`, `"human"`, `"film"`, …). Entity tables start empty and **lazy-sync** rows from Wikidata the first time a QID is needed. |
| **`c_<32hex>`** | one schema per **conversation** — its uploaded tables and resolution bridges. The working schema for a run is the conversation, self-contained and archivable. |
| **`m_<md5(sub)>`** | one schema per **user** — their private "master" reference tables (their own products, regions, SKUs), persisting across conversations (`engine/master.py`). |

Each request runs `SET search_path TO "<schema>", knowledgebase, public`, so one query sees the
user's uploads, the shared world knowledge, and the geo tables together.

> The former separate `world` and `wikipedia` schemas were consolidated into **`knowledgebase`**
> and dropped. (The Postgres *database* is still named `world` by default — `KB_PG_DB=world` — but
> the serving *schema* is `knowledgebase`.) The name avoids "world model," which in ML means a
> learned dynamics model; this is a lookup knowledge base.

### Authentication & isolation

The engine verifies **Firebase ID tokens** (`engine/auth.py`). The identity is always the
server-verified Google `sub` — never anything the client sends. A request may carry a
`conversation_id`, but it is honored only after an ownership check against `chat.user_conversation`
(no IDOR); otherwise a fresh conversation is minted. Conversation ids double as schema names, so
they're validated against the strict `c_<32 hex>` shape before touching SQL. `POST /api/dimension`
is unauthenticated by design — it's stateless and stores nothing.

---

## Repository map

| Path | What it is |
|---|---|
| `engine/` | The reasoning engine — one Python `http.server`: column typing, entity resolution, SQL assembly, execution, trace streaming. Endpoints under `/api/*` + `/healthz`. |
| `orchestrator/` | The `prereasoner-chat` service — a Sonnet tool loop that drives the engine through MCP (`POST /chat`) and serves the chat UI locally. |
| `mcp_server/` | The engine exposed as MCP tools (`prereasoner_query`, `prereasoner_describe`) over stdio. |
| `web/` | The static workbook frontend — Firebase Hosting pages + rewrites + RTDB security rules. No build step. |
| `db/` | The `knowledgebase` Postgres: `init.sql` schema contract + `sync/` Wikidata bootstrap scripts. |
| `docs/` | Architecture, the typed SQL planner, training, research, and testing guides. |
| `tests/` | End-to-end suites (need a live, seeded Postgres) + the orchestrator/MCP tests. |
| `spider/` | Spider benchmark tooling: evaluation harness + recorded results. |

**Model weights are gitignored** — `encoder.pt` (~72 MB), `qwen_lora/` (~17 MB), and the `*.npz`
calibration artifacts must be provisioned into `engine/data/` separately. The per-file table (what
each artifact is, sizes, which are in git) is in
**[engine/data/README.md](engine/data/README.md)**.

---

## Quickstart

### 1. Configuration

Everything is environment variables, read in one place (`engine/config.py`). Copy the template and
fill it in:

```bash
cp .env.example .env
```

The core knobs:

| Variable | Purpose |
|---|---|
| `KB_PG_HOST` / `KB_PG_PORT` / `KB_PG_DB` / `KB_PG_USER` / `KB_PG_PASSWORD` / `KB_PG_SSLMODE` | Postgres (pgvector) connection. `KB_PG_PASSWORD` is required. |
| `ANTHROPIC_API_KEY` | Required only to run the **orchestrator** (the Sonnet tool loop) and the engine's optional `/api/converse` presentation layer. The engine's data path needs no key. |
| `RTDB_URL` | Optional Firebase Realtime DB URL for live trace streaming. Unset ⇒ streaming no-ops; the HTTP response still carries the full JSON answer. |
| `AUTH_TEST_SUB` | Test-only auth bypass (a fixed principal, skips token verification). Refused on Cloud Run. |
| `KB_MODEL_ROUTE` | `0` disables model-driven column routing (falls back to value-membership). Default on. |
| `DEVICE` / `BASE_MODEL_ID` | Torch device (`cpu` default) and the base encoder id (`Qwen/Qwen2.5-0.5B`). |

**Weights first.** Before the engine can serve, provision the gitignored artifacts into
`engine/data/` per [engine/data/README.md](engine/data/README.md) (`encoder.pt`,
`encoder_meta.pt`, `qwen_lora/`, the `*.npz` files).

### 2. Run the engine

The simplest path is Docker Compose — Postgres (pgvector) + the engine, with a one-time world-data
seed:

```bash
docker compose up --build                       # db + engine on http://localhost:8080
docker compose --profile seed run --rm seed     # one-time knowledgebase seed (~15-45 min)
```

Compose sets `AUTH_TEST_SUB=localdev` on the engine, so you can call it without a Firebase token:

```bash
curl -s localhost:8080/api/reason -X POST \
  -H "Content-Type: application/json" -H "Authorization: Bearer dev" \
  -d '{"tables":[{"name":"cities","data":"city,population\nParis,2100000\nLyon,520000"}],
       "question":"which city has the largest population?"}'
```

The response carries the full trace: the SQL, each view's rows, and the result. To run the whole
`browser → orchestrator → MCP → engine` loop locally, add `ANTHROPIC_API_KEY` to `.env` and bring
up the `chat` service (chat UI on `http://localhost:8090`). Running without Docker is covered in
**[docs/TESTING.md](docs/TESTING.md)**.

### 3. Tests

The suites in `tests/` run end-to-end against a live, seeded database (see
[docs/TESTING.md](docs/TESTING.md)):

```bash
python -m tests.run_all
```

---

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — how the whole system works, top to bottom.
- **[docs/TRAINING.md](docs/TRAINING.md)** — the training pipeline and how to add a new type.
- **[docs/SQL_AST.md](docs/SQL_AST.md)** — the deterministic typed-AST SQL planner: API, capability
  map, and Spider results.
- **[docs/RESEARCH.md](docs/RESEARCH.md)** — the research thesis and how it differs from RAG and
  agentic text-to-SQL.
- **[docs/TESTING.md](docs/TESTING.md)** — running the engine locally and the end-to-end suites.

## Citing

See [CITATION.cff](CITATION.cff).

## License

[Apache 2.0](LICENSE)
</content>
</invoke>
