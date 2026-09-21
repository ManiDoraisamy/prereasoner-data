# Replacement OAuth demo video script

Record one continuous, unlisted video. Keep browser zoom and recording resolution high enough that
every scope and menu label is readable. Do not edit out the consent flow.

## Before recording

- Use one Google account in the browser to avoid the Apps Script multi-account authorization issue.
- Use Apps Script version `20` after the release steps in the Marketplace document are
  complete.
- Prepare a spreadsheet with a header row and several data rows, including an `amount` column.
- Revoke the app's existing Google account authorization so the complete OAuth consent flow appears.
- Open these tabs in advance: the spreadsheet, Apps Script `appsscript.json`, Google Cloud OAuth Data
  Access, and https://chat.prereasoner.com/privacy.

## Recording sequence

1. **Identify the app and source spreadsheet.** Show the spreadsheet title, headers, and values.
   State that the add-on reads only the current spreadsheet through `spreadsheets.currentonly` and
   does not write to it.
2. **Open the add-on.** Choose **Extensions → Prereasoner - Sheets Copilot → Ask a question**. This demonstrates
   `script.container.ui`: the Prereasoner menu and sidebar are rendered inside Google Sheets.
3. **Show the complete consent screen.** Continue through OAuth, click **Show all services**, and
   pause while every requested permission is expanded and readable. The recording must show all six
   scopes represented by the consent screen.
4. **Show the first-use disclosure.** After authorization, pause on the sidebar notice explaining
   that the question and visible, non-empty tabs are processed securely and are not used to train
   generalized AI models.
5. **Exercise `script.external_request`.** Ask `What is the total amount?`. Explain that the add-on's
   server-side `UrlFetchApp` call sends the bounded snapshot and question to the allowlisted
   Prereasoner endpoint and retrieves the answer. Show the answer and expand **Reasoning steps**.
6. **Show follow-up functionality.** Ask a follow-up question and show the returned answer in the
   same sidebar.
7. **Show previous conversations.** Choose **Extensions → Prereasoner - Sheets Copilot → Previous conversations**,
   show the server-retrieved list, and open the saved conversation on chat.prereasoner.com. This
   demonstrates both the container dialog and the authenticated external request.
8. **Show source-account impact.** Return to the spreadsheet and show that the original cells are
   unchanged. State that no write or delete Google scope is requested, so the reviewer's
   write/delete source-account requirement is not applicable.
9. **Prove scope matching.** Show the Apps Script manifest and then Google Cloud **Data Access**.
   Slowly show the same six scopes in both places. Also show the three allowed external-request
   destinations in `urlFetchWhitelist`.
10. **Show privacy remediation.** Open https://chat.prereasoner.com/privacy and show the **Google API
    Limited Use and AI/ML** and **Data protection** sections.

## Scope narration

- `openid`, `userinfo.email`, and `userinfo.profile`: authenticate the Google user and associate
  saved conversations with that verified identity.
- `spreadsheets.currentonly`: read the visible, non-empty tabs only from the spreadsheet where the
  add-on is running; no broader file access is requested.
- `script.container.ui`: create the Extensions menu, sidebar, and Previous conversations dialog.
- `script.external_request`: call the allowlisted Prereasoner/Firebase services from server-side Apps
  Script. `UrlFetchApp` has no narrower per-domain OAuth scope; the manifest's
  `urlFetchWhitelist` supplies the destination restriction.
