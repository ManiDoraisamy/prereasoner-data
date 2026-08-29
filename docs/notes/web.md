# web/ — frontend notes

> This is a chronological implementation log and older entries describe the UI as it existed on their date.
> The current contributor contract is [../../web/README.md](../../web/README.md); architecture facts belong in
> [../ARCHITECTURE.md](../ARCHITECTURE.md).

`web/` is the static Firebase Hosting frontend. It talks to the engine (ONE Cloud Run service,
`prereasoner-api`) through a single Hosting rewrite `/api/**`, exposing `POST /api/reason`,
`POST /api/knowledge`, `POST /api/dimension`, `POST /api/converse`, `GET /healthz`. RTDB streaming
paths (`/runs/{uid}/{jobId}`) carry the live trace. This note records the page/lib structure and the
deployment identifiers self-hosters need to change.

## Pages

In `web/public/`: `index.html`, `reason.html`, `knowledge.html`, `picker.html`, `404.html`,
`styles.css`, `login_logo.svg`, plus `anthropic-paper.svg` / `interpretability-blog.svg` (static
cards published for the prereasoner.com marketing site; the app itself does not embed them).
`reason.html` and `knowledge.html` are thin shells over the shared workbook (`lib/workbook.js`);
`index.html` is the home/demo page — served at `/` and, via Hosting rewrites, at the
single-source landings `/sheets`, `/excel` and `/csv`, where the bare "+" button attaches that
one source directly; `picker.html` is the Google Sheets import flow, served at `/picker`. The
Picker's own path is irrelevant to Google — its browser API key is restricted by ORIGIN, not path
(measured 2026-08-29: any path under `chat.prereasoner.com` is accepted, `prereasoner.com` and
`localhost` are not) — so the landing keeps the memorable `/sheets`.

`/api/dimension` is exposed by the backend but no kept page calls it (it is used programmatically /
for the dimension model directly). It requires the same verified Firebase token as the reasoning routes.

## Endpoints

Pages call the same-origin `/api/**` routes: `reason.html` and `knowledge.html` POST to
`/api/reason` / `/api/knowledge` (with fallback retries) and fire a pre-warm `GET`; `index.html`
fires the pre-warm `GET` on the home page; `tests/regression.js` POSTs per test case.

## lib/ extraction map (shared frontend code)

| new file | kind | pulled out of | contents |
|---|---|---|---|
| `public/lib/config.js` | ES module | reason/knowledge/sheets inline module scripts | `firebaseConfig` (apiKey, authDomain, projectId, appId, databaseURL), `PICKER_API_KEY`, `PICKER_APP_ID` — with a comment block stating these are public client identifiers, not secrets, and what self-hosters replace |
| `public/lib/shared.js` | classic script | index/reason/knowledge | `esc`, `parseCSV`, `slug`, `sqlTokens`, `OPLBL`/`oplabel`, `SS.*` sessionStorage key constants, `PLAY`/`PAUSE`/`SPINNER` UI constants, `API_BASE` ('' = same-origin `/api/**`; settable for local engines) |
| `public/lib/firebase-init.js` | ES module | reason/knowledge + sheets init | `initializeApp` from `config.js`, exports `app`/`auth`/`db`, publishes `window.ensureToken` + `window.subscribeRun` (the RTDB `/runs/{uid}/{jobId}` trace subscription), exports `ensureSignedIn()` (redirect sign-in flow) |
| `public/lib/table-render.js` | classic script | reason/knowledge | unified `tableBubble(cols, rows, label, opts)` with `opts.hlcol` (resolution highlight), `opts.thExtra` (knowledge.html's dimension-tag hover popup), `opts.maxRows` |

Script-loading pattern per page (module/classic split):

- classic `<script src="lib/shared.js">` (+ `lib/table-render.js` on reason/knowledge) loads BEFORE
  the page's inline classic script; top-level `const`/`function` in classic scripts are visible
  to later classic scripts and to modules.
- the pages' `<script type="module">` is `import { ensureSignedIn } from
  "./lib/firebase-init.js"; if (await ensureSignedIn()) run();` (reason/knowledge). `run()` is a
  classic-script global, callable from the module. sheets.html imports `auth` from
  `firebase-init.js` and the Picker constants from `config.js` and keeps its own picker flow.
- index.html has NO firebase — it only loads `shared.js`.

Deliberate small behavior changes (parity notes):

1. `tableBubble` now always renders non-integer numbers to ≤3 decimals. reason.html already did
   this; knowledge.html previously did not (its table cells are almost always CSV strings, so this
   only affects numeric cells in streamed views — cosmetic).
2. sheets.html now pulls in the RTDB SDK module via `firebase-init.js` even though it doesn't
   use it (one shared init > a second init variant; ~40KB extra on that page only).
3. Every page's redundant `subscribeRun`/`ensureToken` copy is gone; behavior is byte-identical.
4. `qBubble()` (defined but unused in both players) kept as-is in the pages.

## firebase.json

- ONE Hosting → Cloud Run rewrite:
  `{"source": "/api/**", "run": {"serviceId": "prereasoner-api", "region": "us-central1"}}`.
- Cache-Control no-cache header glob is `**/*.@(html|css|svg|js)` so `lib/*.js` can't go stale
  behind Hosting's default 1h cache.
- An `emulators` block (hosting 5000 / auth 9099 / database 9000 / UI) for local dev.
- `hosting.public`, `cleanUrls: true`, ignore list, `database.rules`.
- `database.rules.json`: admin writes bypass rules; a signed-in user may read only
  `/runs/{own uid}`; clients never write.

## Current deployment identifiers (all public)

- Firebase/GCP project id: **`prereasoner-inference`** — project number `271377281957`.
- Cloud Run region: **`us-central1`**; service: **`prereasoner-api`**.
- Auth domain: `chat.prereasoner.com`; RTDB: `https://prereasoner-inference-default-rtdb.firebaseio.com`.
- Picker API key (referrer-restricted) + project number: in `lib/config.js`.

## What self-hosters must change

Everything is in two files, plus one CLI command (details in `web/README.md`):

1. `web/public/lib/config.js` — their Firebase web config (and Picker key/project number if
   they want the Google Sheets import; otherwise `picker.html` — the `/picker` flow, and the only
   consumer of `PICKER_API_KEY`/`PICKER_APP_ID` — just won't authenticate the Google Picker).
2. `web/firebase.json` — `serviceId`/`region` if their Cloud Run service differs from
   `prereasoner-api`/`us-central1`.
3. `firebase use <their-project-id>` (no `.firebaserc` is shipped).
4. Optional, local dev: `API_BASE` in `web/public/lib/shared.js` to hit a local engine directly
   (the hosting emulator proxies `run:` rewrites to the DEPLOYED Cloud Run service, never localhost).

## Regression suite

`web/tests/regression.js` — POSTs test cases against `/api/reason`. Its header documents that it is
auto-generated (the generator lives in `training/tools`) and how to run it (browser console on
`/reason` while signed in, `await runRegression()`). It lives outside `public/` so it is not deployed.

## Verification

- Internal links only reference kept pages and routes (`/`, the single-source landings `/sheets`,
  `/excel` and `/csv`, `reason`, `knowledge`, `picker`).
- No page re-defines anything now in `lib/`; the Firebase config literal appears only in
  `lib/config.js`.
- `node --check` passes on both classic libs, both ES-module libs, every inline script of every
  page (classic and module parsed separately), and `tests/regression.js`.

## Post-E2E fixes (2026-07-04, verified in real Chrome against a local engine + live Cloud SQL)

- **Early JSON fallback** (reason.html, knowledge.html): the POST body is parsed once
  (`parseBody`) and raced against the stream — if the body lands and the stream shows no
  life for 3 s, `renderFromJSON` fires immediately instead of waiting for the 90 s safety
  net (which remains, with its cold-start retries, now consuming the same parsed promise).
  Verified: result renders seconds after the engine responds with RTDB disabled.
- **`API_BASE` is now a localStorage override** (`pr_api_base`), not a source edit — local
  testing no longer requires touching lib/shared.js.
- **Auth bypass for local testing** (`sessionStorage pr_test_auth`, firebase-init.js) is
  gated to localhost hostnames and pairs with the engine's `AUTH_TEST_SUB`.
- E2E verified: index demo (France = 270 with full join→world→filter→aggregate trace),
  knowledge page (France cities), clarify flow, info panel, 404, sheets (up to Google Picker).

## Workbook redesign + sign-in loop fix (2026-07-11, live)

- **reason.html is now the read-only WORKBOOK** (per the inbound redesign spec): green input
  sheets (user tables, never touched), blue derivation sheets (one per streamed view, named
  columns, SQL disclosure per sheet), grey reference sheets (streamed resolves) hidden behind a
  "Reference (n)" toggle, bottom tab strip, chat rail on the RIGHT with the question, live
  status, step links (click -> sheet) and the result card. Streaming/early-fallback/90s-safety/
  clarify semantics preserved from the trace player. Verified live on chat.prereasoner.com
  (France demo: 4 streamed steps + 1 reference sheet, answer 270).
- **Google sign-in redirect loop FIXED**: authDomain now follows location.hostname on
  hosting-connected domains (third-party-storage partitioning made cross-domain redirects lose
  the pending sign-in -> infinite account chooser, reported on prereasoner.com), plus loop
  breakers in firebase-init.ensureSignedIn and sheets.html (a returned-but-signed-out redirect
  shows a retry UI instead of re-redirecting). knowledge.html/reason.html catch the throw.

## Shared workbook lib + local dev server flow

- The workbook is SHARED CODE: `lib/workbook.js` (logic, parameterized by `window.WB_CONFIG`:
  endpoint/status strings/demo data) + the workbook styles in `styles.css`. `reason.html` and
  `knowledge.html` are thin shells.
- Local dev via the orchestrator (localhost:8090): its static server emulates Firebase Hosting
  cleanUrls (`/reason` -> `reason.html`), and `ensureSignedIn` auto-detects the orchestrator's
  `GET /config` authMode "test" on localhost — the home-page click-through works with no console
  flags.
- Verified: localhost:8090 home -> arrow -> `/reason` workbook (France demo, 270) and `/knowledge`
  workbook (cities demo), plus live `chat.prereasoner.com/knowledge` with real auth + streaming
  (4 steps + a reference sheet, 270).

## Chat rail + workbook UX pass (2026-07-11, live)

- The rail IS a chat now: turns (user bubble right-aligned, no speaker labels — ChatGPT-minimal),
  an "Ask a follow-up…" input; each follow-up archives the previous turn to one answer line and
  re-runs the workbook on the same attached tables (derived/reference sheets replaced, green
  sheets untouched; RUN-guard supersedes stale async callbacks). The separate Sonnet/MCP chat
  surface (served by the `orchestrator/`) is independent of the workbook rail.
- Tabs are Excel-style: attached under the square-bottomed sheet card, rounded DOWN.
- Sheet order everywhere = input -> Reference (collapsed) -> steps.
- Human names only: tabs/band/steps use the step label ("join orders + customers"), never
  v1/b1/step_1 (internal names remain visible only inside the SQL disclosure).

## Launch UX pass (2026-07-12, live)

- App layout: purple gradient is HEADER-ONLY on the workbook pages (<body class=app>); the
  workspace is white edge-to-edge — sheet grid flush left, chat rail flush right, hairline
  dividers, no floating cards.
- Bottom tab bar is Google-Sheets-style: light strip, active tab WHITE and fused with the
  sheet above (covers the strip border), ‹ › arrow paging (native scrollbar hidden,
  auto-disable when no overflow, active tab scrolled into view), reference sheets are
  ordinary always-visible tabs (collapse toggle removed).

## Conversations: chat schema + conversation-scoped data schemas (2026-07-12, live)

- Identity model changed: the working Postgres schema is the CONVERSATION id (c_<32 hex>), not
  the user. A new `chat` schema holds user_profile (verified Google sub) / conversation
  (initial_prompt, tables jsonb, created_at) / user_conversation (ownership link). engine/
  conversations.py owns this; db/init.sql has the DDL (engine also ensures it at runtime).
- Security (IDOR): the user id is always the verified token subject; a client-supplied
  conversation_id is honored ONLY after chat.user_conversation confirms ownership, else 403.
  A malformed/injection id fails the c_<32hex> shape guard. Verified: cross-user POST→403,
  cross-user GET→404, "chat; DROP TABLE"→403.
- Engine API: POST /api/reason and /api/knowledge accept+echo conversation_id; GET /api/conversations
  (drawer list, ownership-scoped) and GET /api/conversation?id= (re-open: opening prompt + stored tables).
- Frontend: header shows the conversation's opening question (truncates); hamburger drawer lists
  past conversations and opens them (rehydrates stored tables + reloads); a query from home or
  "+ New" starts a fresh conversation. conversation_id round-trips via sessionStorage.
- GCS archival (the "later" capability): db/sync/archive_conversation.py serializes a
  conversation's schema to gs://$GCS_BUCKET/conversations/<id>.sql.gz (pg_dump→gzip→GCS, optional
  DROP) and restores it (download→psql). Written + self-consistent; NOT yet E2E-tested against a
  live bucket.

## Conversational layer: Sonnet fallback + presentation, in-chat (2026-07-17, live)

The rail now answers NON-data messages IN the conversation instead of redirecting. Full design:
docs/ARCHITECTURE.md §10.

- **Backend.** New `POST /api/converse` (engine/server.py `_post_converse` → engine/converse.py
  `reply()`) returns one short Sonnet reply. Two modes: FALLBACK (a clarify rephrasing or a meta
  question) and PRESENT (wrap an already-computed answer in human words, verbatim, never a new
  number). Optional: no `ANTHROPIC_API_KEY` ⇒ 503 ⇒ the client uses its built-in "did you mean"
  text. Three engine signals drive it: the COVERAGE PRE-GATE (`low_confidence`, no data intent),
  `clarify`, and `present` (real answer + emotional phrasing, `_human_tone`). RTDB streams
  `low_confidence` and `present` alongside the existing terminal nodes (trace.py `stream_final`).
- **Frontend (lib/workbook.js, lib/firebase-init.js).** `conversationalReply()` renders the Sonnet
  reply in the rail; clarify adds a one-tap **Run** button. `tryPresent()` routes a computed
  answer through /api/converse race-safely (present/result/status arrive on separate RTDB nodes)
  and keeps the derivation in the panel. The previous turn's derivation/reference sheets are kept
  ("stale") across a conversational follow-up and retired only when a new data query builds its own
  (`dropStale`). subscribeRun added `onLowConfidence`/`onPresent`.
- **clarify.html DELETED.** It was the old "did you mean" redirect target; nothing navigated to it
  once clarify moved in-chat (no code set `SS.CLARIFY`). Removed the page + the dead `SS.CLARIFY`
  key from lib/shared.js. The page map in web/README.md was corrected.
- Verified: detector precision (12/12 clean data queries, 11/11 emotional fire, schema-word
  collisions treated as data), the present race state machine (all trigger orderings present once
  with a real answer, 0 Sonnet calls on plain queries), live Sonnet output uses the exact computed
  number and never fabricates. Deployed: engine revision 00013-4k5, Firebase Hosting.
