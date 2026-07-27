# PreReasoner — Architecture

> **Read this to understand how the whole system fits together.** It is top-down: each section zooms
> in one level, and you can stop at any depth and still have a coherent picture. Every claim is
> anchored to a file path and, where it helps, the function that does the work.
>
> Sibling docs (kept separate on purpose): **[RESEARCH.md](RESEARCH.md)** = why the approach is novel
> (the research claims and their caveats). **[SQL_AST.md](SQL_AST.md)** = the deterministic typed-AST
> SQL planner and its Spider numbers. **[TRAINING.md](TRAINING.md)** = how the models were trained and
> how to reproduce them. **[../db/README.md](../db/README.md)** = the knowledgebase-database contract
> and how to bootstrap it. **[../web/README.md](../web/README.md)** = the frontend.
>
> The code in this repository was consolidated from an iterative research codebase into one set of
> packages with functional names; the internal iteration numbering does not appear here.

## Contents

1. [What it is](#1-what-it-is) · 2. [The one big idea](#2-the-one-big-idea) ·
3. [Serving topology](#3-serving-topology) · 4. [A question, end to end](#4-a-question-end-to-end) ·
5. [The engine request flow](#5-the-engine-request-flow) ·
6. [Column typing — property superposition-decode](#6-column-typing--property-superposition-decode) ·
7. [SQL generation over your own data](#7-sql-generation-over-your-own-data) ·
8. [The knowledge-join path](#8-the-knowledge-join-path) · 9. [The data model](#9-the-data-model) ·
10. [Auth & multi-tenancy](#10-auth--multi-tenancy) · 11. [Live trace streaming](#11-live-trace-streaming) ·
12. [The conversational layer (Sonnet)](#12-the-conversational-layer-sonnet) ·
13. [Model artifacts & training](#13-model-artifacts--training) · 14. [Glossary](#14-glossary) ·
15. [Repository layout](#15-repository-layout)

---

## 1. What it is

**The problem.** AI chatbots can't be trusted with real numbers. Paste a spreadsheet into a chatbot
and ask for a total and it may *guess* — and you can't tell whether it did the math or made it up. For
a business decision, "probably right" isn't good enough.

**What PreReasoner does.** You attach one or more CSVs and ask a plain-English question. It **writes a
precise SQL query, runs it on your actual data, and shows you both the answer and the query.** When the
question needs a fact you didn't upload (e.g. "in *France*" when your sheet only lists cities), it joins
your data to an implicit **Wikidata knowledgebase**. Because the answer comes from *executing SQL* — not
from a language model generating text — a wrong answer is a *traceable query*, never a hallucination.

**One sentence.** PreReasoner is an interpretable query-planning layer that turns *question +
spreadsheet(s)* into a *deterministic SQL query*, using a frozen LLM only as an encoder (never a
generator) and a small trained model whose hidden dimensions are named.

---

## 2. The one big idea

A normal "ask your data" tool built on RAG embeds everything into anonymous vectors and *approximately*
retrieves the nearest ones, then lets an LLM write the answer. PreReasoner does the opposite: it
**names** the dimensions of its representation (this column reads as a *place*; that one is a *literal
amount*) and then **queries them exactly** with SQL. (The full argument, and why this is *not* RAG, is
in [RESEARCH.md](RESEARCH.md).)

Two consequences shape the whole architecture:

- **No generation at inference in the engine.** The query is *assembled* by deterministic templates (or
  searched as a typed AST) and *executed*. The engine holds Qwen only as an **encoder** — it is run
  forward for hidden states, never `.generate()`d (`engine/tables.py: TableQuery._encode`).
- **The structure is split in two.** *What each column is* is **learned but readable** (named property
  dimensions read off one trained model, §6); *how the tables relate* (the joins) is **computed by a
  deterministic algorithm**, not learned (`engine/relations.py`). The model never invents which tables
  join — it is handed that and only weights it.

A conversational LLM (Sonnet) *does* exist in the system, but only at two edges — an optional chat
orchestrator in front of the engine (§3), and an optional in-rail "present / clarify" surface (§12).
Neither ever produces a number.

---

## 3. Serving topology

Production is **four tiers**, deployed as two Cloud Run services behind Firebase Hosting, over one
Postgres knowledgebase on Cloud SQL.

```
                 +-----------------------------------------------+
    User ------> |  Browser app   (web/public/, Firebase Hosting)|  static pages: index, reason,
                 |  attach CSV(s) + chat / ask a question         |  knowledge, chatui, sheets, admin
                 +----+--------------------------------^----------+
                      |                                 |
   Google sign-in     |  POST /chat  (chat UI)          |  live trace: subscribe
   (Firebase Auth)    |  POST /api/reason (reason page) |  /runs/{uid}/{jobId | turnId}
                      |  + Bearer <Firebase ID token>   |
                      |                                 |          +------------------------+
                      |                       (best-effort writes) |  Firebase Realtime DB  | (OPTIONAL, §11)
   ============ Firebase Hosting rewrites (web/firebase.json) ==== +-----------^------------+
        /chat  -> prereasoner-chat        /api/** -> prereasoner-api          |
                      |                                 |                      |
          +-----------v-----------+                     |                      |
          |  ORCHESTRATOR         |  Cloud Run          |                      |
          |  prereasoner-chat     |  (orchestrator/)    |                      |
          |  Sonnet tool-loop     |                     |                      |
          +-----------+-----------+                     |                      |
                      | spawns per session (stdio)      |                      |
          +-----------v-----------+                     |                      |
          |  MCP SERVER           |  subprocess         |                      |
          |  prereasoner_query    |  (mcp_server/)      |                      |
          |  prereasoner_describe |                     |                      |
          +-----------+-----------+                     |                      |
                      |  HTTP POST /api/reason           |                      |
                      +-----------------+----------------+                      |
                                        v                                       |
                            +-----------------------+   emit(node,value) -------+
                            |  ENGINE               |  Cloud Run (engine/)
                            |  prereasoner-api      |  python http.server; encoder-only
                            |  /api/reason etc.     |  model on CPU; stateless per request
                            +-----------+-----------+
                                        | SQL (SELECT-only) + lazy ensure_entity(qid)
                            +-----------v-----------+       +-----------------+
                            | Postgres knowledgebase|<----->| Wikidata (WDQS) |
                            | (Cloud SQL)           |       | fills rows on   |
                            | knowledgebase / public|       | demand          |
                            | / chat / per-conv / m_|       +-----------------+
                            +-----------------------+
```

**The two paths into the engine.** The reason/knowledge *pages* (`web/public/reason.html`,
`knowledge.html`) POST directly to `/api/reason` on the engine — this is the classic single-question
demo. The *chat UI* (`web/public/chatui.html`) POSTs to `/chat` on the **orchestrator**, which runs a
Sonnet tool-loop that decides when to call the engine and rewrites shorthand into standalone questions.
Both ultimately reach the same engine endpoint and stream the same reasoning trace.

**The four tiers.**

- **Browser app** (`web/`) — the **workbook**: the user's tables and every reasoning step appear as
  spreadsheet tabs, with a chat rail. Plain HTML/JS, no build step. It renders the reasoning trace live
  as it streams (§11). Served by Firebase Hosting, which rewrites `/chat` → `prereasoner-chat` and
  `/api/**` → `prereasoner-api` (`web/firebase.json`).
- **Orchestrator** (`orchestrator/`, service `prereasoner-chat`) — the chat backend. `orchestrator/
  server.py` handles `POST /chat`; `orchestrator/orchestrator.py: run_chat` runs a **manual Anthropic
  tool loop** (Sonnet, `claude-sonnet-5` by default) over the PreReasoner MCP tools. It is deliberately
  a manual loop, not the SDK tool-runner, so it can mint a per-call `jobId`, inject the session `tables`
  (kept out of the LLM's context — the model only ever sees the `question`), and keep the full `views`
  stack for the reasoning player while feeding the model only a trimmed result. `orchestrator/server.py`
  also proxies `/api/**` straight to the engine and, in local dev, serves the static UI from one origin.
- **MCP server** (`mcp_server/`) — a **stdio** MCP server (`mcp_server/server.py`, `FastMCP`) exposing
  two tools, `prereasoner_query` and `prereasoner_describe` (`mcp_server/descriptions.py`). The
  orchestrator spawns one per chat session over stdio, injecting the user's Firebase token into the
  subprocess env (`ENGINE_BEARER_TOKEN`) — identity passthrough, never a tool argument.
  `mcp_server/engine_client.py` makes the actual HTTP call to the engine and shapes the response to a
  stable `{status: answered|clarify|error, answer, sql, ...}`. There is no separate MCP Cloud Run
  service: the MCP server ships **inside the orchestrator image** and runs as its subprocess.
- **Engine** (`engine/`, service `prereasoner-api`) — the model + planner + SQL executor. `engine/
  server.py` is a plain `http.server.ThreadingHTTPServer` (no web framework). Encoder-only model on CPU,
  stateless per request. Request limits: 10 MB body, 8 sheets, 5,000 rows/sheet.

**Postgres knowledgebase** (Cloud SQL) holds the shared serving schema `knowledgebase`, the raw geo
import `public`, the `chat` ownership schema, one schema per conversation (`c_<32hex>`), and one master
schema per user (`m_<md5(sub)>`) — see §9. Contract in [`db/README.md`](../db/README.md).

**Wikidata (WDQS).** The faithful entity tables start empty and **lazy-sync** rows from Wikidata on
demand (`engine/knowledge_sync.py: ensure_entity` / `lazy_resolve`), the first time a resolved QID is
needed — not a bulk offline import on the hot path.

All runtime configuration is environment variables, read in one place (`engine/config.py`): `HOST`/
`PORT`, `KB_PG_HOST/PORT/DB/USER/PASSWORD/SSLMODE` (the knowledgebase Postgres), `RTDB_URL` (optional,
§11), `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL` (Sonnet, §12), `ENGINE_BASE_URL`/`ORCH_HOST`/`ORCH_PORT`/
`MCP_SERVER_CMD`/`ENGINE_BEARER_TOKEN` (the MCP tier), `PREREASONER_DATA_DIR` (artifacts, §13), `DEVICE`,
`BASE_MODEL_ID`, `KB_MODEL_ROUTE`, and the test-only `AUTH_TEST_SUB` (§10).

---

## 4. A question, end to end

The canonical demo: tables `customers.csv` + `orders.csv`, question **"total amount in France"**.
`customers` has a `city` column (Paris/Lyon/Berlin) but no country. The answer is **270** (Paris + Lyon
orders; Berlin resolves to a German city and is excluded).

Below is the *direct* path (reason page → engine). The chat path is the same, wrapped in a Sonnet loop:
§4.7.

1. **Upload + submit.** The browser saves the sheets + question, signs the user in (Firebase Google),
   gets a fresh ID token, generates a `jobId`, and does `POST /api/reason` with `Authorization: Bearer
   <token>` and body `{tables, question, jobId, conversation_id?}`. It renders the Input slide instantly
   and subscribes to `/runs/{uid}/{jobId}` for the live trace (§11).
2. **Auth gate** (`engine/server.py: _post_world` → `engine/auth.py: _verify_principal`). Firebase Admin
   verifies the ID token server-side and returns both the Google `sub` (identity) and the Firebase `uid`
   (trace-stream key). No/invalid token → **401**.
3. **Resolve the working schema** (`engine/conversations.py: resolve_conversation`). The working Postgres
   schema is the **conversation**, not the user. A client-supplied `conversation_id` is honored *only
   after* an ownership check against `chat.user_conversation`; otherwise a fresh `c_<32hex>` conversation
   is minted for the verified user (§10). The `conversation_id` is streamed to RTDB immediately, so the
   browser learns it even if the HTTP body is later lost to a proxy timeout.
4. **Serve** (`engine/knowledge.py: KnowledgeReasoner.serve`, under a process-wide `WORLD_LOCK`). This is
   the one shared model instance. It first checks the **coverage pre-gate** (is this even a data query?
   §12), then delegates down the planner stack (§5). For "total amount in France":
   - **Typing/routing.** FK discovery finds `orders.customer_id → customers.customer_id`
     (deterministic, `engine/relations.py`). The property router (`engine/router.py: Router`) reads
     `customers.city` and decodes the **place** family; grounding then confirms the cells resolve as
     cities (`engine/knowledge_query.py: KnowledgeQuery.route` / `_grounds`, §6). The word "France" is
     recognized as a country filter (`engine/entities.py: _resolve`).
   - **Operator from the model.** `intent_agg_*` dims read off the question tokens give `SUM`
     (`engine/encoder_overlay.py: read_op_model` / `read_op_all`) — no keyword list.
5. **Resolve cells to QIDs + persist the bridge.** Each city cell resolves through `knowledgebase.
   "words"` (exact normalized match first, else bge-small pgvector cosine NN) to a city QID (Paris →
   Q90, Lyon → Q456, Berlin → Q64). If `knowledgebase."city"` has no row yet, `ensure_entity(qid, "Q515")`
   lazily fetches the faithful Wikidata row (item-property columns kept as QIDs — `city.country` is the
   country's QID) and INSERTs it, cascading the country fill. The resolved column→value→QID mapping is
   persisted in the per-conversation bridge `"customers connected to wikipedia"`
   (`engine/entities.py: _city_bridge_sql`, `engine/knowledge_query.py: _persist_connected`).
6. **Assemble + execute SQL** (Postgres, `search_path = "<conv>", knowledgebase, public`). JOIN the
   upload to `knowledgebase."city"` via the bridge, filter on the **QID foreign key**
   (`city.country = 'Q142'` = France), aggregate `SUM(orders.amount)`. A **SELECT-only guard** rejects
   anything but a read. Result = **270**.
7. **Re-express as a view stack + respond + stream.** A clean world-filtered scalar is re-expressed as
   the composed view stack so the demo *shows* the steps (resolve → world join → filter → aggregate)
   rather than jumping to the number (`engine/knowledge_compose.py: ComposedKnowledgeQuery.serve`). The
   HTTP body carries the answer + the SQL; in parallel each stage was streamed to RTDB as it completed
   (§11).

**A question that grounds a WORLD dependency AND composes over it** — *"top 3 cities by population"*,
*"total amount in Europe by city"*, world year-over-year / share / running total — is decomposed by
`ComposeEngine` (`engine/compose.py`) into a DAG of simple primitive views over the FK + knowledge base,
each streamed as it materializes (§7). Routing authority is a *grounded* world dependency, read off the
built plan (`engine/routing.py: compose_owns`), never the primitive-head prediction alone. A question
answerable from the uploaded tables ALONE — including own-data HAVING and group-by — is owned by the
typed-AST planner; the self-contained (no-world) year-over-year / running / share / divide variants were
retired (untested and weaker than the planner).

### 4.1–4.6 The engine endpoints

`engine/server.py` routes these (all under one `ThreadingHTTPServer`):

| Endpoint | Method | Auth | What it does |
|---|---|---|---|
| `/api/reason` | POST | Firebase | The composition reasoner on the live path (the §4 walk-through). |
| `/api/knowledge` | POST | Firebase | The knowledge-join path run directly (§8). Shares the **one** `KnowledgeReasoner` + `WORLD_LOCK` with `/api/reason`. |
| `/api/dimension` | POST | none | Stateless per-column / per-cell named-dimension readout (`engine/dimension.py: DimensionModel.analyze`). No Postgres, nothing persisted; its own model + `DIM_LOCK`. |
| `/api/converse` | POST | Firebase | The Sonnet conversational fallback / presentation (§12). One Anthropic call; 503 if `ANTHROPIC_API_KEY` is unset. |
| `/api/conversations`, `/api/conversation?id=` | GET | Firebase | The signed-in user's conversation list / re-open one (ownership-scoped, §10). |
| `/api/conversation/state`, `/delete`, `/delete-all` | POST | Firebase | Persist a renderable snapshot so a reload restores the conversation without re-running; delete conversations (§10). |
| `/api/master`, `/api/master/delete` | GET/POST | Firebase | Per-user master (reference) tables (`engine/master.py`, §9). |
| `/api/admin/*` | GET/POST | admin allowlist | Admin dashboard (`engine/admin.py`): list/delete users, conversations, orphan schemas. |
| `/healthz`, `/api/healthz` | GET | none | Liveness + whether both models finished loading. The frontend fires a warm-up GET because the service scales to zero and the first cold hit must load the model. |

### 4.7 The chat path (orchestrator wrapping)

For the chat UI, `POST /chat` → `orchestrator/orchestrator.py: run_chat`:

1. Auth-gate on the same Firebase token (paid Sonnet inference must not be anonymous — denial-of-wallet).
2. Spawn the MCP server over stdio with the token in its env.
3. Run the Sonnet tool loop (`MAX_TOOL_ROUNDS = 8`). The system prompt (`orchestrator/system_prompt.py`)
   and the tool descriptions (`mcp_server/descriptions.py`) both carry the **routing discipline**: *any*
   factual number must come from a `prereasoner_query` call — never Sonnet's own arithmetic or memory;
   follow-on math is another call; a `clarify` result is surfaced verbatim, not filled in.
4. For each `prereasoner_query` tool call, the orchestrator mints a derivable `jobId` (`<turnId>_<i>`),
   announces it on the turn's RTDB node *before* running, injects the session `tables`, and calls the MCP
   tool (→ `engine_client.call_query` → `POST /api/reason`). It keeps **one** conversation for the whole
   session (captured from the first call's `conversation_id`), returns the trimmed value to Sonnet, and
   keeps the full trace for the browser.
5. Returns `{reply, traces, history, conversation_id}`; Sonnet's plain-English reply is the only thing
   the user reads.

---

## 5. The engine request flow

The planner is a **class chain** — every layer adds one capability over the one below, and each is
independently readable. The serving entry points compose the whole stack, and (crucially) **the encoder
is loaded once and shared across every layer** (one Qwen in memory).

```
engine/tables.py            TableQuery          base: CSV parse, SQL identifier/literal quoting, the
                                                deterministic typed-AST planner over the user's own
                                                tables (SQLite for the local path)
engine/knowledge_tables.py  KnowledgeTableQuery  the implicit knowledgebase word-tables + meaning graph
engine/pg.py                PgQuery             Postgres execution: per-user/conversation schema load,
                                                type affinity (INTEGER->BIGINT, NUMERIC->py), SELECT-only
engine/resolve_base.py      RoutedQuery         generalized routing + country aliases + states/elements
engine/entities.py          EntityQuery         embedding entity resolution: knowledgebase."words"
                                                exact-norm match then pgvector cosine NN; cell bridges;
                                                lazy city fill
engine/encoder_overlay.py   EncoderQuery        the unified-encoder overlay: loads encoder_meta.pt /
                                                encoder.pt / qwen_lora (load_encoder — the ONE loader),
                                                operator-from-intent, shares the model onto every layer
engine/knowledge_query.py   KnowledgeQuery      the live knowledge path: route -> resolve -> QID join ->
                                                QID-FK filter -> aggregate; hybrid semantic SQL; clarify
engine/knowledge_compose.py ComposedKnowledgeQuery  view-stacking composition over the knowledge base;
                                                delegates non-composed queries to KnowledgeQuery
engine/knowledge.py         KnowledgeReasoner   + geo NEARBY (haversine over public.settlement); the
                                                coverage pre-gate + the present tag; the top serve()
engine/dimension.py         DimensionModel      the stateless /api/dimension readout (EncoderQuery subclass)
```

**How a question becomes an answer** (`KnowledgeReasoner.serve` → down the chain):

1. **Ingest** (`TableQuery.ingest` → `engine/relations.py: relate`): dedup rows + **deterministic FK
   discovery** (inclusion-dependency ≥90%, many-to-one cardinality, name/type heuristics, a confidence
   gate). No model.
2. **Schema + column typing** (`TableQuery.schema`, `engine/router.py: Router`): encode each unit (a
   column name / a cell value / a question token) with the shared LoRA-Qwen, mean-pool its subtokens to
   one vector, run the relational readout, read the named dims. Datatype affinity (INTEGER/REAL/TEXT) and
   the **property family** are read here (§6).
3. **Plan** — one of two routes (§7):
   - the **compose engine** view-stacks (`engine/compose.py: ComposeEngine`) for composition depth or a
     multi-table/knowledge base, driven by the learned primitive head; or
   - the **deterministic typed-AST planner** (`TableQuery.search_ast` / `_serve_ast`) for general
     single-relation questions.
4. **Resolve + join to the knowledgebase** where the question names a place/type the sheet lacks (§8).
5. **Guard + execute** (`TableQuery.guard` SELECT-only; `engine/pg.py` on Postgres for the live path,
   in-memory SQLite for the local/compose path) → `{columns, rows}`.
6. **Trace** each step to RTDB as it completes (§11), and return the answer + the SQL + the step stack.

`TableQuery` on its own loads **no** model weights — the encoder overlay (`engine/encoder_overlay.py:
load_encoder`) supplies the one shipped model to the whole stack, so the process holds a single Qwen used
by routing, resolution, and the intent readout alike.

Supporting modules: `engine/primitives.py` (pure SQL view builders), `engine/primitive_head.py`
(`PrimitiveReader`, the learned 10-primitive head on the same encoder), `engine/embeddings.py` (the
bge-small retrieval embedder + surface normalization), `engine/bridge.py` (bridge predicate machinery),
`engine/fk_edges.py` + `engine/graph_walk.py` (the relational edge graph), `engine/knowledge_sync.py`
(the lazy Wikidata client), `engine/trace.py` (§11), `engine/auth.py` (§10), `engine/config.py` (env).

---

## 6. Column typing — property superposition-decode

> This is the recent rewrite. It **replaces** the older taxonomy-leaf typing (which anchored one hidden
> dim per Wikidata `P279` node and read a column's type off the leaf that fired). That taxonomy-path
> readout is gone; nothing is anchored as a "type" any more. **The type now emerges from properties.**

**The idea (Mani's thesis: an interpretable model is a database of properties).** The one trained model
reads **schema.org PROPERTY dimensions** per column — a *superposition* of property activations — and the
entity **family** is **decoded by column consensus** over that firing: the fraction of a family's
*distinctive* properties that fire, calibrated by per-property **Youden-J thresholds**. A column that
fires no family's distinctive properties above a floor is a **literal** (an amount / id / status) and
**abstains** — no type is forced onto it.

**The encoder is one model, shared everywhere** (`engine/encoder_overlay.py: load_encoder`):

- a **frozen-base Qwen2.5-0.5B + a LoRA adapter** (`engine/data/qwen_lora/`, `BASE_MODEL_ID =
  Qwen/Qwen2.5-0.5B`, hidden size 896), producing one mean-pooled vector per unit
  (`TableQuery._encode`); feeding
- the trained **`RelationalModel`** (`engine/encoder_model.py`) — a 10-layer bidirectional relational-
  attention transformer whose **named dimensions** are read off the last `nc` hidden coordinates. Weights
  ship as a plain `state_dict` (`encoder.pt`) plus `{alloc, cfg}` (`encoder_meta.pt`), so loading never
  depends on pickled class paths.

**The dimension allocation — 86 content dims** (`engine/data/alloc.json`):

| Group | Count | What it names |
|---|---:|---|
| **struct** | 9 | `is_str` / `is_num` / `num_frac` / `is_time` / `is_bool` / `is_enum` / `is_key` / `is_ref` / `currency` (the datatype shape) |
| **taxonomy** | 67 | one 0/1 dim per **schema.org property** (`director`, `birthDate`, `taxonName`, `addressCountry`, `byArtist`, …) — the property superposition a column fires |
| **intent** | 10 | `intent_agg_{sum,count,avg}`, `filter_{eq,gt,lt}`, `group`, `sort_{desc,asc}`, `limit` — the query's operation |

(The `family="taxonomy"` group name is a historical artifact of the dim-allocation schema; those 67 dims
now hold **properties**, not taxonomy nodes.)

**The router** (`engine/router.py: Router`). For a column it builds a units graph (header + up to 40
value units, `same_col` edges) → encodes with the shared LoRA-Qwen → relational content readout → **means
the per-dim readout over the value units** = the column's *property profile*. Then `_consensus` scores
each family = the fraction of its distinctive props (from `families.json`) that fire above their
`props_thr.json` threshold. `route()` returns the argmax family, or **None** (abstain) when the best
family fires `< 0.40` of its distinctive props. It reuses the already-loaded encoder+readout — one Qwen
for operator, bridge, *and* typing.

**The 8 families** (`engine/data/families.json`): `place` (geo=true; grounds to `city` / `country` /
`administrative territorial entity` / `hospital` / `school` / …), `person` (`human`), `org`
(`organization` / `corporation` / `NGO` / `political party` / `credit institution`), `film` (`Movie`),
`music` (`MusicGroup` / `MusicAlbum` / `MusicComposition` / `MusicRecording`), `publication` (`periodical`
/ `book` / `creative work`), `product` (`product` / `application software` / `vehicle` / `website`),
`organism` (`taxon`). Each family carries its distinctive property list, a `geo` flag, and the candidate
world tables.

**Two-tier by design.** The router gives the **coarse family** — that is what gates *entity-vs-literal*
and primes the resolver. The **fine table + QID** come from cell resolution against the knowledgebase
(`engine/knowledge_query.py: _dominant_nongeo_type` picks the dominant `knowledgebase."words"` type the
cells actually resolve to). So the model proposes; grounding decides. A city false-positive on a
first-name column (short names like Ada/Bo/Sam are real cities and can fire the place family) is dropped
because the *names* don't ground — never a wrong answer. Grounding uses `GROUND_FRAC = 0.8` for geo types
(`KnowledgeQuery._grounds`).

Set `KB_MODEL_ROUTE=0` to disable model routing entirely and fall back to pure value-membership routing
(`engine/entities.py: _value_membership_routes`), so the live demo can never hard-break on a model
regression. On any router exception the code logs loudly and falls back the same way.

**Operator from intent** (`engine/encoder_overlay.py: read_op_model`). The `intent_agg_{sum,count,avg}`
dims are read off the *question* tokens (operand tokens — column names + cell values — excluded), gated
against calibrated thresholds (`load_encoder` sets SUM/AVG at 0.30, COUNT at 0.05), so "how much did we
sell" yields `SUM` without any keyword list. Keyword heuristics survive only as an encoder-free fallback
so the compose engine stays testable without a model (`engine/compose.py`, `TableQuery.plan`'s
`AGG_CUES`).

**The relational structure (the deterministic, auditable part).** Relationships between units are built
**outside** the network and injected as a fixed prior; the network only learns *how strongly* to weight
each one. Edge types `same_col` / `same_row` / `same_cell` / `self` / the query/SQL edges / and the
cross-table `fk` edge form an integer adjacency matrix (`engine/fk_edges.py: edges`, 10 edge types). The
model's *only* relational parameter is a per-edge-type × per-head additive attention bias
(`RelBlock: att = att + eb[edges]`, `engine/encoder_model.py`). It learns how much to attend along a
*given* edge — never *which* units relate.

---

## 7. SQL generation over your own data

The **typed-AST planner (§7.2) owns every query answerable from the uploaded tables alone.** The compose
engine (§7.1) is a *world-enrichment / analytical-lowering* component — the shared router
(`engine/routing.py: compose_owns`) hands it a query ONLY when the built plan grounds a world dependency
(`world_join` / `world_filter`). Both are deterministic; neither samples SQL tokens.

### 7.1 The compose engine (world-grounded view stacking)

`engine/compose.py: ComposeEngine` decomposes an analytical question into a **DAG of simple primitive
views**, each materialized as a SQL view, stacked over a base relation (the uploaded FK join + the
knowledge-meaning join). The primitives:

```
EXCL   categorical row exclusion ("excluding returns")
RATIO  year-over-year growth
TOPN   top-N
SHARE  share of total (percentage/proportion)
TIME   time-window filter
HAVING post-aggregate predicate
SORT   ranking (rank XOR top-N)
DIVIDE ratio of two measures
RUNNING cumulative / running total
GROUP  group-by a dimension
```

These are the primitive-head cues that make it worth *building* a compose plan — they are **evidence, not
authority**. The engine only *stands* on that plan when it grounds a world dependency (`compose_owns`).
HAVING and GROUP over own data are handled by the typed-AST planner; RATIO/SHARE/DIVIDE/RUNNING are
supported as world-grounded composites (the self-contained variants were retired).

Which primitives are present is read off the **learned 10-primitive head** (`engine/primitive_head.py:
PrimitiveReader.present`) on the **same** unified encoder, OR'd with cheap lexical/operand cues for rare
synonyms (`ComposeEngine._decompose`). `engine/primitives.py` holds the pure SQL view builders; the DAG
runs on SQLite for the composed path, joined to the in-memory knowledge-meaning table
(`ComposedKnowledgeQuery._world_lookup`). `ComposedKnowledgeQuery.DEPTH_PRIMS` is the set that gates a
question to the engine; `serve()` **stands on** the engine's result only when it actually built a genuine
composition or knowledge composite (`_ENGINE_ONLY_VIEWS` always stand; `_SLOT_OVERLAP_VIEWS` — top-N /
sort / time-filter — stand only when a knowledge join is in the stack), otherwise it defers to the
authoritative delegate. This context-aware routing is what keeps live knowledge composites working
without regressing the simple single-relation paths.

### 7.2 The deterministic typed-AST planner

This is the **one own-data SQL planner**. For general single-relation questions, `TableQuery.search_ast`
searches a bounded space of valid SQL abstract syntax trees rather than filling slots, and
`TableQuery._serve_ast` selects `candidates[0]`. It runs unconditionally — there is no planner-mode
toggle.

Pipeline: `engine/sql_schema.py` builds the typed FK graph → `engine/sql_search.py` (`SQLSearcher`) runs
bounded search → capability modules add recursive queries, constraints, extrema, and set operations
(`engine/sql_recursive.py`, `sql_constraints.py`, `sql_extrema.py`, `sql_expansion.py`,
`sql_candidate.py`) → candidate ASTs are scored by **hand-written, fully-inspectable deterministic
features** (`engine/sql_rank.py`, with structural profiles in `sql_profile.py` and optional deterministic
candidate expansion in `sql_profile_expansion.py`) → only validated ASTs (`engine/sql_ast.py`) are
rendered to SQL. The ranking is entirely hand-written; there is no trained proposer or learned ranker.

Measured in the **exact serving config** (top-1, `--max-candidates 25`), the deterministic planner scores
**30.3% strict / 38.9% lenient / 50.0% scalar-gold** (313/1034, 402/1034, 204/408) on **standard Spider
dev** — the gold-blind `whole_db` config that feeds every table, the number to compare against other
Spider systems. In the **oracle-table-selection** `gold_tables` config (only the gold-referenced tables
fed, the product analogue) it scores **37.6% strict / 49.2% lenient / 57.6% scalar-gold** (389/1034,
509/1034, 235/408). The measurement boundary, capability map, and API are in
[`docs/SQL_AST.md`](SQL_AST.md).

---

## 8. The knowledge-join path

This is how text columns resolve to Wikidata entities and join — the machinery behind "total amount in
France". Two halves cooperate: the **words index** (metric resolution) and the **property router**
(typing, §6).

**How a value reaches the knowledgebase — the family picks the table, the QID picks the row.**

1. **Type** the column to a family, gated by grounding, giving a world type + its `knowledgebase."<type>"`
   table (`engine/knowledge_query.py: route` / `_dominant_nongeo_type`, §6).
2. **Resolve** each cell to a **QID** via `knowledgebase."words"`: exact normalized match first, then
   bge-small pgvector **cosine NN** (`engine/entities.py: _resolve` / `_nn`; the cell-side bridge
   `_cell_bridge_sql` / `_city_bridge_sql` runs the fuzzy remainder as an in-Postgres `<=>` LATERAL NN so
   the similarity search happens in the database). Cities resolve with same-name disambiguation
   (context country → global `is_primary` → population), and a per-row country column disambiguates two
   "Paris" rows (`_city_bridge_disamb_sql`).
3. **Lazy-fill** the row if missing: `engine/knowledge_sync.py: ensure_entity(qid, type_qid)` fetches the
   faithful Wikidata row from WDQS (item-property columns kept as **QIDs**) and INSERTs it, cascading the
   `country` FK so 2-hop filters work. `lazy_resolve` handles a value not yet in `words` at all.
4. **Join on the QID FK** — `JOIN knowledgebase."city" ON qid = bridge.world_key`,
   `WHERE city.country = 'Q142'` — exact equality, no string match at join time. A 2-hop knowledge fact
   ("total amount in Europe") is two QID FK hops: `city.country` → `country.continent = 'Q46'`.

The resolved mapping is persisted per conversation in **`"<table> connected to wikipedia"`** (column /
value / world_type / world_key / country / world_qid) so re-runs don't re-resolve
(`KnowledgeQuery._persist_connected`). A second bridge **`"<table> unconnected to wikipedia"`** holds a
unified-encoder `vector(896)` per free-text cell, backing the semantic predicate.

**Two extras live at this layer:**

- **Geo NEARBY** (`engine/knowledge.py: KnowledgeReasoner._nearby`): "big cities near Paris" resolves the
  reference city to lat/lng (`public.settlement`) and ranks knowledge cities by **haversine distance in
  plain SQL** — no PostGIS.
- **Hybrid structured + semantic queries** (`engine/knowledge_query.py: _serve_hybrid`): "who complained
  about *bad delivery* in France" combines an exact knowledge filter (`country = France` via the resolved
  QID, read from the connected bridge) with a pgvector cosine predicate over the free-text unconnected
  bridge — both sides embedded by the *same* unified encoder, so the `<=>` cosine is a valid same-space
  comparison. This is why the encoder had to be unified.

**The clarify gate** (`engine/knowledge_query.py: _uncovered` / `_clarify`). A query that would silently
drop part of the question — an entity that **resolved** but isn't filtered, or a measure word with **no
aggregate** applied — is caught instead of answered wrong, and the engine returns a `clarify` response (a
best-guess rephrasing from the model's sub-threshold signals). Coverage is checked against the resolved
**QID** in the QID-keyed SQL. The clarify is answered in the chat rail by the conversational layer (§12),
not a page redirect. A sibling **coverage pre-gate** (`ComposedKnowledgeQuery._has_data_signal`, applied
in `KnowledgeReasoner.serve`) catches the opposite case — a message with *no* data intent at all ("how
does this work?") — before any reasoning runs.

---

## 9. The data model

**One Postgres database** (`KB_PG_DB`, default `world`) holds four schema families. DDL + bootstrap in
[`db/init.sql`](../db/init.sql) and the [`db/sync`](../db/README.md) pipeline.

| Schema | Holds | Built by |
|---|---|---|
| **`public`** | the raw Wikidata geo/type import (`settlement`, `country`, `admin`, `continent`, `currency`, `element`, `timezone`, `entity_label`) — read directly for geo NEARBY | `db/init.sql` + `db/sync/sync_wikidata.py` / `import_dump.py` |
| **`knowledgebase`** | THE shared serving schema — the resolution index, the taxonomy, the qid-keyed faithful Wikidata tables, and the friendly geo tables/views | `db/sync/build_words.py`, `sync_types.py`, `build_world.py`, `build_wikipedia.py`; filled lazily by `engine/knowledge_sync.py` |
| **`chat`** | conversation identity + ownership: `user_profile`, `conversation`, `user_conversation` | `engine/conversations.py` (idempotent at runtime) + `db/init.sql` |
| **`c_<32hex>` / `m_<md5(sub)>`** | per **conversation**: uploaded CSVs + the two bridges. per **user**: master (reference) tables | the engine, at request time (`engine/pg.py`, `engine/master.py`) |

> **Naming note.** The shared schema is called **`knowledgebase`**, *not* `world` or `wikipedia` — because
> "world model" means a learned dynamics model in ML, and this is a lookup KB. Older names (`world`,
> `wikipedia`, `/api/world`) are gone; the DB schema and all tables use `knowledgebase`.

Inside `knowledgebase`:

```text
-- resolution + taxonomy index
knowledgebase."words"  ( surface, canonical, type, props jsonb, norm, embedding vector(384),
                         qid, canon_country, is_primary )   -- exact-norm match, then pgvector cosine NN (HNSW)
knowledgebase."types"  ( qid PK, label, parent_qid, is_leaf, world_table, depth, resolver_type )

-- qid-keyed faithful Wikidata tables — one per type, named by the EXACT Wikidata label,
-- created + filled LAZILY (start empty). Item-property columns hold related-entity QIDs (true FKs).
knowledgebase."city"     ( qid PK, name, country <FK->country.qid>, population, … )
knowledgebase."country"  ( qid PK, name, continent, currency, … )
knowledgebase."hospital" ( qid PK, name, country <FK->country.qid>, … )
… all qid-PK …

-- friendly denormalized geo tables + "… in the World" views (population ranking, ENTITY_ATTRS enrichment)
knowledgebase."Cities" / "Countries" / "Places" / "Elements" / "Continents" / "States"
```

Per-request namespace (`engine/pg.py: _load_user_schema`):

```text
"<conv>"."customers"                            -- the upload, re-created from the request
"<conv>"."customers connected to wikipedia"     -- resolved cell -> world qid (column/value/world_type/world_key/country/world_qid)
"<conv>"."customers unconnected to wikipedia"   -- a unified-encoder vector(896) per free-text cell
SET search_path TO "<conv>", knowledgebase, public   -- one query sees upload + knowledgebase + geo
```

**The `db/sync` pipeline** bootstraps the shared data: `sync_wikidata.py` (bulk geo import into `public`)
→ `build_world.py` / `build_words.py` / `sync_types.py` / `unify_words_qid.py` (build the `knowledgebase`
resolution index + taxonomy + friendly tables) → `build_wikipedia.py` / `mirror_schema.py` (optional
empty qid-PK type tables). `sync_entity.py` (`ensure_entity`) is the lazy per-entity filler the engine
calls at query time. `archive_conversation.py` `pg_dump`s a conversation schema to GCS (§10).

---

## 10. Auth & multi-tenancy

`/api/reason`, `/api/knowledge`, `/api/converse`, and the conversation/master/admin routes are gated by
Firebase Google auth. `/api/dimension` is unauthenticated **by design** — it is stateless and stores
nothing.

**Identity is always the verified token subject** (`engine/auth.py: _verify_principal` returns the
Google `sub` + the Firebase `uid`) — never anything the client sends. `AUTH_TEST_SUB` is a test-only
bypass that pins a fixed principal without verifying a token; it is **refused on Cloud Run** (a hard
guard on `K_SERVICE` in `engine/config.py: auth_test_sub`), so a stray env var can never disable auth in
production.

**A conversation owns a schema.** Each conversation gets its own Postgres schema (`c_<32hex>`, validated
against a strict regex before it ever reaches SQL / `DROP SCHEMA`), holding that conversation's uploads +
bridges. So a conversation is self-contained — inspectable on its own and archivable as a unit
(`engine/conversations.py`; `chat` metadata in three tables: `user_profile`, `conversation`,
`user_conversation`).

**Why this isn't an IDOR.** A request may carry a `conversation_id`, but it is honored **only after** an
ownership check against `chat.user_conversation`; otherwise the engine mints a fresh conversation
(`resolve_conversation`). A `conversation_id` that isn't yours (or doesn't exist) returns the same "not
found" either way — no enumeration.

- **Isolation is application-enforced.** The working schema comes from the authorized conversation;
  `search_path` scopes queries to it + the shared `knowledgebase`/`public`; generated SQL is SELECT-only
  with quoted identifiers. One Postgres role, separation by schema.
- **No concurrent-request deadlock.** The bridge read connection is `autocommit=True` (`entities.py:
  _rconn`), and the one `ACCESS EXCLUSIVE` bridge migration is guarded by an `information_schema` check so
  steady state takes no exclusive lock.

**Master data** (`engine/master.py`) is a per-user schema `m_<md5(sub)>` in the same database, holding
the private reference entities Wikidata doesn't know (products, reps, regions). The schema name is
*derived* from the verified sub, so a user can only ever read/write their own master data — no
client-controlled schema path.

**Archiving** (optional). Because a conversation is one self-contained schema, an idle one can be
`pg_dump`ed to `gs://$GCS_BUCKET/conversations/<id>.sql.gz` and restored with `psql`
(`db/sync/archive_conversation.py`); the `chat` metadata stays so it remains listed. Operator tooling; the
restore bucket is a trust boundary.

Note: the engine initializes firebase-admin (Application Default Credentials) for token verification even
when trace streaming is off, so the authenticated routes need Google credentials unless `AUTH_TEST_SUB`
is set locally.

---

## 11. Live trace streaming

The reasoning trace is **streamed live as the backend computes it**, decoupling rendering from the HTTP
response: a slow 2-hop geo query that would exceed a ~60 s proxy timeout still streams to the answer, and
"watch how the model reasons" becomes literal.

```
Browser (reason page / chat UI)      engine (or orchestrator)      Firebase RTDB /runs/{uid}/{jobId|turnId}
   | generate jobId; render Input slide instantly
   |-- POST {tables, question, jobId} + Bearer -->|  (fire-and-forget)
   |-- subscribe /runs/{uid}/{jobId} ------------------------------------->|
   |                                    | _verify_principal -> (sub, uid)
   |                                    |-- conversation_id ------------->| (early, survives a proxy timeout)
   |                                    |-- status: resolving ----------->|
   |                                    |-- resolve/{i}: {table,column,…}>|
   |                                    |-- views/{0..n}: {op,label,sql,columns,rows} -->|
   |                                    |-- result / present / clarify / low_confidence / error -->|
   |<------------------ each write pushed -> render slide ----------------|
   |<-- HTTP body still returns (full-response fallback) --|
```

- **Backend** (`engine/trace.py`). `emitter(uid, jobId) -> emit(node, value)`; the server emits `status`
  and the terminal state via `stream_final`, while the compose engine emits `status: resolving`, each
  `resolve/{i}` slide, and each `views/{i}` as it is built. A per-request emit **context** (`set_ctx` /
  `ctx_emit`) lets deep resolution code stream the cell→QID lookup without threading `emit` through every
  signature (safe because the server sets it inside the per-model `WORLD_LOCK`). firebase-admin (already
  present for auth) does the writes. Every write is best-effort — streaming must never break the answer.
- **RTDB is OPTIONAL.** Driven by `RTDB_URL`. Unset ⇒ the emitter functions become clean no-ops,
  firebase-admin still initializes for auth, and the frontend falls back to the full-JSON HTTP response.
  Self-hosters get a working system with no Realtime Database.
- **Keyed on the Firebase `uid`**, not the schema `sub`, so a client cannot choose another user's stream.
  On the chat path the orchestrator streams under `/runs/{uid}/{turnId}` and announces each engine call
  (`calls/{i}: {jobId, question}`) so the browser can subscribe to each nested `/runs/{uid}/{jobId}`
  trace.
- **Security.** `web/database.rules.json` makes `/runs/{uid}` **read-only by its owner** (`auth.uid ===
  $uid`); the client never writes (the admin SDK bypasses rules).
- **Failure path.** A failed run streams a terminal `error` / `status: error`, so a client never hangs on
  `running`; and the full HTTP response covers the case where RTDB is unavailable.

---

## 12. The conversational layer (Sonnet)

Everything above is deterministic: a message becomes SQL, or it doesn't. But a chat rail invites messages
that *aren't* data queries — "how does this work?", "did you mean revenue over $100?", or an emotional
"I'm worried our top region is too concentrated." The **conversational layer** handles them **in the same
conversation**, using a frozen Sonnet (`ANTHROPIC_MODEL`, default `claude-sonnet-5`) as a thin
**presentation / fallback** surface — **never as a calculator.**

> **The one invariant: Sonnet never produces a number.** Every figure still comes from SQL the
> deterministic engine ran. Sonnet only *phrases* — it either explains why a message wasn't run, or wraps
> a value the engine already computed. This keeps the "no hallucinated numbers" guarantee intact even
> with an LLM in the loop.

**Two places Sonnet appears** — both optional (`ANTHROPIC_API_KEY`), both never on the arithmetic path:

1. **The orchestrator chat loop** (§3, §4.7) — the tool-loop front end. It decides *when* to call the
   engine and rewrites shorthand, but every number is a `prereasoner_query` result.
2. **`/api/converse`** — the in-rail present/clarify surface for the direct reason page. `engine/
   converse.py: reply()` has exactly two modes:
   - **PRESENT** — given the engine's `answer` + `sql`, wrap the value(s) *verbatim* in human words
     ("Your total revenue comes to $48,213.50. Whether that's 'good' depends on your targets…").
   - **FALLBACK** — given a `clarify` rephrasing or an `error` (no answer), offer the rephrasing or
     explain a meta question — never stating a data value it wasn't given.

**When it fires — three signals from the deterministic engine** (the engine decides; the browser reacts):

| Signal | Raised when | Engine site |
|---|---|---|
| **coverage pre-gate** (`low_confidence`, no result) | a message has no data intent, no schema word, no resolvable entity ("how does this work?") | `KnowledgeReasoner.serve` → `ComposedKnowledgeQuery._has_data_signal` (short-circuits **before** reasoning) |
| **clarify** (a rephrasing, no result) | a query would silently drop part of the question (§8 clarify gate) | `KnowledgeQuery._clarify` |
| **present** (`present: true`, **with** a real result) | the answer is real but the phrasing is emotional/opinion/first-person | `KnowledgeReasoner._tag_present` → `ComposedKnowledgeQuery._human_tone` |

Anything else — a normal data query — never touches this layer; plain queries stay raw, at zero LLM cost.
The detectors are cheap and **schema-aware** (a cue word that is actually a column name reads as data),
and fail safely: `_has_data_signal` fails **open** (never blocks a real query), `_human_tone` fails
**closed** (never forces a needless present). If `ANTHROPIC_API_KEY` is unset, `/api/converse` returns
503 and the browser degrades to a payload-based reply.

---

## 13. Model artifacts & training

Everything the engine opens at runtime lives in `engine/data/` (override with `PREREASONER_DATA_DIR`).
The authoritative per-file table is [`engine/data/README.md`](../engine/data/README.md); the short
version:

| Artifact | What it is |
|---|---|
| `qwen_lora/` | the LoRA adapter for the Qwen2.5-0.5B unified encoder (the trained metric space) |
| `encoder.pt` | state_dict of the trained relational readout (`RelationalModel`) — a plain state_dict, no pickled classes |
| `encoder_meta.pt` | `{"alloc", "cfg"}` — the dim allocation + the model constructor config |
| `alloc.json` | the dim allocation as JSON (86 content dims: 9 struct + 67 property + 10 intent) |
| `families.json` | the 8 entity families → distinctive schema.org properties, `geo` flag, and candidate world tables (§6) |
| `props_thr.json` | per-property Youden-J firing thresholds used by the family consensus decode |
| `anchor_assignment.npz` | per-dim firing thresholds from the anchor head (incl. the intent/operator gates) |
| `dim_thresholds.json` | calibrated threshold overrides for the `/api/dimension` readout |
| `primitives.npz` | the learned 10-primitive head read by `PrimitiveReader` (§7) |
| `taxonomy.csv` / `assignment.csv` | the type→table map (`_world_type_map`) + the training-token table |
| `word_*.json` | knowledgebase word-table metadata for the meaning-graph planner |

The large binaries are produced by the training pipeline — see [`docs/TRAINING.md`](TRAINING.md) for how
the encoder, the relational readout, the property/family calibration, and the SQL artifacts are built and
reproduced. The knowledge-facts data (words index, faithful tables, settlements) lives in Postgres,
populated by [`db/sync`](../db/README.md) — not in this directory.

---

## 14. Glossary

- **unit** — the atomic thing the model reads: one column name, one cell value, or one question token.
  Names and numbers are never split below this level (subtokens are mean-pooled).
- **named dimension** — a reserved hidden dimension of `RelationalModel` trained to fire for a specific
  interpretable concept (a datatype, a schema.org property, or a query intent), so its value is directly
  readable off the last `nc` coordinates.
- **property superposition-decode** — the column-typing method (§6): the model reads a superposition of
  schema.org **property** dims, and the entity **family** is decoded by consensus over how many of a
  family's distinctive properties fire (Youden-J thresholds). Replaced the older taxonomy-leaf typing.
- **family** — one of 8 coarse entity classes (`place` / `person` / `org` / `film` / `music` /
  `publication` / `product` / `organism`). The router returns a family or **abstains** (a literal column).
- **abstain / literal** — a column whose best family fires below 0.40 of its distinctive props; it is a
  literal (amount / id / status) and is not typed as an entity.
- **QID** — a Wikidata entity id (France = `Q142`, city type = `Q515`). In the qid-keyed
  `knowledgebase."<type>"` tables it is the PRIMARY KEY; item-property columns store the related entity's
  QID as a FOREIGN KEY.
- **RelationalModel** — the trained 10-layer bidirectional relational-attention transformer
  (`engine/encoder_model.py`) that carries the named dimensions, on top of the LoRA-Qwen encoder.
- **unified encoder** — Qwen2.5-0.5B + a LoRA adapter, one model reused for entity resolution (a metric
  space), the anchored property/intent readout, and the free-text bridge embeddings.
- **ensure_entity** — `engine/knowledge_sync.py: ensure_entity(qid, type_qid)`: lazily fetches a QID's
  faithful row from WDQS into `knowledgebase."<type>"` the first time it's needed.
- **bridge** — a per-conversation table: `"<t> connected to wikipedia"` (resolved cell → QID, backs the
  join) and `"<t> unconnected to wikipedia"` (encoder vector per free-text cell, backs the semantic
  predicate).
- **search_path** — the Postgres setting `"<conv>", knowledgebase, public` that lets one query see the
  upload, the knowledgebase, and the geo tables together.
- **clarify gate** — returns a "did you mean?" rephrasing when the SQL would silently drop part of the
  question. Answered in the chat rail by the conversational layer, not a page redirect.
- **coverage pre-gate** — `_has_data_signal`: a message with no data intent, schema word, or resolvable
  entity short-circuits with `low_confidence` before any reasoning.
- **view stacking** — the composition mechanism (`engine/compose.py`): a complex question decomposes into
  a DAG of primitive views (EXCL / RATIO / TOPN / SHARE / TIME / HAVING / SORT / DIVIDE / RUNNING / GROUP)
  over a JOIN + knowledge base.
- **typed-AST planner** — THE own-data SQL planner: the deterministic bounded SQL-AST search with
  hand-written inspectable ranking (`engine/sql_*.py`, [SQL_AST.md](SQL_AST.md)). It runs unconditionally;
  there is no planner-mode toggle.
- **orchestrator** — the `prereasoner-chat` Cloud Run service (`orchestrator/`) running the Sonnet tool
  loop over the MCP tools.
- **MCP server** — the stdio server (`mcp_server/`) exposing `prereasoner_query` / `prereasoner_describe`,
  spawned per session by the orchestrator, forwarding to the engine over HTTP.

---

## 15. Repository layout

```
engine/            the serving engine: model, planner stack, SQL execution, HTTP server
                   (run: python -m engine.server; see §4–§8)
engine/data/       runtime model + calibration artifacts (engine/data/README.md, §13)
orchestrator/      the Sonnet chat backend (prereasoner-chat): server.py + orchestrator.py + system_prompt.py
mcp_server/        the stdio MCP server (prereasoner_query / prereasoner_describe) fronting the engine
db/                the knowledgebase database: init.sql + the sync/ bootstrap pipeline (db/README.md)
web/               the static frontend: Firebase Hosting pages + rewrites + RTDB rules (web/README.md)
training/          the training pipeline (docs/TRAINING.md)
tests/             end-to-end tests (engine suites need live Postgres) + the MCP/orchestrator contract tests
spider/            the Spider evaluation harness for the typed-AST planner (docs/SQL_AST.md)
docs/              this document + RESEARCH.md + SQL_AST.md + TRAINING.md
Dockerfile         builds the engine container (prereasoner-api)
Dockerfile.orchestrator  builds the orchestrator + bundled MCP server (prereasoner-chat)
cloudbuild.yaml / cloudbuild.orchestrator.yaml   the two Cloud Build pipelines
requirements.txt   serving dependencies
```
