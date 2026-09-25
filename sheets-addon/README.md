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

`npm run test:web` runs the server test (`tests/addon.test.js`). `npm run test:browser` drives the real `Sidebar.html` on an Apps Script-like origin, with the shared files served from `web/public` (`web/tests/browser/sheets-sidebar.spec.js`). `docs/marketplace/render-review-assets.js` renders the Marketplace images the same way.

Review changes before each `clasp push`: it updates the linked cloud Apps Script project. Marketplace installs run the script version set in the Marketplace SDK App Configuration, and a change there takes effect when the listing is published.

If the sidebar reports that Google blocked access to the sheet, open the sheet in a browser session with only the Google account that owns or has access to it, then authorize the add-on again. This is a Google Apps Script V8 multi-account session issue, not a Prereasoner API error.

## Runtime flow

1. `onOpen` adds **Prereasoner → Ask a question** and **Prereasoner → Previous conversations** (a dialog that lists saved conversations with links to `/reason/<conversation_id>`).
2. `getSidebarContext` returns the Google token and the grids. The sidebar signs in to Firebase with the token for the live trace, imports the grids, and restores this sheet's conversation (`/api/spreadsheet/conversation/restore`). If a tab cannot be read, the sidebar shows the importer's message, such as `Column H has values but no header in row 1`.
3. Before each question, the sidebar reads the grids again. If the sheet changed since the conversation last saw it, `/api/conversation/sync` brings the conversation's source up to date, which marks earlier answers stale.
4. `askPrereasoner` sends the question, tables, history, and a turn id to `/chat`. Meanwhile the sidebar follows `runs/<uid>/<turn id>` and each engine call's trace, showing the steps and the reply as they arrive.
5. The finished turn shows the answer and its reasoning steps, linked to the full analysis in Prereasoner, and is saved for this sheet (`/api/spreadsheet/conversation/state`).

Before reading any cells, the add-on applies the upload's worksheet limits (`web/public/lib/upload-limits.js`): at most eight non-empty tabs, and 10,000 data rows and 256 columns per tab. It also refuses more than 250,000 cells in one read. The importer then applies the upload's size limits: at most 2 million characters per converted table and 6 million in total.
