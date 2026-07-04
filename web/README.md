# PreReasoner — web frontend

The static web UI for PreReasoner: attach a spreadsheet (CSV / Excel / Google Sheets), ask a
question in plain language, and watch the model arrive at the answer step by step — every
intermediate table is real, computed data, not generated text.

It is plain HTML/CSS/JS (no build step, no framework) served by **Firebase Hosting**, with:

- **Auth**: Firebase Authentication (Google sign-in, same-tab redirect).
- **Engine**: one Cloud Run service (`prereasoner-api`) reached via the Hosting rewrite
  `/api/**` → Cloud Run. Endpoints the pages use: `POST /api/reason`, `POST /api/world`
  (plus `GET` pings to the same paths to pre-warm the scale-to-zero service, and
  `GET /healthz` on the service itself).
- **Live trace**: Firebase Realtime Database. The engine streams the reasoning trace to
  `/runs/{uid}/{jobId}`; the browser subscribes and renders slides live, decoupled from the
  60s HTTP proxy timeout. A signed-in user can read only their own runs (`database.rules.json`).

## Page map

```
index.html   home: attach data (+ Add data -> Excel/CSV file picker, or sheets.html), type a question
   ├─> sheets.html   Google Sheets import (drive.file scope + Picker) — returns to home with the sheet attached
   └─> reason.html   the main reasoning player: decomposition into a stack of views, streamed live
          │             (POST /api/reason; sign-in happens here as a redirect)
          └─> clarify.html   shown when the model is UNSURE: confirm/edit its proposed reading
                 └─> world.html   the world-knowledge player: SQL forming + world joins (POST /api/world)
404.html     Firebase default not-found page
```

`world.html` is also a standalone player (clarify re-runs through it); `reason.html` delegates
plain/world queries to the same backend paths.

Shared code lives in `public/lib/`:

| file | contents |
|---|---|
| `lib/config.js` | Firebase web config + Picker credentials — the ONE place self-hosters edit (all values are public client identifiers, not secrets) |
| `lib/shared.js` | `esc`, `parseCSV`, `slug`, `sqlTokens`, op labels, sessionStorage keys (`SS.*`), play/pause/spinner UI constants, `API_BASE` |
| `lib/firebase-init.js` | Firebase app/auth/RTDB init, `ensureSignedIn()`, `window.ensureToken`, `window.subscribeRun` |
| `lib/table-render.js` | `tableBubble()` — the table-in-a-bubble renderer used by the players |

`lib/config.js` and `lib/firebase-init.js` are ES modules (imported by the pages'
`<script type="module">` blocks); `lib/shared.js` and `lib/table-render.js` are classic scripts
loaded via `<script src>` **before** the pages' inline scripts that use them.

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
- **To point the pages at a locally running engine**, set `API_BASE` at the top of
  `public/lib/shared.js` to e.g. `'http://localhost:8080'`. The pages then call
  `http://localhost:8080/api/reason` etc. directly — your local engine must serve those paths
  and allow CORS from your dev origin (`http://localhost:5000`).
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
