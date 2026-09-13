# Prereasoner Sheets Copilot

This Apps Script sidebar brings the Prereasoner answer rail into Google Sheets. It reads up to eight visible, non-empty tabs from the current spreadsheet, sends a bounded CSV snapshot to the existing Prereasoner chat endpoint when the user asks a question, displays the returned reasoning steps and result, and keeps the conversation ID for follow-up questions.

The presentation is a host-native editor sidebar: it uses Google’s add-on CSS package, keeps branding brief, and places the answer rail and follow-up composer in the fixed sidebar rather than building a separate home page.

The add-on is read-only. It requests `spreadsheets.currentonly`, does not add editors, and never writes answers or derived values into the workbook. Conversation history remains server-authoritative: the add-on never stores a local conversation index.

## Local workflow

The project was downloaded with `clasp` and remains linked to the source Apps Script project through `.clasp.json`.

```powershell
node tests/addon.test.js
clasp status
```

Review changes before each `clasp push`: it updates the linked cloud Apps Script project.

If a sidebar call reports that Google could not read from storage, open the sheet in a
browser session with only the Google account that owns or has access to the sheet,
then authorize the add-on again. This is a Google Apps Script V8 multi-account
session issue, not a Prereasoner API error.

## Runtime flow

1. `onOpen` adds **Prereasoner → Ask a question** and **Prereasoner → Previous conversations**.
2. `showSidebar` reads a bounded workbook summary while the menu action has the active Sheets context and injects that summary into the sidebar; no workbook data is sent externally at startup.
3. `askPrereasoner` serializes the non-empty tabs and exchanges the Apps Script Google OAuth token for a short-lived Firebase ID token.
4. The server calls `https://chat.prereasoner.com/chat` with the workbook tables, question, bounded history, and conversation ID.
5. The sidebar renders the answer, compact result table, reasoning steps, and follow-up composer.
6. `Previous conversations` uses the same Firebase identity to read `/api/conversations` and renders direct links to `/reason/<conversation_id>`; the database remains the source of truth.

Prereasoner applies the same server limits as the web workbook: eight tables, 10,000 data rows, 2 MB per table, and 6 MB combined.
