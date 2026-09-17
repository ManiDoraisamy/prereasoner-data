# Prereasoner Sheets Copilot — Marketplace release

## Store listing

- **Application name:** Prereasoner Sheets Copilot
- **Sheets Extensions menu:** Prereasoner
- **Category:** Office Applications
- **Pricing:** Free of charge trial
- **Short description:** Ask questions about your Google Sheets™ data. Check every answer with visible reasoning and source rows.
- **Developer:** MailRecipe LLC
- **Website:** https://chat.prereasoner.com/
- **Privacy:** https://chat.prereasoner.com/privacy
- **Terms:** https://chat.prereasoner.com/terms
- **Support:** https://chat.prereasoner.com/support
- **Reviewer demo (unlisted):** https://youtu.be/uND51puaL8A

### Detailed description

Spreadsheet answers often require formulas, lookups, pivot tables and queries—and still leave
reviewers asking how the number was calculated. Prereasoner lets you ask a question in plain
language, review the answer, and inspect the source rows and reasoning behind it. Your source
spreadsheet stays unchanged.

❇️ Features

➤ Ask questions in plain language

Ask “What is the total amount in France in US dollars?” or “Which products had the highest sales?”
No formulas or query language to learn.

➤ Inspect every answer

See the answer, supporting rows and step-by-step reasoning together, so you can check how each result
was produced.

➤ Analyze multiple tabs

Prereasoner reads visible, non-empty tabs in the current spreadsheet and can connect related records
across sheets.

➤ Look up missing context

Your sheet has cities, but your question asks by country or currency. Prereasoner can add the exact
geographic or currency context needed to answer.

➤ Continue with follow-up questions

Ask another question without rebuilding the spreadsheet context. The conversation stays connected
to the original analysis.

➤ Reopen previous conversations

Choose Extensions → Prereasoner → Previous conversations to open saved analyses directly in
Prereasoner.

➤ Keep source data unchanged

The add-on reads the current spreadsheet only when you ask a question. It never edits spreadsheet
cells.

❇️ Why Prereasoner?

➤ Answers you can audit

Don't accept a number from a black box. Review the reasoning and source rows behind every answer.

➤ Native to Google Sheets™

Start from the Extensions menu and analyze the spreadsheet already open in your browser.

➤ Built for real spreadsheet work

Filter rows, join tabs, group records, calculate totals and ratios, rank results, and enrich data with
exact lookups.

❇️ Use cases

Sales analysis: Calculate revenue by country, product, customer or time period and inspect the rows
behind each total.

Operations reporting: Summarize orders, inventory, delivery performance or service data without
building formulas manually.

Finance checks: Calculate totals, ratios and currency conversions while keeping the calculation steps
visible.

Data enrichment: Connect cities to countries, products to categories, or records across related
worksheet tabs using exact identifiers.

Management questions: Ask follow-ups, compare segments and reopen previous conversations when you
need to explain or revisit a result.

❇️ Privacy and data use

Prereasoner reads visible, non-empty tabs from the spreadsheet where the add-on is running. It sends
the question and bounded workbook data to Prereasoner only after you submit a question. It does not
request file-storage access and does not edit spreadsheet cells. Conversations are associated with
your signed-in account so only you can retrieve them.

❇️ Pricing

Start free with 50 questions per month. Paid plans start at $18 per month when billed annually. For
details, visit:
https://prereasoner.com/sheet-copilot/pricing.html

Privacy: https://chat.prereasoner.com/privacy

Terms: https://chat.prereasoner.com/terms

Support: https://chat.prereasoner.com/support

Google Sheets™ is a trademark of Google LLC.

### Installation and use

1. Install Prereasoner Sheets Copilot and authorize the requested permissions.
2. Open a spreadsheet containing a header row and at least one data row.
3. Choose **Extensions → Prereasoner → Ask a question**.
4. Ask a question and review the answer and reasoning steps.
5. Choose **Extensions → Prereasoner → Previous conversations** to reopen saved work.

### OAuth scope justification

| Scope | Reason |
| --- | --- |
| `openid` | Identifies the signed-in Google user for Prereasoner authentication. |
| `userinfo.email` | Associates the user with their Prereasoner account and supports account communication. |
| `userinfo.profile` | Completes Google identity federation through Firebase Authentication. |
| `script.external_request` | Sends the user's question and bounded current-workbook data to `chat.prereasoner.com` and exchanges the Google token with Firebase Authentication. |
| `script.container.ui` | Adds the Prereasoner menu, sidebar, and previous-conversations dialog to Google Sheets. |
| `spreadsheets.currentonly` | Reads only the spreadsheet in which the user invokes the add-on. The add-on does not write to it. |

### Reviewer test path

Use a spreadsheet with columns such as `city` and `amount`, then ask `What is the total amount in
France?`. Expand **Reasoning steps** to inspect the calculation. Ask a follow-up in the same sidebar.
Choose **Previous conversations** and open the saved conversation link. A reviewer account is not
required because the add-on uses the reviewer's Google identity through Firebase Authentication.

## Assets

- The Marketplace icons use the Prereasoner purple-mask artwork from `C:\work\FormFacade\public`.
- `marketplace/icon-32.png`
- `marketplace/icon-48.png`
- `marketplace/icon-96.png`
- `marketplace/icon-128.png`
- `marketplace/card-banner-220x140.png`
- `marketplace/screenshot-1-open-prereasoner-1280x800.png`
- `marketplace/screenshot-2-ask-question-1280x800.png`
- `marketplace/screenshot-3-answer-reasoning-1280x800.png`
- `marketplace/screenshot-4-follow-up-1280x800.png`
- `marketplace/screenshot-5-previous-conversations-1280x800.png`
- `marketplace/prereasoner-sheets-copilot-oauth-demo.webm`

## Release identifiers

- **Google Cloud project:** `prereasoner-inference` (`271377281957`)
- **Apps Script project:** `17TO27c1vtTAfHo66XhnbKmKFwO9t-Koihd7ZtCFC_lJnIjGQLhDWp2jL`
- **Apps Script version:** `18`
- **Deployment ID:** `AKfycbxsTRCTIQ41Th_7T-CuHQ9bB5KzkU1SUeRInLH_EI7CksH7g8TzZd6pLeHih2nO7lxX`
- **YouTube channel:** `Prereasoner` (`UCcY6pYi3Pu-xbt5iH-CE-0Q`)
- **Reviewer video:** `uND51puaL8A` (Unlisted)

## Submission status — September 15, 2026

- Marketplace App Configuration is saved with Apps Script version `18`, the developer contact,
  and the exact six OAuth scopes declared in `appsscript.json`.
- The Marketplace Store Listing draft is saved with the compliant English copy, five workflow
  screenshots at 1280 × 800, the unlisted reviewer video, icons, card banner, and post-install tip.
- OAuth branding is verified. OAuth data-access verification has been submitted and is under review.
- Do not submit the Marketplace review until OAuth data-access verification is approved. The saved
  Marketplace draft is otherwise ready for submission.
- Logo source: `C:\work\FormFacade\public\logo-full.png`. SHA-256:
  `C8663738280FBF368E3D1768F97FC70C8F1B1F2C8E04DF2684923466DEFFCD41`.
