# PreReasoner — Full Code Review Findings

> Started: 2026-07-19 · Reviewer: Claude Code (adversarial multi-agent review)
> Method: 4 parallel review workflows by area, each dimension-sharded, every finding independently
> adversarially verified (a skeptic agent must trace a concrete failing input in the real code before
> a finding is accepted). Only CONFIRMED findings are listed. Status updates live as areas complete.

## Status

| Area | Scope | Status |
|---|---|---|
| Area | Scope | Status |
|---|---|---|
| A. Security & Auth | engine auth/server/conversations/master/admin/pg, orchestrator server, mcp_server, firebase rules | ⚠️ 2 confirmed (both FIXED); SQLi dimension rate-limited → **re-run pending** |
| B. Engine Core | compose, world_query/compose/tables, tables, converse, bridge, trace, router, joins, entities, resolve | ⚠️ 3 confirmed (1 dim done); 4 dims rate-limited → **re-run pending** |
| C. SQL Subsystem | sql_*.py (17 files, ~8k lines) | ⏳ rate-limited (empty) → **re-run pending** |
| D. Frontend & Infra | web/public (workbook.js, shared.js, html, css), db/sync, Dockerfiles, cloudbuild, terraform, firebase.json | ✅ done — 8 confirmed |

Severity: **P0** exploitable/security or data-loss · **P1** wrong results/crash in normal use · **P2** wrong behavior in edge cases · **P3** hygiene/robustness

### Fix status
- **FIXED (code, pending deploy):** A-1, A-2, B-1, B-2, B-3, D-1, D-3, D-4, D-5, D-7, D-8. (11 of 13.)
- **NEEDS USER ACTION:** D-2 — rotate the live API keys + DB password (I can't issue/revoke credentials).
- **DEFERRED (risky infra change):** D-6 — RTDB IAM has no data-plane-only permission; a naive custom role breaks trace streaming. Documented; needs a tested rollout, not a blind edit.
- **REVIEW GAPS (re-running):** SQL subsystem (all 5 dims), engine-core (4 dims), security SQLi dim — findings from these will be appended + fixed as they land.

---

## A. Security & Auth

### A-1 · P2 · Unauthenticated `POST /chat` — anonymous denial-of-wallet on the owner's Anthropic key
- **Files:** `orchestrator/server.py:92-135` (`_chat`; token used only opportunistically at 106-116), paid call at `orchestrator/orchestrator.py:143`; exposure via `infra/orchestrator.tf:145-149` (allUsers invoker) + `web/firebase.json:12-15` (`/chat` rewrite).
- **Scenario:** `_chat()` never verifies a token before running — it reads `message`/`tables`, computes `token=self._bearer()` (may be `None`), and unconditionally calls `run_chat()`, which immediately does `client.messages.create(...)` and loops up to `MAX_TOOL_ROUNDS=8` Sonnet calls (`max_tokens=8192` + thinking). The token is only touched for optional RTDB streaming, and only when `turn_id` is present. An anonymous attacker sends `curl -s https://<host>/chat -d '{"message":"do a long multi-step analysis..."}'` with **no** Authorization header, in a loop → each request drives up to 8 paid Anthropic calls billed to the owner's key. The engine's `/api/reason` 401s the downstream tool call (so **no data leak / no DB mutation**), but the Sonnet inference already ran and is billed. Net: unbounded third-party API spend + orchestrator instance exhaustion by an anonymous caller.
- **Fix:** At the top of `_chat()`, gate on identity before any work — `sub, uid = _verify_principal(self._bearer()); if not sub: self._send(401, ...); return` — unconditionally (not gated on `turn_id`), mirroring `engine/server.py` `_post_world`. The orchestrator already imports `engine.auth._verify_principal` for streaming. The allUsers invoker + rewrite are intentional (auth is designed at the app layer); the app-layer gate is the fix. Optionally add a per-identity rate limit.

### A-2 · P2 (latent — NOT currently exploitable) · `AUTH_TEST_SUB` is an unguarded full-auth-bypass
- **Files:** `engine/auth.py:30-32` (`_verify_principal` returns `(test, test)` with **no token check** when set), `engine/config.py:91-94` (`auth_test_sub()` — plain `os.environ.get`, **no prod guard**).
- **Verified inline:** `AUTH_TEST_SUB` is **not set** on prod `prereasoner-api` or `prereasoner-chat` (checked via `gcloud run services describe`). It appears only in `docker-compose.yml:54` (`localdev`) and `.env.example:25`. So auth is **not** bypassed in production today.
- **Why it still matters (defense-in-depth):** the bypass is one env var away from total compromise — if `AUTH_TEST_SUB` were ever set in a prod-like deploy (copy-paste from compose, a bad `gcloud run deploy --set-env-vars`, a leaked config), **every** request authenticates as that fixed sub with no token, reading/writing/deleting that principal's conversations + master data, and there is no second line of defense. The code trusts operator discipline alone.
- **Fix:** hard-gate the bypass so it cannot activate in prod. E.g. in `auth_test_sub()`, return `None` unless an explicit non-prod signal is present (`os.environ.get("ALLOW_AUTH_BYPASS") == "1"` AND no `K_SERVICE`/Cloud-Run marker), or refuse at startup if `AUTH_TEST_SUB` is set while a Cloud Run env is detected (`K_SERVICE` is always set on Cloud Run). At minimum, log a loud `CRITICAL` on every request when the bypass is active.

### SQLi dimension (security) — NOT YET REVIEWED (rate-limited on first pass); re-running below.

_more to come_

## B. Engine Core

_Only 1 of 5 dimensions completed before rate-limiting (resolution-joins); compose-aggregate, trace-fidelity, crashes-robustness, state-resource were rate-limited and are being re-run. 3 confirmed so far._

### B-1 · P1 · Hybrid country filter compares a country QID against a country NAME → every country-filtered hybrid query returns ZERO rows
- **File:** `engine/world_query.py:483` (filter emit), with `:663-669` (serve passes `cr[0]`), `entities.py:104/127` (`_resolve` returns a **QID** post-migration), `build_words.py:48` / `build_world.py:40` (bridge `country` column stores the country **NAME**).
- **Scenario:** Ask "who complained about bad delivery in France" over `{name, city, remarks}`. `_resolve(question,"country")` now returns `'Q142'` (qid), which `_serve_hybrid` puts into `AND c."country" = 'Q142'` — but the connected bridge's `country` column holds `'France'` (a name). The `EXISTS` never matches → **empty result** instead of the France customers. Fails silently (well-formed WHERE, truthy country → no fallback). The stale comment at `:663` (`# ("France", sim, surface)`) documents the pre-migration name-return assumption `_serve_hybrid` still relies on. Same QID-as-context also defeats `_city_bridge_sql` disambiguation (`entities.py:277`).
- **Fix:** Map `cr[0]` (qid) back to the canonical country **name** before passing into `_serve_hybrid` (look up `world."words"` for the qid) so the filter compares `'France' = 'France'`; or store the qid in the bridge `country` column and keep qid-vs-qid. Also fix `_city_bridge_sql` `pick()` (`entities.py:277`) to compare against the country name. **This is the flagship hybrid demo path — high priority.**

### B-2 · P2 · `ambiguities()` matches the uploaded value against the qid key column → same-name world entities never flagged ambiguous
- **File:** `engine/pg.py:150` (and `:155` non-country branch); `entities.py:381` caller; `word_city.json:3` (`key='qid'`).
- **Scenario:** A city question with an ambiguous value (e.g. "Springfield") + no disambiguator. `ambiguities()` runs `... WHERE lower("qid") = 'springfield'` — comparing a city name to the qid column, which never matches → `opts` always empty → **no ambiguity warning ever emitted**. The system silently picks highest-population and presents it confidently. Misrepresents the warnings/trace the product promises.
- **Fix:** Match on the entity **name** column for qid-keyed tables (`WHERE lower("name")=%s`), or count distinct qids per normalized name via `world."words"` and flag when >1. Apply to both branches (`:150`, `:155`). `word_state` (key already `name`) needs no change.

### B-3 · P2 · `is_key()` accepts a non-unique column as an FK target (0.98 tolerance) → join fan-out inflates SUM/COUNT
- **File:** `engine/relations.py:51`.
- **Scenario:** A dimension sheet whose `name` has 2 duplicate 'Paris' among 100 rows (98/100 = 0.98) + a fact sheet with `city`⊆names and an `amount`. `is_key()` returns True (`>= 0.98`) despite the dup; `discover_fks` records `fact.city → dim.name`; the join matches each 'Paris' fact row against BOTH dim rows → **doubles** those rows → `SUM(amount)`/`COUNT(*)` over-counts. The compose/SQLite path (`joins.py`) guards this with strict `len(set)!=len(vals)`; the live `relations.py` path does not.
- **Fix:** Require exact uniqueness for an FK target — change `len(set(nn))/len(nn) >= 0.98` to `len(set(nn)) == len(nn)` (matching `joins.discover_fks`), keeping the no-nulls / min-cardinality checks.

## C. SQL Subsystem

_Re-run reached 1 confirmed P1 before hitting the hard session limit (resets 11am Europe/Paris). The ast-serialization review dim + several aggregation/extrema/ranking/expansion verifies did not complete — **still a gap**._

### C-1 · P1 · ASC extrema (lowest/cheapest/earliest) returns a NULL row as the minimum — **FIXED + verified**
- **File:** `engine/sql_extrema.py:177` (`_row_superlative_candidates`) + `:335` (`_direct_row_superlatives`); root cause: `OrderTerm`/`_render_query` emit bare `ASC` with no NULLS ordering and no `IS NOT NULL` guard.
- **Scenario:** `products(id,name,price)` with `Date`=NULL. "which product has the lowest price" → `... ORDER BY price ASC LIMIT 1`. SQLite sorts NULLs first under ASC → returns **Date** (NULL) instead of **Banana** (10). Execution reranking can't rescue it (the all-null penalty only fires when *every* value is NULL). Verified end-to-end with real sqlite3.
- **Fix applied:** both row-superlative construction sites now AND `Comparison(target.column, "IS NOT", Literal(None))` into the WHERE, so a NULL ordering value can never be the extreme (no-op when there are no NULLs; correct for ASC and DESC). Verified locally: renders `WHERE "price" IS NOT NULL ... ORDER BY "price" ASC LIMIT 1` → returns Banana; the old unguarded query returned Date.

## D. Frontend & Infra

_8 confirmed (1 false-positive rejected: a claimed stale-RTDB-replay race in addCall — verifier could not build a reachable repro)._

### D-1 · P1 · Attribute-injection XSS via the cell `title="…"` (shared `esc()` doesn't escape `"`)
- **Files:** `web/public/lib/workbook.js:87` (renderGrid emits `... title="'+esc(val)+'">'+esc(val)+'</td>`), `web/public/lib/shared.js:24` (`esc()` replaces only `& < >`, **not** `"`).
- **Scenario:** `val = fmt(row[ci])` is a user/server value. A cell equal to `" onmouseover="alert(document.cookie)` breaks out of the `title` attribute → `title="" onmouseover="alert(...)">` — a live handler on the tabbable `<td>`. Fires on hover/focus. Reaches the DOM from: an uploaded CSV cell, a **server world-DB row** on a read-only ref sheet (same renderGrid path), and a **deep-linked stored conversation** re-rendering its persisted tables. No CSP in `firebase.json` to blunt it. (Note: `admin.html`'s own `esc()` *does* escape `"`; workbook.js is the outlier putting an untrusted value in an attribute with the text-only `esc()`.)
- **Fix:** Add `escAttr(s){return esc(s).replace(/"/g,'&quot;');}` in shared.js and use it for the `title` attribute (line 87). Consider adding a CSP header as defense-in-depth. **Note: this is in the renderGrid code recently reworked — worth fixing immediately.**

### D-2 · P1 · Live, self-documented-as-compromised secrets in plaintext `.env`, unrotated
- **File:** `.env:3-16` (gitignored + in `.dockerignore`/`.gcloudignore`, so NOT shipped into images — but the live-secret exposure stands).
- **Scenario:** `.env` holds real active creds: `HF_TOKEN`, `RUNPOD_API_KEY`, `ANTHROPIC_API_KEY`, and a **production** Cloud SQL credential `WORLD_PG_HOST=34.123.19.176` (public IP) + `WORLD_PG_PASSWORD=…`. The file's own header says these "sat in plaintext in the old repo" and must be rotated — i.e. already exposed in old git history AND still active. This matches the pre-publish key-rotation item in memory ([[prereasoner-oss-consolidation]]).
- **Fix:** Rotate the three API keys **now** (treat as leaked; revoke old), rotate the Cloud SQL password and pull it from Secret Manager at use time, and confirm old-repo history is purged. (DB password is network-guarded by zero authorized networks per `infra/main.tf`, so lower urgency than the API keys.)

### D-3 · P1 · `saveMaster` clears `sh.dirty` for edits made during the in-flight POST → silent lost update
- **File:** `web/public/lib/workbook.js:~480-491` (snapshot at ~484, unconditional `sh.dirty=false` at ~488).
- **Scenario:** Save snapshots rows, POSTs; the disabled Save button doesn't block **cell keyboard editing**. An edit made while the POST is in flight sets `sh.dirty=true`, but the `.then` unconditionally clears it → sheet shows "Saved", tab dot gone, `beforeunload` guard won't fire → that edit is lost silently on reload.
- **Fix:** Capture a signature of exactly what was persisted; on success only clear `dirty` if the current model still matches it (else leave dirty so the guard fires). Or make the sheet read-only while a save is in flight.

### D-4 · P2 · `loadMaster` / `surfaceUnresolved` race shadows SAVED master data with an empty sheet (clobber on next Save)
- **File:** `web/public/lib/workbook.js:~447-476` (`run()` calls `loadMaster()` un-awaited then `startRun()`).
- **Scenario:** If the reasoning turn finishes before the per-table master GETs resolve, `surfaceUnresolved()` finds `MDATA[key]` empty and surfaces a **blank** master sheet (`saved:false`), adding `key` to `MSEEN`. `loadMaster` then skips that key (`MSEEN.has(k)`), so the user's previously-saved attributes never appear; filling in + Save **replaces** the server copy → destroys saved data.
- **Fix:** Either `await loadMaster()` before `startRun()`, or in loadMaster's loop, when a saved table arrives for a key already surfaced as an unsaved shadow, **upgrade the shadow in place** (replace cols/rows/saved) instead of skipping.

### D-5 · P2 · ORCH follow-up can orphan into a NEW server conversation when `conversation_id` hasn't landed
- **File:** `web/public/lib/workbook.js:204` (send gate `SETTLED && (convId()||ORCH)`), server emits `conversation_id` **last** (`orchestrator/orchestrator.py:~206`).
- **Scenario:** ORCH re-enables send on `SETTLED` alone. Since `conversation_id` is written after `status:done` (and the HTTP body often lost to the 60s proxy timeout on cold starts), a fast follow-up POSTs `conversation_id:null` → server creates a **new** conversation; turns 1 & 2 split server-side, `/reason/<id>` URL never updates (only client HISTORY keeps the rail continuous).
- **Fix:** Gate the FIRST turn of a brand-new conversation on `conversation_id` in ORCH too (in `sendChat` and the button gate), or thread the client turnId as the conversation_id so the id is known before `status:done`.

### D-6 · P2 · Engine Cloud Run SA has project-wide `roles/firebasedatabase.admin` (only needs `/runs/{uid}/{jobId}` writes)
- **File:** `infra/main.tf:121-125`.
- **Scenario:** `firebasedatabase.admin` at project scope = manage every RTDB instance + (via Admin SDK) read/write ALL users' data, bypassing `database.rules.json`. The engine only writes the trace stream under `/runs/{uid}/{jobId}` (`engine/trace.py`). Paired with the allUsers invoker + LLM/text-to-SQL surface, a coerced write or leaked token has project-wide RTDB blast radius.
- **Fix:** Replace with a custom role granting only RTDB data-plane get/set/update (no instance create/delete/rule-management). (Admin SDK still bypasses rules regardless, so this is blast-radius reduction, not path isolation.)

### D-7 · P3 · Firebase ID token is sent to whatever origin `localStorage['pr_api_base']` names (XSS → durable token exfiltration)
- **File:** `web/public/lib/shared.js:11` (`API_BASE = localStorage.getItem('pr_api_base') || ''`, no origin allowlist).
- **Scenario:** `API_BASE` is prepended to every authenticated fetch (all send `Authorization: Bearer <freshToken>`). A same-origin write of `pr_api_base=https://evil.example` (via any XSS, e.g. D-1) makes every future token auto-ship cross-origin (getIdToken mints fresh tokens each call) — turns transient XSS into persistent credential theft.
- **Fix:** Gate `pr_api_base` on localhost only (mirror `firebase-init.js:74`/`config.js:24`): `const API_BASE = ((location.hostname==='localhost'||location.hostname==='127.0.0.1') && localStorage.getItem('pr_api_base')) || '';`

### D-8 · P3 · Firebase Hosting sets no security headers (no X-Frame-Options / CSP / X-Content-Type-Options)
- **File:** `web/firebase.json:22-27` (only a Cache-Control rule; repo-wide grep for these headers = 0 matches).
- **Scenario:** Every page (including the authenticated workbook + admin.html) is framable by any origin → clickjacking; no `nosniff`; no CSP to blunt the innerHTML/XSS sinks (D-1).
- **Fix:** Add a `source:"**"` headers entry with `X-Frame-Options: DENY` (or CSP `frame-ancestors 'none'`) and `X-Content-Type-Options: nosniff`. A full CSP is a valuable follow-up.
