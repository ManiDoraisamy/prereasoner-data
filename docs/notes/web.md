# web/ — frontend consolidation notes

How `web/` was produced from the private `prereasoner-inference` repo (2026-07-04), for the
open-source release. The backend counterpart: all Cloud Run services consolidated into ONE
service `prereasoner-api` exposing `POST /api/reason`, `POST /api/world`, `POST /api/dimension`,
`GET /healthz`, reached via a single Firebase Hosting rewrite `/api/**`. RTDB streaming paths
(`/runs/{uid}/{jobId}`) and the request/response JSON schemas are UNCHANGED.

## Kept / dropped

KEPT (in `web/public/`): `index.html`, `reason.html`, `world.html`, `clarify.html`,
`sheets.html`, `404.html`, `styles.css`, `login_logo.svg`, `interpretable.svg`.

DROPPED and why:

| item | reason |
|---|---|
| `public/analyze.html` | unreachable (nothing links to it), called dead endpoints |
| `public/dimension.html` | unreachable, called the retired `/infer-runtime20` dimension endpoint directly |
| `public/result.html` | unreachable dead-end page from an earlier flow |
| `firebase.json` rewrites `/infer-runtime10`, `/infer-runtime11` | pointed at retired Cloud Run services (`prereasoner-runtime10/11`) |
| `firebase.json` rewrites `/infer-runtime20`, `/infer-runtime20reason`, `/infer-runtime20w` | replaced by the single `/api/**` -> `prereasoner-api` rewrite |
| `functions/` (entire dir) | unused Cloud Functions stub + a committed Python venv; the engine is Cloud Run, not Functions |
| `firebase_serve.log`, `serve5000.log` | stray local logs |
| `.firebaserc` | intentionally NOT copied — self-hosters bind their own project with `firebase use <project-id>` (documented in `web/README.md`) |
| `public/regression20.js` | moved OUT of the deployed tree to `web/tests/regression.js` (updated, see below) |

Note: `/api/dimension` (was `/infer-runtime20`) is exposed by the consolidated backend but no
kept page calls it — its only caller was the dropped `dimension.html`.

## Endpoint rename

`/infer-runtime20reason` → `/api/reason`, `/infer-runtime20w` → `/api/world`, in:

- `reason.html` — `ENDPOINT` (POST + fallback retries) and the pre-warm `GET`
- `world.html` — same
- `index.html` — the pre-warm `GET` fired on the home page
- `tests/regression.js` — the POST per test case

`grep -r "infer-runtime\|runtime20\|runtime1" web/` → zero hits (verified).

## lib/ extraction map (~200 duplicated lines removed)

| new file | kind | pulled out of | contents |
|---|---|---|---|
| `public/lib/config.js` | ES module | reason/world/sheets inline module scripts (3 copies of the Firebase config; Picker key was inline in sheets) | `firebaseConfig` (apiKey, authDomain, projectId, appId, databaseURL), `PICKER_API_KEY`, `PICKER_APP_ID` — with a comment block stating these are public client identifiers, not secrets, and what self-hosters replace |
| `public/lib/shared.js` | classic script | index/reason/world/clarify | `esc`, `parseCSV`, `slug`, `sqlTokens`, `OPLBL`/`oplabel`, `SS.*` sessionStorage key constants, `PLAY`/`PAUSE`/`SPINNER` UI constants, `API_BASE` ('' = same-origin `/api/**`; settable for local engines) |
| `public/lib/firebase-init.js` | ES module | reason/world (2 near-identical copies) + sheets init | `initializeApp` from `config.js`, exports `app`/`auth`/`db`, publishes `window.ensureToken` + `window.subscribeRun` (the RTDB `/runs/{uid}/{jobId}` trace subscription), exports `ensureSignedIn()` (redirect sign-in flow) |
| `public/lib/table-render.js` | classic script | reason/world (2 diverged copies of `tableBubble`) | unified `tableBubble(cols, rows, label, opts)` with `opts.hlcol` (resolution highlight), `opts.thExtra` (world.html's dimension-tag hover popup), `opts.maxRows` |

Script-loading pattern per page (module/classic split preserved):

- classic `<script src="lib/shared.js">` (+ `lib/table-render.js` on reason/world) loads BEFORE
  the page's inline classic script; top-level `const`/`function` in classic scripts are visible
  to later classic scripts and to modules.
- the pages' `<script type="module">` shrank to `import { ensureSignedIn } from
  "./lib/firebase-init.js"; if (await ensureSignedIn()) run();` (reason/world). `run()` is a
  classic-script global, callable from the module. sheets.html imports `auth` from
  `firebase-init.js` and the Picker constants from `config.js` and keeps its own picker flow.
- index.html and clarify.html have NO firebase at all (unchanged) — they only load `shared.js`.

Deliberate small behavior changes (parity notes):

1. `tableBubble` now always renders non-integer numbers to ≤3 decimals. reason.html already did
   this; world.html previously did not (its table cells are almost always CSV strings, so this
   only affects numeric cells in streamed views — cosmetic).
2. sheets.html now pulls in the RTDB SDK module via `firebase-init.js` even though it doesn't
   use it (one shared init > a second init variant; ~40KB extra on that page only).
3. Every page's redundant `subscribeRun`/`ensureToken` copy is gone; behavior is byte-identical.
4. `qBubble()` (defined but unused in both players) kept as-is in the pages.

## firebase.json changes

Old → new:

- `functions` block (python313 codebase, venv ignores): **removed entirely**.
- 5 per-service rewrites (`/infer-runtime{10,11,20,20reason,20w}` → `prereasoner-runtime*`):
  **replaced by one**: `{"source": "/api/**", "run": {"serviceId": "prereasoner-api", "region": "us-central1"}}`.
  Region `us-central1` read from the old firebase.json (all old services were us-central1).
- top-level `"auth": {"providers": {}}` (non-functional stray key): dropped.
- Cache-Control no-cache header glob widened from `**/*.@(html|css|svg)` to
  `**/*.@(html|css|svg|js)` so `lib/*.js` can't go stale behind Hosting's default 1h cache.
- Added an `emulators` block (hosting 5000 / auth 9099 / database 9000 / UI) for local dev.
- `hosting.public`, `cleanUrls: true`, ignore list, `database.rules` → unchanged.
- `database.rules.json` copied byte-for-byte (admin writes bypass rules; a signed-in user may
  read only `/runs/{own uid}`; clients never write).

## Current deployment identifiers (for reference; all public)

- Firebase/GCP project id: **`prereasoner-inference`** (from the old `.firebaserc`, which was
  deliberately not copied) — project number `271377281957`.
- Cloud Run region: **`us-central1`**; consolidated service: **`prereasoner-api`**.
- Auth domain: `chat.prereasoner.com`; RTDB: `https://prereasoner-inference-default-rtdb.firebaseio.com`.
- Picker API key (referrer-restricted) + project number: in `lib/config.js`.

## What self-hosters must change

Everything is in two files, plus one CLI command (details in `web/README.md`):

1. `web/public/lib/config.js` — their Firebase web config (and Picker key/project number if
   they want the Google Sheets import; otherwise sheets.html just won't authenticate the picker).
2. `web/firebase.json` — `serviceId`/`region` if their Cloud Run service differs from
   `prereasoner-api`/`us-central1`.
3. `firebase use <their-project-id>` (no `.firebaserc` is shipped).
4. Optional, local dev: `API_BASE` in `web/public/lib/shared.js` to hit a local engine directly
   (the hosting emulator proxies `run:` rewrites to the DEPLOYED Cloud Run service, never localhost).

## Regression suite

`web/tests/regression.js` (from `public/regression20.js`): endpoint → `/api/reason`, globals
renamed `CASES20`→`CASES`, `CSVS20`→`CSVS`, `runRegression20`→`runRegression`, `__REG20`→`__REG`,
`eq20`/`judge20`→`eq`/`judge`; new header documents that it is auto-generated (generator
`build_regression20.py` lives in the training/tools area) and how to run it (browser console on
/reason while signed in, `await runRegression()`). Moved outside `public/` so it is not deployed.

## Verification performed

- `grep -r "infer-runtime|runtime20|runtime1" web/` → zero hits.
- Internal links between kept pages: `/`, `reason`, `world`, `clarify`, `sheets` only — no page
  references analyze/dimension/result.
- No page re-defines anything now in `lib/` (grepped for every extracted symbol); the Firebase
  config literal appears only in `lib/config.js`.
- `node --check` passes on both classic libs, both ES-module libs, every inline script of every
  page (classic and module extracted and parsed separately), and `tests/regression.js`.

## Post-E2E fixes (2026-07-04, verified in real Chrome against a local engine + live Cloud SQL)

- **Early JSON fallback** (reason.html, world.html): the POST body is parsed once
  (`parseBody`) and raced against the stream — if the body lands and the stream shows no
  life for 3 s, `renderFromJSON` fires immediately instead of waiting for the 90 s safety
  net (which remains, with its cold-start retries, now consuming the same parsed promise).
  Verified: result renders seconds after the engine responds with RTDB disabled.
- **`API_BASE` is now a localStorage override** (`pr_api_base`), not a source edit — local
  testing no longer requires touching lib/shared.js.
- **Auth bypass for local testing** (`sessionStorage pr_test_auth`, firebase-init.js) is
  gated to localhost hostnames and pairs with the engine's `AUTH_TEST_SUB`.
- E2E verified: index demo (France = 270 with full join→world→filter→aggregate trace),
  world page (France cities), clarify flow, info panel, 404, sheets (up to Google Picker).
