# Prereasoner

This Apps Script add-on puts Prereasoner in a Google Sheets™ sidebar. The sidebar is the add-on's own UI, styled with Google's add-on CSS, and it draws each answer with the web app's rail component, so the reasoning steps read exactly as they do on chat.prereasoner.com:

- `web/public/lib/turn-renderer.js`: turns, reasoning steps (the step sentences, lineage, and Python/SQL badges), and answers.
- `web/public/lib/firebase-init.js`: the live trace. Each step appears as it finishes, and the reply streams as it is written.
- `web/public/lib/workbook-import.js`: the upload importer. The sidebar reads a sheet exactly as an upload of the same cells, with the same header, date, duration, merge, total, and formula-error rules and the same messages.

All three load from chat.prereasoner.com, so a Hosting deploy updates them; the add-on needs a new version only when `Code.js`, `Sidebar.html`, `Previous.html`, or the manifest change.

`Code.js` supplies what the sidebar cannot do from the Apps Script sandbox: the cells of up to eight visible, non-empty tabs (the active tab first), and authenticated calls to Prereasoner. Prereasoner accepts browser requests only from its own origins, so these calls go server to server with `UrlFetchApp`, using a Firebase identity for the user's Google account. `/chat` goes directly to Cloud Run for its 300-second timeout.

The add-on is read-only. It requests `spreadsheets.currentonly`, stores nothing, and never writes answers or derived values into the spreadsheet. Conversation history remains server-authoritative.

## Local workflow

The project was downloaded with `clasp` and remains linked to the source Apps Script project through `.clasp.json`.

```powershell
node tests/addon.test.js
clasp status
```

`npm run test:web` runs the server test (`tests/addon.test.js`). `npm run test:browser` drives the real `Sidebar.html` on an Apps Script-like origin, with the shared files served from `web/public` (`web/tests/browser/sheets-sidebar.spec.js`). `docs/marketplace/render-review-assets.js` renders the Marketplace images the same way. Listing copy is in `MARKETPLACE_LISTING.md`.

Review changes before each `clasp push`: it updates the linked cloud Apps Script project. Marketplace installs run the script version set in the Marketplace SDK App Configuration, and a change there takes effect when the listing is published.

In a browser signed in to several Google accounts, the menu runs as the account that opened the sheet, but Apps Script sends the sidebar's `google.script.run` calls as the browser's default account. Google then refuses them before any add-on code runs, with "Authorization is required to perform that action.", `PERMISSION_DENIED`, "You do not have permission to access the requested document.", or "No item with the given ID could be found". The sidebar shows one message for all of them: open the sheet in a window signed in only to the account that installed Prereasoner (an Incognito window works). This is a Google Apps Script multi-account limitation, not a Prereasoner API error.

## Runtime flow

1. `onOpen` adds **Prereasoner → Ask a question** and **Prereasoner → Previous conversations** (a dialog that lists saved conversations with links to `/reason/<conversation_id>`).
2. `getSidebarContext` returns the Google token and the grids. The sidebar signs in to Firebase with the token for the live trace, imports the grids, and restores this sheet's conversation (`/api/spreadsheet/conversation/restore`). Blank/repeated headers retain their cells with stable column-letter names and warnings. Merged cells and formula errors preserve stored values. If all visible tabs exceed the request capacity, the sidebar selects and labels the active-sheet scope; the user can choose all tabs in the scope selector. The composer stays editable during loading and answering. Ambiguity in a repeated field affects only questions that need to select that field.
3. Before each question, the sidebar reads the grids again. If the sheet changed since the conversation last saw it, `/api/conversation/sync` brings the conversation's source up to date, which marks earlier answers stale.
4. `askPrereasoner` sends the question, tables, history, and a turn id to `/chat`. Meanwhile the sidebar follows `runs/<uid>/<turn id>` and each engine call's trace, showing the steps and the reply as they arrive.
5. The finished turn shows the answer and its reasoning steps, linked to the full analysis in Prereasoner, and is saved for this sheet (`/api/spreadsheet/conversation/state`).

Before reading any cells, the add-on applies the upload's worksheet limits (`web/public/lib/upload-limits.js`): at most eight visible, non-empty tabs, 50,000 data rows and 256 columns per tab, and 1,000,000 cells in one read. An oversized visible tab prevents analysis; the error names the tab and asks the user to hide unwanted tabs or reduce it. This makes partial-workbook scope an explicit user choice. A 30,000-row tab fits when the total workload is within these bounds. The importer then applies at most 8 million characters per converted table and 20 million in total.
