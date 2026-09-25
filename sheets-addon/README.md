# Prereasoner

This Apps Script add-on puts Prereasoner in a Google Sheets™ sidebar. The sidebar is the Prereasoner web workbook itself: `Sidebar.html` frames `https://chat.prereasoner.com/embed/sheets`, which runs the same page as chat.prereasoner.com in a compact layout. Live reasoning steps, the workbook, and conversation history all come from the web app (`web/public/lib/workbook.js`, `web/public/lib/host-bridge.js`).

The add-on is only a host. It supplies what the framed page cannot read for itself:

- the signed-in user's Google OAuth access token, which the page exchanges for its Firebase session;
- the spreadsheet's id and name;
- the cell grids of up to eight visible, non-empty tabs, with the active tab first: values, number formats, failed formulas, and merged ranges.

The page imports those grids with the web upload importer (`web/public/lib/xlsx-worker.js`), so a sheet reads exactly as an upload of the same cells would. The same header, date, duration, merge, total, and formula-error rules apply, and the page shows the same message when a tab cannot be read.

The add-on is read-only. It requests `spreadsheets.currentonly`, makes no external requests of its own, stores nothing, and never writes answers or derived values into the spreadsheet. Conversation history remains server-authoritative.

## Local workflow

The project was downloaded with `clasp` and remains linked to the source Apps Script project through `.clasp.json`.

```powershell
node tests/addon.test.js
clasp status
```

`npm run test:web` runs the add-on test, and `npm run test:browser` drives the framed page through a stand-in host (`/__sheets-host` in `web/tests/browser/server.js`).

Review changes before each `clasp push`: it updates the linked cloud Apps Script project. The framed page is served by Firebase Hosting, so a change to `web/public/` reaches the sidebar with a Hosting deploy and needs no new add-on version.

If the sidebar reports that Google blocked access to the sheet, open the sheet in a browser session with only the Google account that owns or has access to it, then authorize the add-on again. This is a Google Apps Script V8 multi-account session issue, not a Prereasoner API error.

## Runtime flow

1. `onOpen` adds **Prereasoner → Ask a question** and **Prereasoner → Previous conversations** (the same sidebar with its conversation list open).
2. `showSidebar` opens `Sidebar.html`, which frames `/embed/sheets`. The framed page asks its host for context with `postMessage`. The host answers only that frame, only at the Prereasoner origin, and the page accepts replies only from its parent at an Apps Script content origin.
3. `getHostContext` returns the Google token, the spreadsheet identity, and its grids. The page signs in, imports the grids, and restores the spreadsheet's conversation (`/api/spreadsheet/conversation/restore`), or waits for a first question.
4. Questions run through the web workbook with its live RTDB trace. The first answer's conversation becomes the spreadsheet's conversation.
5. Before each question the page re-reads the grids (`getWorkbookGrids`). When the sheet changed, it brings the conversation's source up to date (`/api/conversation/sync`, which marks the old answer stale) and reloads with the question pending.

Before reading any cells, the add-on applies the upload's worksheet limits (`web/public/lib/upload-limits.js`): at most eight non-empty tabs, and 10,000 data rows and 256 columns per tab. It also refuses more than 250,000 cells in one read. The page then applies the upload's size limits: at most 2 million characters per converted table and 6 million in total.
