# Web Frontend

`web/` is a static Firebase Hosting application with no bundler or framework. It presents uploaded data, references,
SQL derivations, and results as a workbook with a conversational rail.

## Runtime Shape

- `reason.html` and `knowledge.html` load the same classic script, `public/lib/workbook.js`.
- Firebase Authentication supplies the ID token used by authenticated engine routes.
- Hosting rewrites `/api/**` to the engine and `/chat` to the optional orchestrator.
- Firebase Realtime Database can stream trace nodes under `/runs/{uid}/{jobId}`. When RTDB is unavailable, the
  completed HTTP response renders the same result.
- Conversation snapshots preserve the visible workbook and rail without re-running a query on reload.

## Workbook Sheet Types

| Class | Meaning | Editable |
|---|---|---|
| `input` | User-uploaded source table | Yes; changes require recalculation |
| `master` | User-owned reusable reference table | Yes; dirty changes are saved before a query |
| `ref` | Public world lookup materialized by the engine | No |
| `deriv` | SQL reasoning step or result | No |

The browser request sends uploaded source tables. Saved references are authenticated server data: the browser saves
dirty reference sheets through `/api/master`, then `engine.master.relevant_tables` selects the references connected
to the current upload. A save failure stops the query, preventing an answer from using stale reference values.

Reference actions have distinct meanings:

- **Remove from workbook** hides the sheet in this conversation and keeps it available under `+ Reference`.
- **Delete saved reference** calls `/api/master/delete`, removes the cross-conversation copy, and cannot be undone.

Dirty state and AI-cell provenance survive conversation snapshot reloads. The first reference column is the join key;
the engine requires non-empty, unique keys and unique column names.

## Page Map

| Page | Purpose |
|---|---|
| `index.html` | Attach CSV/Excel/Google Sheets data and begin a question — served at `/` and at the single-source landings `/sheets`, `/excel`, `/csv` (Hosting rewrites; the add button narrows to that source) |
| `reason.html` | Workbook over the general reason endpoint |
| `knowledge.html` | Same workbook over the knowledge endpoint |
| `chatui.html` | Orchestrated conversational entry point |
| `picker.html` | Google Sheets picker/import flow at `/picker` (returns to the landing that opened it) |
| `admin.html` | Allowlisted operational view |

`public/lib/shared.js` owns common storage, escaping, CSV parsing, and navigation helpers. `workbook.js` owns workbook
state, rendering, editing, reference lifecycle, conversation restoration, trace subscription, and request submission.
`public/lib/firebase-init.js` bridges Firebase module APIs into the classic page scripts.

## Local Development

Start the engine first, then serve Hosting:

```powershell
npm install --global firebase-tools
Set-Location web
firebase serve --only hosting --project <firebase-project> --port 5057
```

For localhost-only testing, set:

```js
localStorage.setItem('pr_api_base', 'http://localhost:8080');
sessionStorage.setItem('pr_test_auth', '1');
```

Open `http://localhost:5057`. `pr_test_auth` is a local browser convenience and must be paired with the engine's
development-only `AUTH_TEST_SUB`; neither is valid production authentication.

## Validation

```powershell
Get-ChildItem public/lib/*.js | ForEach-Object { node --check $_.FullName }
node tests/workbook_reference.test.js
```

`tests/workbook_reference.test.js` evaluates the production classic script in a minimal VM and verifies reference
row compaction, numeric zero preservation, dirty/provenance snapshot state, successful autosave, and surfaced save
errors. `tests/regression.js` is the larger signed-in browser regression against a live `/api/reason` endpoint.

## Deployment

`firebase.json` is the source of truth for Hosting rewrites. Deploying static files and deploying the Cloud Run
engine are separate operations. Do not point production Hosting at an unverified engine revision; validate the tagged
revision first, then update traffic and Hosting deliberately.
