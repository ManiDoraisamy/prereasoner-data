# Web frontend

`web/` is the static Firebase Hosting client. It lets a user attach tables, ask a question, inspect
the query and source rows, and review the result in a workbook-style view. There is no bundler or
framework; the pages use the browser APIs and classic JavaScript modules already in the repository.

## Runtime shape

- `reason.html` and `knowledge.html` load the same classic script, `public/lib/workbook.js`.
- Firebase Authentication supplies the ID token used by authenticated engine routes.
- The home-page Login button invokes the same Firebase redirect flow used by the workbook; it is not a placeholder.
- Hosting rewrites `/api/**` to the engine and `/chat` to the optional orchestrator.
- Firebase Realtime Database can stream trace nodes under `/runs/{uid}/{jobId}`. When RTDB is unavailable, the
  completed HTTP response renders the same result.
- Conversation snapshots preserve the visible workbook and rail without re-running a query on reload.
- Excel parsing runs in a disposable Web Worker using vendored SheetJS 0.20.3. Compressed input, expanded output,
  worksheet, row, column, and parse-time limits are enforced before data reaches the request API.
- `column_provenance` is authored by the engine from source records and typed computation evidence. The browser
  renders those records verbatim and does not classify a column by its name.

## Workbook sheet types

Home URLs accept `?load=<dataset>&use=sql|py|both`, with one `use` value at a time. `load` selects
the demo files; `use` follows navigation into `/reason` and `/reason/<conversationId>`, example
selection, and the Sheets picker round trip. Request builders include it in both direct engine
and chat JSON bodies, including follow-ups. `customer-orders` contains only `orders.csv`;
`orders-tiers` contains `orders.csv` and `tier.csv`.

The engine owns backend selection and reports the actual execution. Explicit `py`/`both` cannot
accept unsupported SQL fallback results. Changing a saved conversation URL does not rerun its
snapshot; submit another question to execute with the selected mode. Source is available in
shared-plan response/snapshot records, but there is no dedicated Python code viewer in this UI.
See [the execution contract](../docs/DETERMINISTIC_EMITTERS.md).

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

## Page map

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

## Local development

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
Set-Location ..
npm ci
npx playwright install chromium
npm run test:browser
```

`tests/workbook_reference.test.js` evaluates the production classic script in a minimal VM and verifies reference
row compaction, numeric zero preservation, dirty/provenance snapshot state, successful autosave, and surfaced save
errors. The Playwright release journey uses a real XLSX upload and covers sign-in, answer rendering, provenance,
SQL trace, follow-up, and deletion against a deterministic local API fixture. `tests/regression.js` remains the
larger signed-in browser regression against a live `/api/reason` endpoint.

## Deployment

`firebase.json` is the source of truth for Hosting rewrites. Deploying static files and deploying the Cloud Run
engine are separate operations. Do not point production Hosting at an unverified engine revision; validate the tagged
revision first, then update traffic and Hosting deliberately.
