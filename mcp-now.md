# MCP Implementation — v1 (reasoning in the LLM)

> **Audience: a developer new to this repo, plus Claude Code.** This is the build spec for wrapping
> PreReasoner as an **MCP tool** that a Sonnet orchestrator calls, and for the **chat + reasoning-player
> UI** on top of it. Every "how it works today" claim below was checked against the code in `engine/`
> and `web/`. **Where this doc and the code disagree, the code wins** — but this rewrite was reconciled
> against the code, so they shouldn't.
>
> **What v1 delivers, in one line:** the numbers are *derived and auditable* (PreReasoner writes and runs
> the SQL, and you can watch it), but the *decomposition* of a multi-hop question into steps happens
> inside the LLM's forward pass and is **not** auditable. So v1 = **interpretable execution, opaque
> planning**. Making the plan itself derived is v2 (see [`mcp-future.md`](mcp-future.md)).

---

## 0. Orientation — how the system works *today* (read this first)

There is no `runtime20/` directory (an old internal name). The serving code is the Python package
[`engine/`](engine); the browser app is [`web/`](web). A top-down tour lives in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); here is the minimum you need to build the MCP layer.

### 0.1 The engine is one stateless HTTP service

[`engine/server.py`](engine/server.py) is a stdlib `ThreadingHTTPServer` (no web framework). It exposes:

| Method + path | Handler | Auth | Purpose |
|---|---|---|---|
| `POST /api/reason` | `_post_world` | Firebase | The compositional reasoner (view-stacking). **This is the tool's target.** |
| `POST /api/knowledge`  | `_post_world` | Firebase | The world-join path. Same handler, same contract; shares one model with `/api/reason`. |
| `POST /api/dimension` | `_post_dimension` | none | Stateless per-column/per-cell named-dimension readout. No Postgres. |
| `GET /healthz`, `GET /api/healthz` | — | none | Liveness; `ok:true` only once both models finished loading. |

`/api/reason` and `/api/knowledge` share **one** `KnowledgeReasoner` behind **one** `threading.Lock` — the engine
serves **one reasoning request at a time per instance**. This matters for the orchestrator: sequence
multi-hop calls, don't fan them out in parallel (they'd just queue on the lock).

### 0.2 The request is self-contained — there is no upload step, no `dataset_id`, no session

Every call to `/api/reason` carries the user's tables **inline** in the body:

```jsonc
// POST /api/reason   (header: Authorization: Bearer <Firebase ID token>)
{
  "tables":   [ { "name": "customers", "data": "<raw CSV text>" }, ... ],  // ≤ 8 sheets, ≤ 5000 rows each
  "question": "total amount in France",
  "as_of":    null,        // optional bitemporal cutoff
  "jobId":    "…uuid…"     // optional; the RTDB key the live trace streams to (see §0.5)
}
```

The engine parses the CSVs, (re)creates the user's tables in their Postgres schema, resolves, joins,
and answers — all in that one request. The **only** thing that persists server-side is a per-user
*resolution cache* (the "bridge" tables), keyed by the verified user. There is **no** `prereasoner_upload`
endpoint and **no** dataset handle to pass around — the caller ships the CSVs every time. Any MCP tool
that takes a `dataset_id` would be inventing an API the engine doesn't have. (See [ARCHITECTURE §4](docs/ARCHITECTURE.md).)

### 0.3 The response shape (this is exact — build against it)

The body is whatever `KnowledgeReasoner.serve(...)` returned, `json.dumps`'d. It is **not one fixed shape** —
different internal paths return slightly different key sets. Group them into three outcomes:

**(a) Answer.** Always present: `question`, `result` (`{"columns": [...], "rows": [[...]]}` — the key is
`columns`, never `cols`), and `model` (a human string like `"engine - composed view stack"`).
Then, *depending on which path answered*, some of:
`sql` (the executed SQL — the audit artifact), `views` (`[{op,label,sql,columns,rows}]` — the reasoning
stack), `plan` (list of op names), `routed`/`meaning_join`/`provenance`/`dims` (world-join metadata),
`warnings`, `as_of`, `reference` (geo-nearby). `error` is present and `null` on the success paths that
carry it.

**(b) Clarify** — the "ask instead of guess" gate:
```jsonc
{ "question": "...", "clarify": true,
  "proposed": "a rephrasing that would be unambiguous",
  "bindings": [ {"token","kind","target","score"}, ... ],
  "dropped":  ["words whose meaning never reached the SQL"],
  "original_sql": "the degenerate SQL that dropped part of the question",
  "model": "engine - clarify (...)" }
// NOTE: no `result`, no `error`. `clarify === true` is the discriminator.
```

**(c) Error** — two forms: an `error` **field** (string) alongside a null `result` (a guard/exec failure,
still HTTP 200), or a top-level `{"error": "..."}` as the **whole body** (a server-level rejection:
401 no token, 500 exception, "payload too large", etc.).

**There is no `status` field in the HTTP body** and there is **no top-level `resolution` field.** The
`status` vocabulary lives only in the trace stream (§0.5). Per-cell resolution is streamed *winner-only*;
candidate lists and confidence scores are computed internally and **discarded** before responding — see
§4.3 for what that means for the MCP tool.

### 0.4 Identity = the verified Google `sub` (never client-supplied)

[`engine/auth.py`](engine/auth.py) `_verify_principal(token)` verifies a Firebase ID token and returns
`(sub, uid)`: the Google `sub` **is** the per-user Postgres schema, and the Firebase `uid` **is** the RTDB
stream key. The schema is always the server-verified `sub` — a client cannot choose another user's data
(no IDOR). **Local/test bypass:** set `AUTH_TEST_SUB=<name>` and `_verify_principal` returns `(name, name)`
before any Firebase code runs — no token required. `docker-compose.yml` sets `AUTH_TEST_SUB=localdev`.
This is the seam the MCP auth story hangs on (§5).

### 0.5 The live reasoning trace (this is what the "video player" plays)

When `RTDB_URL` is configured, the engine streams the trace to Firebase RTDB at `runs/{uid}/{jobId}` as it
computes ([`engine/trace.py`](engine/trace.py)). Node vocabulary (build the UI against this):

| Node | Payload | Meaning |
|---|---|---|
| `status` | `"running"` → `"resolving"` → `"running"` → terminal | lifecycle |
| `status` (terminal) | `"done"` \| `"clarify"` \| `"error"` | **the only three terminal states** — there is no `answered`, no `unresolved` |
| `resolve` | `{ "<cell>": "<qid> · <country>" }` (merge, winner-only) | cell → world-key lookups, streamed live |
| `resolve/{i}` | `{table,column,wtable,columns,rows,hlcol}` or `{table,column,unconnected:true}` | per-column resolution *slides* |
| `views/{i}` | `{op,label,sql,columns,rows}` | one per composed view, in stack order |
| `result` | `{columns,rows}` | final table |
| `clarify` | `{proposed,bindings,dropped,original_sql}` | the clarify payload |
| `error` | `"<message>"` | terminal error |

**`RTDB_URL` is optional.** Unset ⇒ the emitters become no-ops and the full answer (including `views`)
comes back in the HTTP body instead. The browser player already has a JSON fallback, so **the reasoning
playback works with or without RTDB** — live-streamed in prod, rebuilt from the response's `views` locally.

### 0.6 Config, ports, deploy (the one-place pattern)

All runtime knobs are env vars read in [`engine/config.py`](engine/config.py): `HOST`/`PORT` (default
`0.0.0.0:8080`), `WORLD_PG_*`, `RTDB_URL` (optional), `AUTH_TEST_SUB` (test-only), `DEVICE`,
`BASE_MODEL_ID` (the *local Qwen encoder* — unrelated to the orchestrator's model), `KB_MODEL_ROUTE`,
`PREREASONER_DATA_DIR`. A sibling process reaches the engine at `http://localhost:8080` (host) or
`http://engine:8080` (docker-compose network).

Deploy today: the engine is one Cloud Run service `prereasoner-api` (`Dockerfile` + `cloudbuild.yaml` +
`infra/main.tf`); `web/` is static on Firebase Hosting, which rewrites `/api/**` to the engine
(`web/firebase.json`). `docker-compose.yml` runs it all locally (`db` = pgvector Postgres, `engine`, and a
one-shot `seed`). The serving deps in `requirements.txt` are stdlib-HTTP only — **`anthropic` and `mcp` are
not present yet**; the MCP server and orchestrator introduce them.

---

## 1. What v1 is (the division of labor)

A **Sonnet orchestrator** (`claude-sonnet-5`) is the conversational layer. PreReasoner is an **MCP tool**
it calls. The invariant to protect:

- **In the orchestrator (LLM):** conversation, multimodal ingest (PDF/Excel/image → CSV), clarification,
  tool selection, security, and — **in v1 only** — decomposing multi-hop questions into a sequence of
  single-hop tool calls.
- **In PreReasoner (the engine):** typing, resolution, join construction, operator selection, and
  **execution of each hop** as auditable SQL with a streamed trace.

The reasoning PreReasoner *exposes* in v1 is the **single-hop derivation** (typing → resolution → SQL for
one resolved step — the existing `/api/reason` behavior). What is *not* exposed is the **cross-hop
decomposition** — the LLM's choice of which hops, in what order. That is the honest boundary of v1.

**The product shape.** A chat app: a **chat sidebar on the left**, and a **video-player-style
reasoning panel filling the rest of the screen**. The user chats with the orchestrator; whenever a turn
required PreReasoner, that assistant message is **replayable** — pressing play re-plays the reasoning
trace (resolve → world-join → filter → aggregate → result) in the right-hand panel. See §6.

---

## 2. MCP tool surface

A thin, typed MCP server ([`mcp_server/`](mcp_server)) wraps the engine. It adds **no** learned steps and
**no** state — it forwards to `/api/reason` and shapes the response for a tool caller. Two tools ship in v1.

### Tool: `prereasoner_query` — the primary tool

Takes a **single-hop / directly-expressible** data question plus the user's tables (inline, matching the
engine), and returns the auditable answer.

**Input**
- `question` (string): one aggregate/filter/join over the user's tables joined to the world model.
  E.g. `"total amount in France"`, `"how many hospitals in Texas"`.
- `tables` (array of `{name, data}`): the user's CSVs, **inline** — same shape the engine takes
  (§0.2). *No `dataset_id`.* The orchestrator holds the CSVs for the session and passes them on each call.

**Output** (shaped by the MCP server from the engine response, so the orchestrator can both use the value
and surface the trace):
- `status`: `"answered"` \| `"clarify"` \| `"error"`. **This is a mapping the MCP server computes**, not a
  field the engine returns — derive it: `clarify === true` → `"clarify"`; an `error` (field or top-level)
  → `"error"`; otherwise `"answered"`. `clarify` and `error` are **first-class outcomes, not failures**
  (see routing discipline §3, rule 4).
- `answer`: the scalar/table lifted from `result` (`{columns, rows}`).
- `sql`: the exact SQL the engine executed (the audit artifact), when present.
- `views`: the reasoning stack (`[{op,label,sql,columns,rows}]`) when present — this is what the player
  renders when there is no live RTDB stream.
- `clarify`: the `{proposed, dropped, bindings, original_sql}` payload, when `status === "clarify"`.
- `trace`: `{ jobId, uid }` — the coordinates of the live RTDB stream for this call. The orchestrator
  generates `jobId` and passes it in the request; `uid` is the verified user. Construct the RTDB path as
  `runs/{uid}/{jobId}`. Omit/null when RTDB streaming is off (the player then uses `views`).

> **`resolution` is deliberately not in this list.** The invariant "every layer surfaces candidates +
> confidence" is a real goal, but the engine does **not** return candidates or scores today — it streams
> the winning QID only and discards the internal scores (§4.3). Exposing them is a *scoped engine change*,
> not something the MCP wrapper can synthesize. v1 surfaces the winner (via `views`/the trace); the
> candidates-and-confidence upgrade is tracked separately.

### Tool: `prereasoner_describe` — coverage hint before you route

Returns what PreReasoner *believes about the columns* so the orchestrator knows the coverage boundary
before it routes a question. **v1 implementation:** call `/api/dimension` (stateless, no auth) and return
its per-column named-dimension readout — i.e. "the model reads this column as *city* / *hospital* /
free-text / numeric." Input: `tables` (inline). Output: the `columns`/`rows` dimension evolution from
`/api/dimension`.

> **Honest scope limit:** `/api/dimension` tells you what each column *types to*, not which cells actually
> *resolved* to world entities (that needs the router + live world Postgres). A fuller "world-coverage"
> describe — which columns ground to which `knowledgebase."<type>"` tables and at what fraction — is a scoped
> engine addition (the routing state already exists inside `serve`; it just isn't serialized). v1 ships the
> `/api/dimension`-backed version and labels the limit.

*(There is intentionally no `prereasoner_upload` — the engine has no upload step; see §0.2.)*

---

## 3. Routing discipline — the actual crux of v1

v1's trustworthiness lives here, because **the LLM decides when to call the tool**, and the LLM is the
unreliable component. The failure modes are narrow and must be encoded in **both** the MCP tool
descriptions and the shipped orchestrator system prompt:

1. **Numbers buried in conversational questions.** *"Does our French revenue justify hiring in Europe?"*
   hides a must-be-right number inside a strategy question. The LLM will answer fluently and *estimate*
   the number instead of calling the tool. Route on **truth-bearing**, not surface form: *any* factual
   number about the user's data must come from a `prereasoner_query` call.

2. **Confidence bypass on recall.** If the data is already in context, the LLM will read it and compute
   in-head. That in-head arithmetic is exactly the unreliable thing the tool replaces. Rule: **"When a
   number about the user's data must be correct, you do not compute or recall it — you call
   `prereasoner_query`."**

3. **Follow-on math.** The LLM calls the tool, gets `270`, then computes `270 × 1.15` in-head. That
   post-processing is unaudited. Rule: follow-on arithmetic on a tool result is **another tool call**,
   not in-head work.

4. **Confabulation on failure.** When the tool returns `status:"clarify"`, the LLM's failure mode is to
   *fill the gap* with a plausible answer. Rule: **a `clarify` must be passed through to the user, never
   smoothed over.** The refusal is the product — it is the "asks instead of guessing" promise. Do not let
   the orchestrator override it.

Encode 1–4 in the MCP tool descriptions (so *any* orchestrator sees them) **and** in the shipped system
prompt. The limit is real: you cannot *guarantee* the LLM calls the tool — that is v1's irreducible
weakness and the reason v2 exists. Make deferral the default; do not claim bypass is impossible.

---

## 4. Multi-hop, statuses, and the resolution gap

### 4.1 Multi-hop in v1 (the part that is NOT auditable)

For *"the second name of the third person in gold tier"*, the orchestrator **decomposes** the question into
a sequence of `prereasoner_query` calls (resolve gold-tier set → order → take the third → project the
name), passing intermediate results between calls. Each call is auditable; the **decomposition is not** —
it is the LLM's forward-pass reasoning. Present the sequence of executed (traceable) steps, but do **not**
claim the *choice* of steps was derived. If the decomposition must be auditable, that is v2.

**Do not build a "separate explainability agent"** that narrates the LLM's plan. Post-hoc narration of a
black-box step is exactly the interpretability-by-explanation this project rejects (a second LLM can be
unfaithful). If the plan must be auditable, **derive** it (v2), don't narrate it.

Because the engine serializes on one lock (§0.1), sequence the hops — do not fire them concurrently.

### 4.2 Status mapping (engine → tool)

The engine has no `status` field in its HTTP body; the trace has terminal states `done|clarify|error`. The
MCP server maps to the tool's `status`:

| Engine signal | Tool `status` |
|---|---|
| `clarify === true` | `"clarify"` |
| `error` field non-null, or top-level `{error}` body | `"error"` |
| otherwise (has `result`) | `"answered"` |

There is no native `"unresolved"` state. If you want to distinguish "couldn't resolve an entity" from other
errors, derive it in the MCP server (e.g. empty `result` + a resolve trace with no winner) — but treat it
as a flavor of `clarify`/`error`, not a new engine state.

### 4.3 The resolution gap (why `resolution` isn't returned)

`entities.py` computes an embedding similarity per candidate and `router.py` computes a per-column
`confidence`, but callers keep only the winning leaf/QID — the scores are dropped and the bridge tables
store no score column. Neither the HTTP response nor the trace carries candidates or confidence. So the
ROADMAP invariant "surface candidates + confidence" is **not yet met at this layer**. v1 is honest about
this: it surfaces the winner. Threading candidates+confidence into a top-level `resolution` field is a
scoped engine change (add a ranked list + score to the resolve path and to `serve`'s return) tracked for
after launch.

---

## 5. Identity & security — where the token comes from (the load-bearing decision)

The MCP layer must **pass the verified identity through** to the engine so the engine derives the per-user
schema itself (no IDOR). Concretely:

- **The browser authenticates with Firebase** (existing `web/` flow, `firebase-init.js`) and sends its
  Firebase **ID token** to the orchestrator on every chat request.
- **The orchestrator holds that token for the session** and must not let the LLM see or choose it —
  identity never flows through a tool *argument*. Instead, the orchestrator spawns the **MCP server with
  the user's token in its environment** (`ENGINE_BEARER_TOKEN`) for that session; the MCP server attaches
  it as `Authorization: Bearer <token>` on its engine calls. (stdio MCP servers are cheap to spawn
  per session; if you use MCP-over-HTTP instead, pass the token as a per-request transport header — same
  principle, token out of the LLM loop.)
- **Trace ownership:** the RTDB stream is `runs/{uid}/{jobId}`, owner-read-only by the verified Firebase
  `uid` (`web/database.rules.json`). The orchestrator returns `{jobId, uid}` to the browser so the
  reasoning panel subscribes to the correct, permitted path.
- **Local dev:** the engine runs with `AUTH_TEST_SUB=localdev`, so the MCP server needs no token and the
  whole browser → orchestrator → MCP → engine loop works with no Google sign-in. `RTDB_URL` stays unset
  locally, so the reasoning panel rebuilds from the response's `views` instead of streaming.
- **The orchestrator returns the answer + trace, not a dump of the user's tables.** Keep tool outputs
  scoped to the query.

---

## 6. The chat + reasoning-player UI

**Layout:** a fixed-width **chat sidebar on the left** (the conversation — user turns and assistant
turns), and the **reasoning panel filling the rest of the screen**. The reasoning panel *is* the existing
`web/public/reason.html` player, lifted into the right pane. Each assistant turn that called
`prereasoner_query` is **replayable**: selecting it (or pressing play) loads that turn's trace into the
panel and plays it like a video — input tables → per-column resolution slides → the view stack → the
result — with play/pause, ◀/▶ step, and a scrubber.

### 6.1 Reuse as-is (from `web/`)
- **`lib/table-render.js` `tableBubble(cols, rows, label, opts)`** — the table/result/resolution renderer.
- **`lib/shared.js`** — `esc`, `parseCSV`, `slug`, `sqlTokens`, `oplabel`/`OPLBL`, `PLAY`/`PAUSE`/`SPINNER`,
  `SS`, `API_BASE`.
- **`lib/firebase-init.js`** — `ensureSignedIn()`, `window.ensureToken()`, `window.__uid`, and
  **`window.subscribeRun(uid, jobId, cb)`** with callbacks `onStatus/onResolve/onView/onResult/onClarify/
  onError` on `runs/{uid}/{jobId}`. This is exactly the streaming contract the panel needs.
- **`lib/config.js`**, **`web/firebase.json`** (`/api/**` rewrite), **`web/database.rules.json`**.
- **The player algorithm** from `reason.html`: the slide-index arithmetic (slide `0` = inputs;
  `1..nR` = resolution slides; `nR+1..LAST` = views), `render`/`step`/`scrubClick`/`togglePlay`/
  `startPlay`/`stopPlay`/`buildTicks`, the `SEEN`/`SEEN_R` dedupe (RTDB re-delivers `onChildAdded`
  children), the `markDone`→`finalize` 400ms guard, and the HTTP-JSON fallback (`renderFromJSON`).

### 6.2 Build new
- **Chat sidebar + in-page turns.** Today the app is one-question-per-page, passing state through
  `sessionStorage` and navigating with `location.href`. The chat UI keeps an in-page array of turns and a
  chat transcript. No full-page navigation.
- **Per-turn playback state.** `reason.html` keeps the player state (`VIEWS/RESOLVES/J/S/LAST/AUTO/
  SETTLED`) as module-level singletons — one run per page. Refactor these into a **per-turn record**
  `{ question, inputs, resolves, views, result, sql, jobId, last }` so the panel can replay *any* past
  assistant turn by loading that record.
- **Two data sources for the panel, one player.** Live (prod): subscribe via `subscribeRun(uid, jobId,…)`.
  Fallback (local, or replay after the fact): rebuild from the tool result's `views`/`result`/`sql` (the
  existing `renderFromJSON` path). The orchestrator returns both `{jobId, uid}` and the full engine
  `views`/`result`/`sql` per `prereasoner_query` call, so the panel works either way.
- **Multiple subscriptions.** `reason.html` tracks one `UNSUB`; the chat UI may have several turns
  streaming — track one unsubscribe per in-flight turn.
- **Inline clarify.** Today clarify is a separate `clarify.html` page. In chat, render a `clarify` outcome
  as an assistant turn (the `proposed` rephrasing + what was `dropped`) instead of navigating away —
  preserving rule 4 (never smoothed over).

---

## 7. Architecture & deployment

```
Browser (chat UI, web/public/)
  │  Firebase sign-in → ID token
  │  POST /chat  {message, tables, history} + Bearer <token>     ┌──────────────────────────┐
  ▼                                                              │  reasoning panel subscribes│
Orchestrator  (Sonnet, anthropic SDK)  ── MCP client ──┐        │  runs/{uid}/{jobId} (RTDB) │
  │  runs the Sonnet tool loop; holds the user token    │        └─────────────▲──────────────┘
  │  spawns MCP server with ENGINE_BEARER_TOKEN=<token> │                      │ trace
  ▼                                                     ▼                      │
PreReasoner MCP server  ──HTTP (Authorization: Bearer)──▶  engine (prereasoner-api)
  prereasoner_query → POST /api/reason                      POST /api/reason / world / dimension
  prereasoner_describe → POST /api/dimension                (unchanged; the auditable core)
```

- **Engine — unchanged.** Do not co-locate the orchestrator/MCP in the engine container: it is heavy
  (~2 GB resident models, one in-process lock, scale-to-zero) and its health probe gates on model load.
- **MCP server** ([`mcp_server/`](mcp_server)) — a small Python package using the official `mcp` SDK
  (stdio transport for v1). Calls the engine over HTTP at `ENGINE_BASE_URL` (default
  `http://localhost:8080`; `http://engine:8080` in compose; the Cloud Run URL in prod). Reads
  `ENGINE_BEARER_TOKEN` from its env (set per session by the orchestrator; absent locally with
  `AUTH_TEST_SUB`).
- **Orchestrator** ([`orchestrator/`](orchestrator)) — Python + `anthropic` SDK, `claude-sonnet-5` with
  adaptive thinking. It is an MCP client of the PreReasoner MCP server, exposes a streaming `/chat`
  endpoint to the browser, generates a `jobId` per `prereasoner_query` call, and ships the
  routing-discipline system prompt. New config (add to `engine/config.py`'s one-place pattern):
  `ANTHROPIC_API_KEY` (call-time helper, mirrors `kb_pg_password()`), `ANTHROPIC_MODEL`
  (default `claude-sonnet-5`), `ENGINE_BASE_URL`.
- **Deploy** — one new Cloud Run service (`prereasoner-chat`) alongside `prereasoner-api`; the Anthropic
  key as a new Secret Manager secret injected like `KB_PG_PASSWORD`
  ([`infra/orchestrator.tf`](infra/orchestrator.tf)); the image built by
  [`cloudbuild.orchestrator.yaml`](cloudbuild.orchestrator.yaml) (tests-gated); and a sibling Firebase
  Hosting rewrite (`/chat` → `prereasoner-chat`) *above* the `/api/**` block
  ([`web/firebase.json`](web/firebase.json)). The MCP server is **not** a separate service — the
  orchestrator spawns it as a stdio subprocess inside the same image
  ([`Dockerfile.orchestrator`](Dockerfile.orchestrator)).

### Running it — one dev server (no Docker, no cloud)

The chatbot + MCP server + chat UI run as **one process**; the heavy engine (2 GB of models + the
seeded world Postgres) is its own service by design (ARCHITECTURE §3). So one command runs the whole
front half locally, and it talks to a real engine:

```
python -m orchestrator.server      # serves the chat UI + POST /chat on http://localhost:8090
```

It reads the repo `.env` (auto-loaded by `engine/config.py`). Two modes, switched by `ENGINE_BASE_URL`:

- **Real inference (default):** `ENGINE_BASE_URL=<the deployed engine URL>` → real answers from the
  live engine + world DB. `GET /config` reports `authMode: firebase`; the browser signs in with Google
  and that token flows through to the engine. Nothing to run but this one command.
- **Offline / no sign-in:** run the fixture engine (`python -m tests.stub_engine`) and set
  `ENGINE_BASE_URL=http://127.0.0.1:8080` → `authMode: test` (the `AUTH_TEST_SUB` bypass), so the whole
  chat + reasoning-player loop works with no network and no Google sign-in. For CI/dev only.
- **Fully local incl. the engine** needs a local Postgres + a one-time world-data seed
  (`docker compose up` + `docker compose --profile seed run --rm seed`, then `ENGINE_BASE_URL=
  http://localhost:8080`). This is the heavy path — the engine loads ~2 GB of models and the seed is
  ~15–45 min; use it only when you want zero remote dependency.

---

## 8. Carry-over invariants (from ROADMAP.md — do not relax)

- **Determinism where structure exists** (FK discovery, join assembly, SQL) stays deterministic. The MCP
  wrapper and orchestrator add **no** learned steps to the core.
- **No generation in the core.** The engine derives the number/SQL; the orchestrator may generate prose
  *around* the derived answer, but never the number.
- **The clarify gate is a feature**, surfaced as `status:"clarify"` and rendered inline. Protect it end to
  end (rule 4).
- **Candidates + confidence** is the one invariant v1 does **not** yet meet at the resolution layer (§4.3);
  it is a scoped engine change, explicitly deferred — not silently dropped.

---

## 9. Done when

- The orchestrator (Sonnet, via the MCP server) can: take a question + inline tables, ask a single-hop
  question, and get back a value + SQL + view stack + a live/replayable trace.
- A multi-hop question executes as a **sequence** of auditable single-hop `prereasoner_query` calls.
- A deliberately ambiguous query returns `status:"clarify"` and it **passes through to the user intact**
  (adversarial test — no confabulated answer).
- The routing-discipline rules (1–4) are present in **both** the MCP tool descriptions and the shipped
  system prompt.
- The chat UI shows a left sidebar + right reasoning panel, and **each PreReasoner-backed assistant turn
  replays its reasoning** (live via RTDB in prod, from `views` locally).
- No "explainability agent" exists. Exposed reasoning is derived (single-hop); the multi-hop decomposition
  is labeled opaque, not narrated.
- The full test suite (engine + MCP + orchestrator routing discipline + UI) runs green locally and on
  deploy.
