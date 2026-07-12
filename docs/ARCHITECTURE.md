# PreReasoner — Architecture

> **Read this to understand how the system works.** It is top-down: each section zooms in one
> level, and you can stop at any depth and still have a coherent picture.
>
> Sibling docs (kept separate on purpose): **[RESEARCH.md](RESEARCH.md)** = why the approach is
> novel (the research claims and their caveats). **[../db/README.md](../db/README.md)** = the
> world-database contract and how to bootstrap it. **[../web/README.md](../web/README.md)** = the
> frontend. **[../training/README.md](../training/README.md)** = how the model was trained and how
> to reproduce it. Deployment specifics (Cloud Run, Firebase Hosting, secrets) live in
> `infra/README.md`.
>
> The code in this repository was consolidated from an iterative research codebase into one
> package with functional names; the internal iteration numbering does not appear here.

## Contents

1. [What it is](#1-what-it-is) · 2. [The one big idea](#2-the-one-big-idea) ·
3. [System overview](#3-system-overview) · 4. [A request, end to end](#4-a-request-end-to-end) ·
5. [How the model works](#5-how-the-model-works) ·
6. [The layered query engine](#6-the-layered-query-engine) ·
7. [World knowledge & the data model](#7-world-knowledge--the-data-model) ·
8. [Multi-tenancy & auth](#8-multi-tenancy--auth) · 9. [Live trace streaming](#9-live-trace-streaming) ·
10. [Data artifacts](#10-data-artifacts) · 11. [Key decisions](#11-key-decisions-adrs) ·
12. [Glossary](#12-glossary) · 13. [Repository layout](#13-repository-layout)

---

## 1. What it is

**The problem.** AI chatbots can't be trusted with real numbers. Paste a spreadsheet into a
chatbot and ask for a total and it may *guess* — and you can't tell whether it did the math or
made it up. For a business decision, "probably right" isn't good enough.

**What PreReasoner does.** You attach one or more CSVs and ask a plain-English question. It
**writes a precise SQL query, runs it on your actual data, and shows you both the answer and the
query.** When the question needs a fact you didn't upload (e.g. "in *France*" when your sheet
only lists cities), it joins your data to an implicit **Wikidata world database**. Because the
answer comes from executing SQL — not from a language model generating text — a wrong answer is a
*traceable query*, never a hallucination.

**One sentence.** PreReasoner is an interpretable query-planning layer that turns *question +
spreadsheet(s)* into a *deterministic SQL query*, using a frozen LLM only as an encoder (never a
generator) and a small trained model whose hidden dimensions are named.

---

## 2. The one big idea

A normal "ask your data" tool built on RAG embeds everything into anonymous vectors and
*approximately* retrieves the nearest ones, then lets an LLM write the answer. PreReasoner does
the opposite: it **names** the dimensions of its representation (this cell is a *city*; that one
is a *hospital*) and then **queries them exactly** with SQL. Naming is what makes precision
possible — you cannot write `WHERE dim_247 = 'city'` against an anonymous embedding. (The full
argument, and why this is *not* RAG, is in [RESEARCH.md](RESEARCH.md). Here we only build on it.)

Two consequences shape the whole architecture:

- **No generation at inference.** The query is *assembled* by deterministic templates and
  *executed*. There is no decoder anywhere.
- **The structure is split in two** (remember this — it recurs below): *what each cell is* is
  **learned but readable** (named dimensions, anchored to real Wikidata taxonomy nodes); *how the
  tables relate* (the joins) is **computed by a deterministic algorithm**, not learned. The model
  never invents which tables join — it is handed that and only weights it.

---

## 3. System overview

One serving process (`engine/server.py`, the `prereasoner-api` service) exposes three endpoints
plus a health check. The static frontend (`web/`) calls it through a single `/api/**` rewrite.

```
                +------------------------------------+
   User ------> |  Browser app  (web/public/)        |   static pages: index, reason,
                |  attach CSV(s) + ask a question    |   world, clarify, sheets
                +---------+----------------^---------+
                          |                |
        Google sign-in    | POST /api/...  |  live trace: subscribe
        (redirect)        | + Bearer token |  /runs/{uid}/{jobId}
   +---------------+      |                |
   | Firebase Auth |      |        +-------+----------------+
   +-------^-------+      |        | Firebase Realtime DB   |  (OPTIONAL — see §9)
           |              v        +-------^----------------+
           |   +---------------------------+--------+
  verify   +---+  prereasoner-api  (engine/server.py)|   ONE service; encoder-only
  ID token     |  POST /api/reason                   |   model runs on CPU;
               |  POST /api/world                    |   stateless per request
               |  POST /api/dimension                |
               |  GET  /healthz                      |
               +------+--------------------+---------+
                      | run SQL            | lazy ensure_entity(qid)
                      v                    v
            +------------------+   +----------------+
            | Postgres `world` |   | Wikidata (WDQS)|
            | conversation     |   | fills world    |
            | schemas + chat + |   | rows on demand |
            | wikipedia world  |   +----------------+
            | DB + resolution  |
            +------------------+
```

- **Browser app** (`web/`) — the **workbook**: the user's tables and every reasoning step appear
  as spreadsheet tabs, with a chat rail for follow-ups and saved conversations (§8). Plain
  HTML/JS, no build step. It renders the reasoning trace live as it streams (§9).
- **Firebase Auth (Google)** — identifies the user; the verified identity is what authorizes
  access to a conversation's data (§8).
- **`prereasoner-api`** — the model + planner + SQL executor, one HTTP service. Request limits:
  10 MB body, 8 sheets, 5,000 rows per sheet. `/api/reason` and `/api/world` share one
  `WorldReasoner` instance behind one lock; `/api/dimension` has its own stateless model and lock.
  It also serves the conversation endpoints (`GET /api/conversations`, `GET /api/conversation`).
- **Postgres `world`** — holds the shared `wikipedia` world DB, the `world` resolution/taxonomy
  index, a small `chat` schema (who owns which conversation), and one schema per conversation
  holding its tables + bridges (§8). Contract in [`db/README.md`](../db/README.md).
- **Wikidata (WDQS)** — the world tables start empty and **lazy-sync** rows from Wikidata on
  demand (`engine/world_sync.py: ensure_entity`), the first time a resolved QID is needed. Not a
  bulk offline import on the hot path.

All runtime configuration is environment variables, read in one place (`engine/config.py`):
`HOST`/`PORT`, `WORLD_PG_HOST/PORT/DB/USER/PASSWORD/SSLMODE`, `RTDB_URL` (optional, §9),
`PREREASONER_DATA_DIR` (artifact dir, §10), `DEVICE`, `BASE_MODEL_ID`, `WORLD_MODEL_ROUTE`, and
the test-only `AUTH_TEST_SUB` (§8).

---

## 4. A request, end to end

### 4.1 `POST /api/reason` — the compositional reasoner (the France = 270 trace)

The whole machine, moving once, for the default demo: tables `customers.csv` + `orders.csv`,
question **"total amount in France"**. (`customers` has a `city` column — Paris/Lyon/Berlin — but
no country.) The home page submits to the reason player, which calls `/api/reason`; a plain world
query like this one is delegated down to the world planner.

1. **Upload.** The browser saves the sheets + question, signs the user in (Google redirect),
   gets a fresh ID token, and does `POST /api/reason` with `Authorization: Bearer <token>` and
   body `{tables, question, jobId}`.
2. **Auth gate** (`engine/server.py` + `engine/auth.py`). Firebase Admin verifies the ID token
   server-side; `_verify_principal` returns both the Google `sub` (the Postgres schema name) and
   the Firebase `uid` (the trace-stream path). No/invalid token → **401**. The schema is *always*
   this server-verified `sub` — never anything the client sent.
3. **Route to the planner.** `ComposedWorldQuery` (the view-stacking reasoner,
   `engine/world_compose.py`) sees no composition primitive (no yoy/top-N/share/…) and
   **delegates** the plain world join to `WorldQuery` (`engine/world_query.py`). Each *unit* —
   every column name, every cell value, every question token — is encoded by the unified encoder
   (Qwen2.5-0.5B + the `qwen_lora` adapter), its subtokens mean-pooled into one vector; the
   vectors pass through the trained relational model; named dimensions are read off. From these:
   - **Typing/routing:** FK discovery finds `orders.customer_id → customers.customer_id`
     (deterministic, `engine/relations.py`). `engine/router.py: Router` types `customers.city`
     from its taxonomy dims → the Wikidata leaf `city` (QID `Q515`) → the table
     `wikipedia."city"`. The word "France" is recognized as a world filter.
4. **Resolve cells to world QIDs.** Each `city` value is resolved through `world."words"` (exact
   normalized match first, else bge-small pgvector cosine nearest-neighbor ≥ threshold) → a city
   QID (Paris → `Q90`, Lyon → `Q456`, Berlin → `Q64`). If `wikipedia."city"` has no row for that
   QID yet, `ensure_entity(qid, type_qid)` lazily fetches the faithful Wikidata row from WDQS
   (item-property columns kept as QIDs — `city.country` = the country's QID) and INSERTs it. The
   resolved column→value→QID mapping is persisted in the per-user bridge
   `"customers connected to wikipedia"`.
5. **Assemble SQL** — JOIN the user table to `wikipedia."city"` ON `qid` via the bridge, filter
   on the **QID foreign key** (`city.country = 'Q142'` is France), aggregate (operator `SUM`
   chosen from the model's intent dims):
   ```sql
   SELECT SUM(orders.amount) AS total
   FROM customers
   JOIN orders ON orders.customer_id = customers.customer_id
   JOIN "customers connected to wikipedia" b
        ON b.column = 'city' AND lower(b.value) = lower(customers.city)
   JOIN wikipedia."city" ON wikipedia."city".qid = b.world_key
   WHERE wikipedia."city".country = 'Q142';   -- Q142 = France
   ```
6. **Execute** (Postgres). `SET search_path TO "<sub>", wikipedia, world, public`; the upload
   tables + bridges are (re)created from the request. A **SELECT-only guard** rejects anything
   but a read. `SUM` over Paris+Lyon customers' orders = **270** (Berlin resolves to a German
   city → `country = 'Q183'` → excluded).
7. **Respond + stream.** The service returns the answer rows **and the SQL it ran**. In parallel
   the backend has been **streaming the trace** as each stage completed (`status: resolving` →
   each `resolve/{cell}: qid` → each `views/{i}` → `result` → `done`), so the browser fills the
   slides in live rather than waiting on the HTTP body (§9).

A question *with* composition depth — *"top 3 cities by total amount"*, *"total amount in Europe
by city"* — is instead decomposed by `ComposeEngine` (`engine/compose.py`) into a DAG of simple
primitive views (`filter`, `time-filter`, `group_agg`, `yoy`, `running`, `share`, `divide`,
`having`, `top-N`, `sort`) stacked over the FK + world base, each view streamed as it
materializes. A **composition-view gate** decides when the reasoner stands on its own result:
only for a genuine composition view or a world measure. A plain aggregate, count, or group-by
that merely misfired the primitive detector defers to the `WorldQuery` delegate — composition
adds depth without regressing the simple paths.

### 4.2 `POST /api/world` — the world join

Same auth, same request shape, same trace contract as `/api/reason` (the two routes share one
`WorldReasoner` instance). This is the world path run directly: route → resolve → JOIN to the
`wikipedia` QID schema → QID-FK filter → aggregate, re-expressed as a streamable view stack.
Two extras live at this layer:

- **Geo NEARBY** (`engine/world.py: WorldReasoner`): "big cities near Paris" resolves the
  reference city to lat/lng (`public.settlement`) and ranks world cities by haversine distance —
  computed in plain SQL, no PostGIS.
- **Hybrid structured + semantic queries** (`engine/world_query.py`): "who complained about *bad
  delivery* in France" combines an exact world filter (`country = France` via the resolved QID)
  with a pgvector cosine predicate over the free-text bridge — both sides embedded by the *same*
  unified encoder, so the `<=>` cosine is a valid same-space comparison.

### 4.3 `POST /api/dimension` — the interpretability readout

Body `{data, mode: "analyze"}`. Stateless — no Postgres, no auth, nothing persisted. Runs the
same encoder + relational readout (`engine/dimension.py: DimensionModel`) and returns the
per-column / per-cell **named-dimension activations**, walking the anchored taxonomy layer by
layer. This is the view that lets you *inspect what the model believes each cell is* — the
interpretability claim made literal (see RESEARCH.md §1).

### 4.4 `GET /healthz`

Liveness, plus whether the models finished loading. Used by the frontend's warm-up ping: the
service scales to zero, and the first cold hit must load the model, so the home page fires a
warm-up GET and the client retries.

---

## 5. How the model works

The inference path is **encoder-only — there is no decoder, no `.generate()`, no autoregression
anywhere.** Qwen is loaded but only ever run forward for hidden states.

```
question + CSV(s)
  → deterministic ingest: dedup + foreign-key discovery       (engine/relations.py — NO model)
  → unified encoder: Qwen2.5-0.5B + LoRA adapter              (a unit = one column name / one cell value / one question token)
  → mean-pool the unit's subtokens → ONE vector per unit      ("never split a name/number" is honored here)
  → 10-layer bidirectional relational transformer             (engine/encoder_model.py: RelationalModel, weights in encoder.pt)
  → read the NAMED dimensions (93 content dims, alloc.json)   (anchored readout, calibrated per-dim thresholds)
  → read intent dims off question tokens → operator           (SUM/COUNT/AVG without a keyword list)
  → Router types each string column → a taxonomy leaf QID     (engine/router.py)
  → deterministic Python assembles SQL clauses                (engine/world_query.py / engine/compose.py)
  → SELECT-only guard → execute (Postgres / SQLite)
```

**One unified encoder, trained three ways at once.** The encoder is a **frozen-base Qwen2.5-0.5B
+ a LoRA adapter** (`engine/data/qwen_lora/`) feeding the trained 10-layer bidirectional
`RelationalModel`. It was trained jointly with three losses (reproduction in
[`training/`](../training/README.md)):

- **contrastive InfoNCE** on Wikidata altLabel pairs → a **metric geometry** where close entity
  names sit near each other (this is what powers embedding-based entity resolution at serve time);
- **MSE anchoring** of named dims on a column-graph corpus → the **typing** (what a cell/column is);
- reused **SQL/join graphs** → the **operator/intent** dims.

All anchoring is **MSE on RAW activations** (no BCE, no sigmoid).

**The dimension allocation — 93 content dims** (`engine/data/alloc.json`; 42 live entity
leaves). After a forward pass you can literally read what each unit is:

| Group | Count | What it names |
|---|---|---|
| **struct** | 9 | `is_str` / `is_num` / `num_frac` / `is_time` / … (the datatype shape) |
| **taxonomy** | 74 | one 0/1 dim per **real Wikidata P279 node**, co-firing **down a token's root→leaf path**: a city fires `geolocatable_entity` … `populated_place` … `city`; 42 live entity leaves |
| **intent** | 10 | `intent_agg_{sum,count,avg}`, `filter_{eq,gt,lt}`, `group`, `sort_{desc,asc}`, `limit` |

Instead of a flat hand-named "*city* neuron", a value lights up its **whole ancestor path**
through the Wikidata `P279` (subclass-of) tree, so typing is grounded in a real, navigable
taxonomy — and the leaf dim maps directly to a `wikipedia."<type>"` world table.

**Anchoring on clean instances.** The taxonomy readout was re-anchored on clean Wikidata `P31`
instances (6,665 instances over 46 non-geo leaves) after the initial noisy CSV-derived targets
made non-geo dims non-discriminative; the re-anchor trained **only the relational readout**
(encoder frozen) and moved taxonomy AUC **0.886 → 1.0**. The full story — and why it is the
clearest evidence that anchoring does real work — is in [RESEARCH.md §6](RESEARCH.md); the
pipeline is `training/corpus/fetch_type_instances.py` → `training/anchor/reanchor.py`, gated by
`training/calibrate/validate_data.py` and `training/calibrate/validate_route.py`.

**Routing.** `engine/router.py: Router` types an uploaded column to its taxonomy leaf (a QID +
the matching `wikipedia` table) by scoring each candidate leaf over its root→leaf path — the leaf
dim at full weight, ancestors decaying toward the root — gated by per-leaf calibrated thresholds
(`route_thresholds.json`). Routing reads cell **values**, not headers (column names fire
inconsistently). Two more calibration artifacts back the served model: `dim_thresholds.json`
(the per-dim `/api/dimension` thresholds, from `training/calibrate/calibrate_dims.py`) and the
served-model routing gate (`training/calibrate/validate_route.py`), tested through the deployed
grounding path. Set `WORLD_MODEL_ROUTE=0` to fall back to value-membership routing without the
model.

**Operator-from-intent.** The `intent_agg_{sum,count,avg}` dims are read off the question tokens
— the reader takes the max activation across non-operand question tokens and gates against the
calibrated threshold — so "how much did we sell" yields `SUM` without any keyword list. ("Total
*customers*" — a count noun with no measure — maps to a row COUNT, not a SUM of a foreign key.)
Keyword heuristics survive only as the encoder-free fallback so the composition engine stays
testable without a model.

**The relational structure (the deterministic, auditable part).** The relationships between
units are built **outside** the network and injected as a fixed prior; the network only learns
*how strongly* to weight each one:

- Edges `same_col`, `same_row`, `same_cell`, and the cross-table `fk` form an integer adjacency
  matrix built by plain Python (`engine/fk_edges.py`, graphs assembled by `engine/graph_walk.py`).
- Foreign keys are discovered deterministically (`engine/relations.py`): inclusion-dependency
  (≥90% of a column's values contained in a key) + many-to-one cardinality + name/type heuristics
  + a confidence gate.
- The model's *only* relational parameter is a per-edge-type × per-head additive attention bias
  (`att = att + eb[edges]`). It learns how much to attend along a *given* edge — never *which*
  units relate.

---

## 6. The layered query engine

The planner is a stack of layers, each adding one capability over the one below. In code this is
a class chain — every layer is independently readable, and the serving entry points compose the
whole stack:

```
engine/tables.py          TableQuery          base: CSV parsing, SQL identifier/literal quoting,
                                              the anchored-readout planner over the user's own tables
engine/world_tables.py    WorldTableQuery     the implicit world word-tables + meaning graph
                                              (word_*.json metadata; SQLite fallback for offline use)
engine/pg.py              PgQuery             Postgres execution: connections, per-user schema
                                              loading, type affinity (INTEGER→BIGINT, NUMERIC→py)
engine/resolve_base.py    RoutedQuery         generalized routing + country aliases + states/elements
engine/entities.py        EntityQuery         embedding entity resolution: world."words" exact-norm
                                              match then pgvector cosine NN; cell bridges; lazy city fill
engine/encoder_overlay.py EncoderQuery        the unified-encoder overlay: loads encoder_meta.pt /
                                              encoder.pt / qwen_lora (load_encoder — the single loader)
                                              and shares the model onto the layers below
engine/world_query.py     WorldQuery          the live world path: route → resolve → QID join →
                                              QID-FK filter → aggregate; hybrid semantic SQL; clarify
engine/world_compose.py   ComposedWorldQuery  view-stacking composition over the world base;
                                              delegates non-composed queries to WorldQuery
engine/world.py           WorldReasoner       + geo NEARBY (haversine over public.settlement);
                                              wraps, never modifies, the composed planner
engine/dimension.py       DimensionModel      the stateless /api/dimension readout (EncoderQuery subclass)
```

Supporting modules: `engine/compose.py` (`ComposeEngine`, the deterministic composition engine —
executes the primitive DAG on SQLite), `engine/primitives.py` (pure SQL view builders),
`engine/primitive_head.py` (`PrimitiveReader`, the learned 10-primitive head on the same
encoder), `engine/joins.py` (offline FK discovery for the compose engine), `engine/bridge.py`
(the bridge predicate machinery), `engine/embeddings.py` (the bge-small retrieval embedder +
surface normalization), `engine/taxonomy.py` (the P279 taxonomy constants, loaded from
`taxonomy.csv`), `engine/world_sync.py` (the lazy Wikidata client: `ensure_entity`,
`lazy_resolve`, WDQS access), `engine/trace.py` (§9), `engine/auth.py` (§8), and
`engine/config.py` (the one env-var reader).

`TableQuery` on its own does not load model weights — the encoder overlay supplies the one
shipped model to the whole stack, so the process holds a single Qwen in memory shared by routing,
resolution, and the intent readout.

---

## 7. World knowledge & the data model

The world DB is the **`wikipedia` Postgres schema: `qid` PRIMARY KEY / `qid` FOREIGN KEY
everywhere, lazy-synced from Wikidata.** The full database contract — schemas, DDL, indexes,
extensions, bootstrap scripts — is owned by [`db/README.md`](../db/README.md); this section is
the engine's view of it.

**How a value reaches the world DB — the concept picks the table, the QID picks the row.**

1. `Router` **types** a column to its taxonomy leaf → a `wikipedia."<type>"` table (e.g. `city`).
2. Each cell **resolves** to a world **QID** via `world."words"`: exact normalized match first,
   then bge-small pgvector **cosine NN ≥ threshold**. (This is the metric-space half of the
   unified encoder doing entity resolution.) The type is looked up by the **exact Wikidata
   label** (from `world."types"`), the same key `ensure_entity` inserts under.
3. If `wikipedia."<type>"` has no row for that QID, `engine/world_sync.py: ensure_entity(qid,
   type_qid)` **lazily fetches** the faithful row from WDQS — item-property columns kept as
   **QIDs** — and INSERTs it.
4. The join is then **exact equality on the QID FK** — `JOIN wikipedia."city" ON qid =
   bridge.world_key`, `WHERE city.country = 'Q142'` — no string match, no similarity search at
   join time. A 2-hop world fact ("total amount in Europe") is just two QID FK hops:
   `city.country` → `country.continent = 'Q46'`.

**The data model (one Postgres database `world`, four schema families):**

| Schema | Holds | Built by |
|---|---|---|
| **`public`** | the raw Wikidata geo/type import (`settlement`, `country`, `admin`, `continent`, `currency`, `element`, `timezone`, …) — read directly for geo NEARBY | `db/init.sql` + `db/sync` (bulk) |
| **`wikipedia`** | the world meaning DB: **one table per Wikidata type, named by the EXACT Wikidata label** (`wikipedia."city"`, `wikipedia."hospital"`, …), faithful Wikidata property columns, **`qid` PRIMARY KEY**; item-property columns hold the related entity's **QID (a true FK)**. Tables start **empty** and **lazy-sync** on demand. | `db/sync/build_wikipedia.py` (empty qid-PK tables); `engine/world_sync.py: ensure_entity` (fills) |
| **`world`** | the resolution + taxonomy index: `world."words"` (bge-small pgvector HNSW index: `norm → qid + type + embedding`) and `world."types"` (the P279 taxonomy: `qid → label`), plus the friendly geo tables the planner's population ranking reads | `db/sync/build_words.py`, `sync_types.py`, `build_world.py` |
| **`"<google-sub>"`** | per user: one table per uploaded CSV + two persisted **bridges** (below), created per request | the engine, at request time |

```text
-- schema `wikipedia` (qid-keyed; one table per Wikidata type, exact Wikidata labels)
   wikipedia."city"     ( qid PK, label, country  <FK→country.qid>, population, … )
   wikipedia."country"  ( qid PK, label, continent <FK→continent.qid>, currency <FK→…>, … )
   wikipedia."hospital" ( qid PK, label, country  <FK→country.qid>, … )
   … all qid-PK, item-property columns are qids …

-- schema `world` (resolution + taxonomy index)
   world."words"  ( norm, qid, type, embedding vector(384) )   -- exact match, then pgvector cosine NN
   world."types"  ( qid, label, parent_qid, is_leaf, … )        -- the P279 taxonomy

-- schema "<google-sub>" (per request): the uploaded sheets + two bridges
   "<sub>"."customers"                              -- the upload, re-created from the request
   "<sub>"."customers connected to wikipedia"       -- resolved cell → world qid (column / value / world_key)
   "<sub>"."customers unconnected to wikipedia"     -- a unified-encoder vector(896) per free-text cell
```

**The two per-user bridges.**

- **`"<csv> connected to wikipedia"`** — the resolved-cell table, keyed by column / value /
  world_key (QID). It is what the world join reads: `JOIN wikipedia."<type>" ON qid =
  bridge.world_key`. Persisting the resolution means re-runs don't re-resolve.
- **`"<csv> unconnected to wikipedia"`** — a unified-encoder **vector per free-text cell**,
  backing the semantic predicate (the pgvector `<=>` cosine path, for hybrid queries like "who
  complained about *bad delivery*").

**How the layers link.** Each request runs `SET search_path TO "<sub>", wikipedia, world,
public`, so one query resolves the user's upload tables, the shared `wikipedia` world tables,
the resolution index, and the geo tables in one namespace.

**The clarify gate (coverage).** A query that would silently drop part of the question — an
entity that **resolved** but isn't filtered, or a measure word with **no aggregate** applied — is
caught instead of answered wrong. The service returns a `clarify` response (a "did you mean?"
rephrasing, rendered by `web/public/clarify.html`) rather than a degenerate query. Coverage is
checked against the resolved **QID** in the QID-keyed SQL.

---

## 8. Conversations, identity & isolation

`/api/reason` and `/api/world` execute on live Postgres, gated by Firebase Google auth.
`/api/dimension` is unauthenticated by design — it is stateless and stores nothing.

**A conversation owns a schema.** Each conversation gets its own Postgres schema (named by a
random `conversation_id`, `c_<32 hex>`) that holds that conversation's uploaded tables and derived
data. So a conversation is self-contained — inspectable on its own, and archivable as a unit (§8.2).
The engine module `engine/conversations.py` owns this; the DDL is in [`db/init.sql`](../db/init.sql).

**Who a conversation belongs to lives in a small `chat` schema:**

| table | holds |
|---|---|
| `chat.user_profile` | the signed-in identity — the verified Google `sub`. |
| `chat.conversation` | `conversation_id`, the opening question, the uploaded tables (so it re-opens self-contained), a timestamp. |
| `chat.user_conversation` | the ownership link — which user owns which conversation. |

### 8.1 The security model (why this isn't an IDOR)

The identity is **always the verified token subject** (`engine/auth.py: _verify_principal`
returns the Google `sub` + the Firebase `uid`) — never anything the client sends. A request may
carry a `conversation_id`, but it is honored **only after** an ownership check against
`chat.user_conversation` confirms it belongs to the verified user; otherwise the engine mints a
fresh conversation. A `conversation_id` that isn't yours (or doesn't exist) returns the same "not
found" either way — no enumeration. And because a `conversation_id` doubles as a schema name, it
is validated against the strict `c_<32 hex>` shape before it ever reaches SQL, so it can't inject.

- **Isolation** is application-enforced: the working schema comes from the (authorized)
  conversation, `search_path` scopes queries to it plus the shared `wikipedia`/`world` schemas,
  and generated SQL is SELECT-only with quoted identifiers. One Postgres role; separation by schema.
- **Sign-in is a same-tab redirect** (not a popup). The frontend sets `authDomain` to the domain
  you're actually on so the redirect result survives browser storage partitioning, with a
  loop-breaker that shows a retry instead of bouncing forever (details in
  [`web/README.md`](../web/README.md)).
- **No concurrent-request deadlock.** The bridge read connection is `autocommit=True`, and the
  one `ACCESS EXCLUSIVE` migration (adding a bridge column) is guarded by an `information_schema`
  check so it fires only when genuinely needed — steady state takes no exclusive lock.
- **Test bypass.** `AUTH_TEST_SUB` skips token verification and pins a fixed user — local harness
  only; never on a live service.

Note: the engine initializes firebase-admin (Application Default Credentials) for token
verification even when trace streaming is off, so the authenticated routes need Google
credentials unless `AUTH_TEST_SUB` is set.

### 8.2 Archiving a conversation (optional)

Because a conversation is one self-contained schema, an idle one can be serialized to Cloud
Storage and restored on demand: `db/sync/archive_conversation.py` `pg_dump`s the schema to
`gs://$GCS_BUCKET/conversations/<id>.sql.gz` (optionally dropping it to free the database) and
restores it with `psql`. The `chat` metadata stays, so the conversation remains listed and
re-openable. (Operator tooling; the restore bucket is a trust boundary — lock it down.)

---

## 9. Live trace streaming

The reasoning trace is **streamed live as the backend computes it**, and the browser subscribes
and renders slides as they arrive. This **decouples rendering from the HTTP response**: a slow
2-hop geo query that would exceed a ~60 s HTTP proxy timeout streams to the answer anyway, and
"watch how the model reasons" becomes literal — each resolved QID and each view appears as it is
produced, not reconstructed afterward.

```
Browser (reason/world page)          prereasoner-api                Firebase RTDB /runs/{uid}/{jobId}
   | generate jobId; render Input slide instantly
   |-- POST {tables, question, jobId} + Bearer  -->|  (fire-and-forget)
   |-- subscribe /runs/{uid}/{jobId} --------------------------------->|
   |                                    | _verify_principal → (sub, uid)
   |                                    |-- status: resolving --------->|
   |                                    |-- resolve/{cell}: qid ... --->|
   |                                    |-- views/{0..n}: {op,label,sql,columns,rows} -->|
   |                                    |-- result: {columns,rows}; status: done ------->|
   |<------------------- each write pushed → render slide -------------|
   |<-- HTTP body still returns (full-response fallback) --|
```

- **Backend.** `engine/trace.py` exposes `emitter(uid, jobId) → emit(node, value)` (and
  `stream_final`); the server emits `status` and the terminal `result`/`clarify`/`error`/`done`,
  while the compose engine emits `status: resolving` and each `views/{i}` as it is built.
  firebase-admin (already present for auth) does the writes. Every write is best-effort —
  streaming must never break the answer.
- **RTDB is OPTIONAL.** Streaming is driven by the `RTDB_URL` env var. Unset ⇒ the emitter
  functions become clean no-ops, firebase-admin still initializes for auth, and the frontend
  falls back to the full-JSON HTTP response. Self-hosters get a working system with no Realtime
  Database at all.
- **Keyed on the Firebase `uid`, not the schema `sub`.** The stream lives under the
  authenticated user's node; a client cannot choose another user's stream (§8).
- **Stream schema** at `/runs/{uid}/{jobId}`: `conversation_id` (emitted first, so the browser
  learns it even if the HTTP body is lost to a proxy timeout — §8), `status`
  (`resolving|running|done|clarify|error`), `resolve/{cell}: qid`, `views/{i}:
  {op,label,sql,columns,rows}`, `result: {columns,rows}`, `clarify`, `error`.
- **Security.** `web/database.rules.json` makes `/runs/{uid}` **read-only by its owner**
  (`auth.uid === $uid`); the client never writes (the admin SDK bypasses rules).
- **Failure path.** A failed run streams a terminal `error` / `status: error`, so a client never
  hangs on `running`; and the full HTTP response covers the case where RTDB is unavailable.

---

## 10. Data artifacts

Everything the engine opens at runtime lives in `engine/data/` (override with
`PREREASONER_DATA_DIR`). The authoritative per-file table — sizes, consumers, and which files are
gitignored — is [`engine/data/README.md`](../engine/data/README.md). The short version:

| Artifact | What it is |
|---|---|
| `qwen_lora/` | the LoRA adapter for the Qwen2.5-0.5B unified encoder (the trained metric space) |
| `encoder.pt` | state_dict of the trained relational readout (`RelationalModel`) — a plain state_dict, no pickled classes |
| `encoder_meta.pt` | `{"alloc", "cfg"}` — the dim allocation + the model constructor config |
| `alloc.json` | the dim allocation as JSON (same content as `encoder_meta.pt["alloc"]`), used by the torch-free router import |
| `anchor_assignment.npz` | per-dim firing thresholds from the anchor head |
| `dim_thresholds.json` / `route_thresholds.json` | calibrated threshold overrides for the `/api/dimension` readout and the per-leaf routing gates |
| `taxonomy.csv` / `assignment.csv` | the P279 taxonomy (root→leaf paths, QIDs, world tables) and the training-token table (which leaves are supported) |
| `primitives.npz` | the learned 10-primitive head read by `PrimitiveReader` |
| `word_*.json` | world word-table metadata for the meaning-graph planner |

The large binaries are produced by the [`training/`](../training/README.md) pipeline; the
world-facts data (words index, wikipedia tables, settlements) lives in Postgres, populated by
[`db/sync`](../db/README.md) — not in this directory.

---

## 11. Key decisions (ADRs)

Each: *what we chose, the alternative, and why.*

1. **Encoder, not backbone.** Qwen encodes each clean *unit* and we pool its subtokens; we do
   **not** read named dims off Qwen's raw BPE shards. *Alternative:* anchor dims directly on
   subtokens. *Why:* reading a dim off a shard splits names/numbers
   (`Marie Curie` → `["Marie", " Cur", "ie"]`) and ruins interpretability. Pooling makes the unit
   atomic — honoring "never split a name or number."
2. **Deterministic joins, not learned.** FK discovery + the edge graph are plain algorithms fed
   to the model as a fixed prior. *Alternative:* let the model learn which tables join. *Why:*
   auditability, and small models can't reliably *discover* joins — but they can *weight* given
   ones.
3. **No decoder / no generation.** SQL is assembled by templates and executed. *Why:* a wrong
   answer must be a traceable query, not a hallucination — the entire value proposition.
4. **Taxonomy-path dims instead of flat hand-named type dims.** Each entity value fires its
   **whole Wikidata `P279` ancestor path** (root→leaf), not one hand-named neuron. *Why:* typing
   is grounded in a real, navigable taxonomy, and the leaf dim maps directly to a
   `wikipedia."<type>"` table.
5. **Re-anchor the readout on clean Wikidata instances.** Noisy embedding-mapped CSV cells made
   non-geo dims non-discriminative; the non-geo pool was replaced with clean `P31` instances and
   only the relational readout re-trained (encoder frozen, geo spine skipped). *Why:* a `street`
   dim was out-firing `hospital`; the clean re-anchor took taxonomy AUC 0.886 → 1.0 without
   touching the encoder (RESEARCH.md §6).
6. **QID-keyed `wikipedia` schema, lazy-synced.** *Alternative:* name-keyed world tables joined
   on `lower(name)`. *Why:* QID PK/FK gives exact, homonym-free joins and 2-hop world filters
   (`city.country.continent`); lazy `ensure_entity` fills only what's actually queried instead of
   a full bulk import.
7. **One serving process, three endpoints.** *Alternative:* one service per endpoint. *Why:* the
   endpoints share one model and one code path; a single `prereasoner-api` service means one
   deploy, one warm-up, one lock discipline (`/api/reason` + `/api/world` share the model;
   `/api/dimension` is independent).
8. **Streaming out-of-band, HTTP as fallback.** *Alternative:* hold the HTTP response open (or
   chunk it) for the whole computation. *Why:* proxy timeouts break long geo queries; an optional
   RTDB side-channel streams past them while the plain JSON response keeps the system fully
   functional without it.
9. **Scale-to-zero + warm-up retry, not always-on.** *Why:* idle cost ≈ $0; the first cold hit's
   model load + lazy fill is absorbed by a warm-up GET + client retry. (Deployment detail:
   `infra/README.md`.)

---

## 12. Glossary

- **unit** — the atomic thing the model reads: one column name, one cell value, or one question
  token. Names and numbers are never split below this level.
- **named dimension / anchoring** — a reserved hidden dimension trained (via MSE on raw
  activations) to fire for a specific interpretable concept, so its value is directly readable.
- **taxonomy dim** — one of the 74 taxonomy dims, each a real Wikidata `P279` node; an entity
  value co-fires the dims down its root→leaf path (`geolocatable_entity` … `populated_place` …
  `city`); 42 live entity leaves in the shipped allocation.
- **QID** — a Wikidata entity id (e.g. France = `Q142`, city = `Q515`). In the `wikipedia`
  schema it is the PRIMARY KEY, and item-property columns store the related entity's QID as a
  FOREIGN KEY.
- **RelationalModel** — the trained 10-layer bidirectional transformer
  (`engine/encoder_model.py`) that sits on top of the unified encoder and carries the named
  dimensions.
- **unified encoder** — Qwen2.5-0.5B + a LoRA adapter, trained jointly as a metric space
  (InfoNCE on altLabels, for resolution) **and** the anchored readout encoder (MSE typing +
  intent).
- **edge / FK edge** — typed relations (`same_col`/`same_row`/`same_cell`/`fk`) injected as an
  attention prior; the `fk` edge links a foreign-key column's units to the referenced key's units
  across tables.
- **intent dims** — named dims for the query's operation (`intent_agg_*`, `filter_*`, `group`,
  `sort_*`, `limit`); what turns the readout into SQL.
- **Router** — types a column to its taxonomy leaf (QID + `wikipedia` table) from the anchored
  taxonomy dims, choosing the world table to join (`engine/router.py`).
- **ensure_entity** — `engine/world_sync.py: ensure_entity(qid, type_qid)`: lazily fetches a
  QID's faithful row from WDQS into `wikipedia."<type>"` the first time it's needed.
- **bridge** — a per-user table: `"<csv> connected to wikipedia"` (resolved cell → QID, backs
  the join) and `"<csv> unconnected to wikipedia"` (encoder vector per free-text cell, backs the
  semantic predicate).
- **search_path** — the Postgres setting `"<sub>", wikipedia, world, public` that lets one query
  see the user's tables, the world DB, and the resolution index together.
- **clarify gate** — returns a "did you mean?" rephrasing when the SQL would silently drop part
  of the question (a resolved-but-unfiltered entity, or a measure with no aggregate).
- **view stacking** — the composition mechanism (`engine/compose.py`): a complex analytical
  question decomposes into a DAG of simple primitive views (filter / time-filter / group_agg /
  yoy / running / share / divide / having / top-N / sort) over a JOIN base + a WORLD base. Plain
  world/aggregate/clarify queries delegate to the world planner.
- **alloc** — the allocation map (`alloc.json`) assigning each of the 93 content dims to a named
  concept.

---

## 13. Repository layout

```
engine/            the serving package: model, planner stack, SQL execution, HTTP server
                   (run: python -m engine.server; see §4–§6)
engine/data/       runtime model + taxonomy artifacts (engine/data/README.md)
db/                the world database: init.sql + the sync/ bootstrap pipeline (db/README.md)
web/               the static frontend: Firebase Hosting pages + rules (web/README.md)
training/          the full training pipeline: corpus building, taxonomy, encoder training,
                   anchoring, calibration, release gates (training/README.md)
tests/             end-to-end engine tests (need live Postgres; tests/README.md)
docs/              this document + RESEARCH.md
Dockerfile         builds the serving engine container
requirements.txt   serving dependencies
```
