# PreReasoner — web frontend

The static web UI for PreReasoner: attach a spreadsheet (CSV / Excel / Google Sheets), ask a
question in plain language, and read the answer as a **workbook** — your tables and every
reasoning step shown as spreadsheet tabs, with a chat rail for follow-ups. Every intermediate
table is real, computed data, not generated text.

It is plain HTML/CSS/JS (no build step, no framework) served by **Firebase Hosting**, with:

- **Auth**: Firebase Authentication (Google sign-in, same-tab redirect).
- **Engine**: one Cloud Run service (`prereasoner-api`) reached via the Hosting rewrite
  `/api/**` → Cloud Run. Endpoints the pages use: `POST /api/reason`, `POST /api/world`, and the
  conversation endpoints `GET /api/conversations` / `GET /api/conversation` (plus `GET` pings to
  pre-warm the scale-to-zero service, and `GET /healthz`).
- **Live trace**: Firebase Realtime Database. The engine streams the reasoning trace to
  `/runs/{uid}/{jobId}`; the workbook subscribes and adds sheets live as they're produced,
  decoupled from the 60s HTTP proxy timeout. A signed-in user can read only their own runs
  (`database.rules.json`).

## The workbook

`reason.html` and `world.html` are the same **workbook**, differing only in which engine endpoint
they call. Its logic is one shared module, `lib/workbook.js`:

- The left side is the spreadsheet — a sheet with a Google-Sheets-style bottom tab bar. Sheets are
  colour-coded: **green** = your uploaded tables (read-only; the AI never writes here), **blue** =
  one per reasoning step (named for what it does, with a per-sheet SQL disclosure), **grey** = the
  world-knowledge lookups a step used.
- The right side is the **chat**: your question, the live status, links to each step, the result,
  and a follow-up box. A follow-up re-runs over the same tables in the same conversation.
- The header shows the conversation's opening question; the ☰ menu opens the **conversations
  drawer** (past conversations from `GET /api/conversations`, re-openable — each re-hydrates its
  stored tables). A conversation's `conversation_id` round-trips in `sessionStorage` and on every
  request, so follow-ups continue it (see [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) §8).

## Page map

```
index.html   home: attach data (+ Add data -> Excel/CSV file picker, or sheets.html), type a question
   ├─> sheets.html   Google Sheets import (drive.file scope + Picker) — returns to home with the sheet attached
   └─> reason.html   the workbook on POST /api/reason (sign-in happens here as a redirect)
          │
          ├─> world.html    the same workbook on POST /api/world (world-knowledge joins)
          └─> clarify.html  shown when the model is UNSURE: confirm/edit its proposed reading,
                            then re-run on whichever page raised it
404.html     Firebase default not-found page
```

Shared code lives in `public/lib/`:

| file | contents |
|---|---|
| `lib/config.js` | Firebase web config + Picker credentials — the ONE place self-hosters edit (all values are public client identifiers, not secrets) |
| `lib/shared.js` | `esc`, `parseCSV`, `slug`, `sqlTokens`, op labels, sessionStorage keys (`SS.*`), UI constants, `API_BASE` |
| `lib/firebase-init.js` | Firebase app/auth/RTDB init, `ensureSignedIn()`, `window.ensureToken`, `window.subscribeRun` |
| `lib/workbook.js` | the workbook itself — sheets, tabs, the chat rail, conversations, and the streaming/fallback run logic |
| `lib/table-render.js` | `tableBubble()` — a standalone table renderer (used by the regression harness) |

`lib/config.js` and `lib/firebase-init.js` are ES modules (imported by the pages'
`<script type="module">` blocks); `lib/shared.js` and `lib/workbook.js` are classic scripts loaded
via `<script src>` **before** the module block calls `run()`.

## Self-hosting checklist

1. Create a Firebase project with **Authentication (Google provider)** and a
   **Realtime Database** instance; create a Web App and copy its SDK config into
   `public/lib/config.js`.
2. Deploy the PreReasoner engine to Cloud Run **in the same Google Cloud project**, service
   name `prereasoner-api` (or edit `serviceId`/`region` in `firebase.json`).
3. For the Google Sheets import (`sheets.html`): create a browser API key restricted to your
   domain with the Picker, Sheets and Drive APIs enabled, and put it plus your numeric project
   number in `public/lib/config.js` (`PICKER_API_KEY` / `PICKER_APP_ID`).
4. Bind the directory to your project — this repo intentionally ships no `.firebaserc`:

   ```sh
   cd web
   firebase use <your-project-id>
   ```

## Local development

```sh
npm i -g firebase-tools
cd web
firebase use <your-project-id>
firebase emulators:start        # hosting :5000, auth :9099, database :9000, emulator UI
```

Notes on what the emulator does and does not give you — honestly:

- **Pages, auth, RTDB** run locally.
- **`/api/**` rewrites are proxied to the real, deployed Cloud Run service** in the project you
  selected with `firebase use`. The hosting emulator cannot proxy a `run:` rewrite to localhost.
- **To point the pages at a locally running engine**, run once in the browser console:
  ```js
  localStorage.setItem('pr_api_base', 'http://localhost:8080')
  ```
  Every page then calls `http://localhost:8080/api/...` directly (the engine sends permissive
  CORS). `localStorage.removeItem('pr_api_base')` returns to the same-origin rewrite.
- **To skip Google sign-in entirely** (works on `localhost` only), run the engine with
  `AUTH_TEST_SUB=localdev` and set in the browser console:
  ```js
  sessionStorage.setItem('pr_test_auth', '1')
  ```
  The pages then send a dummy bearer token that the engine accepts without verification.
  Both toggles together give a fully local, no-Google, no-deploy test loop — see
  [docs/TESTING.md](../docs/TESTING.md) for the end-to-end recipe.
- Sign-in against the **production** Google identity works on `localhost` only if `localhost`
  is in the Firebase Auth authorized domains (it is by default for new projects).

## Regression test

`tests/regression.js` is an auto-generated, browser-console regression suite (the generator
lives in the training/tools area of the model repo). To run: open the site, go to `/reason` so
you are signed in, paste the whole file into the devtools console, then `await runRegression()`.
It asserts final answers (row counts / first cell) against `/api/reason`. It is outside
`public/` on purpose — it is not deployed.

## Deploy

```sh
cd web
firebase use <your-project-id>
firebase deploy                       # hosting + database rules
# or: firebase deploy --only hosting
```
