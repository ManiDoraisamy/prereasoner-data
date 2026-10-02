# Prereasoner

This Apps Script add-on puts Prereasoner in a Google Sheets™ sidebar. The sidebar is the add-on's own UI, styled with Google's add-on CSS, and it draws each answer with the web app's rail component, so the reasoning steps read exactly as they do on chat.prereasoner.com:

- `web/public/lib/turn-renderer.js`: turns, reasoning steps (the step sentences, lineage, and Python/SQL badges), and answers.
- `web/public/lib/firebase-init.js`: the live trace. Each step appears as it finishes, and the reply streams as it is written.
- `web/public/lib/workbook-import.js`: the upload importer. The sidebar reads a sheet exactly as an upload of the same cells, with the same header, date, duration, merge, total, and formula-error rules and the same messages.

The shared suggestion controller also loads from Hosting. `Sidebar.html` and `Previous.html` are stable loaders: the actual markup, platform JavaScript and CSS live under `web/public/office/sheets/`. A Hosting deploy updates the whole UI without a new Apps Script version. Changes to the mandatory `Code.js` bridge, loader protocol, permissions or manifest still need a platform release.

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
2. From its first moment the sidebar says what it is doing: reading the sheet ("Reading 6 tabs…", with the time taken and, after 20 seconds, a note that large spreadsheets take a few minutes), preparing the sheets, restoring the conversation. The three starters come with the headers (the headers-only `getWorkbookSchema`), before the cells are read, and a question asked while the sheet is read waits for that read. `getSidebarContext` returns the Google token and the grids. The sidebar signs in to Firebase with the token for the live trace, imports the grids, and restores this sheet's conversation (`/api/spreadsheet/conversation/restore`). Blank/repeated headers retain their cells with stable column-letter names and warnings. Merged cells and formula errors preserve stored values. The add-on automatically uses all visible tabs when they fit, or the current sheet when workbook limits require it; a short note explains an automatic single-sheet choice without listing tab names. Once a chat exists, the header shows New chat on the left and, on the right, "Syncing…" or how old the sheet's last read is ("Synced 5 mins ago"); a click reads the sheet again. Three sheet-aware prompt suggestions appear as clickable cards; selecting one fills the composer and never submits it. The composer stays editable during loading and answering. Ambiguity in a repeated field affects only questions that need to select that field.
3. Before each question, the sidebar reads the grids again when its last read took under 15 seconds, so an edit is in the answer. A sheet that reads more slowly keeps its last read, whose age the header shows (a click reads it again). Before the first question, and whenever the sheet changed since the conversation last saw it, `/api/conversation/sync` uploads it (the first upload starts the conversation; a later one marks earlier answers stale) and returns its `source_hash`. An unchanged sheet is not sent again, and the restore at the sidebar's start sends no cells.
4. `askPrereasoner` sends the question, the conversation id, the sheet's `source_hash`, the history and a turn id to `/chat`; the cells are not sent with it. If the stored sheet was replaced since (another sidebar synced it), the chat answers 409, and the sidebar uploads the sheet again and asks once more. Meanwhile the sidebar follows `runs/<uid>/<turn id>` and each engine call's trace, showing the steps and the reply as they arrive.

The sidebar is hosted (`web/public/office/sheets/sidebar.js`) and deploys with the web app, while Marketplace installs run the published script version. The sidebar uses steps 3 and 4 when the script's `getSidebarContext` returns `uploadOnce: true`. With a script published before that, it restores and asks with the cells, as that script expects.
5. The finished turn shows the answer and its reasoning steps, linked to the full analysis in Prereasoner, and is saved for this sheet (`/api/spreadsheet/conversation/state`).

Before reading any cells, the add-on applies the upload's worksheet limits (`web/public/lib/upload-limits.js`): at most eight visible, non-empty tabs, 50,000 data rows and 256 columns per tab, and 1,000,000 cells in one read. If all tabs exceed that capacity, the initial read automatically selects the active sheet and says why without requiring a scope choice or listing unrelated tab names; users do not have to reformat unrelated tabs to ask a question. Unrelated oversized tabs do not block a usable active sheet. A 30,000-row tab fits when its workload is within these bounds. The importer then applies at most 8 million characters per converted table and 20 million in total.
