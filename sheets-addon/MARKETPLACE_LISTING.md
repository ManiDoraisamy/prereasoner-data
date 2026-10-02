# Google Workspace Marketplace listing — Sheets

## Store listing

- **Application name:** Prereasoner
- **Sheets Extensions menu:** Prereasoner
- **Category:** Office Applications
- **Pricing:** Free of charge trial
- **Short description:** Ask questions about your Google Sheets™ in plain language. See how the answer is calculated, step by step.
- **Developer:** MailRecipe LLC
- **Website:** https://chat.prereasoner.com/
- **Privacy:** https://chat.prereasoner.com/privacy
- **Terms:** https://chat.prereasoner.com/terms
- **Support:** https://chat.prereasoner.com/support
- **Marketplace promo video:** omitted; the listing uses three workflow illustrations.

### Detailed description

You can use formulas, lookups and pivot tables to calculate results in Google Sheets™. Or, you can upload it in ChatGPT, ask a question in plain English and get the answer without knowing how it was calculated. What if you could ask a question in plain English and see how it was calculated step by step?

Prereasoner lets you ask a question in plain language, review the answer, and inspect the source rows and reasoning behind it. Your source spreadsheet stays unchanged, while each calculation can be inspected step by step as sheets.

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

❇️ Pricing

Start free with 50 questions per month. Paid plans start at $18 per month when billed annually. For
details, visit:
https://prereasoner.com/sheet-copilot/pricing.html

Privacy: https://chat.prereasoner.com/privacy

Terms: https://chat.prereasoner.com/terms

Support: https://chat.prereasoner.com/support

Google Sheets™ is a trademark of Google LLC.

### Installation and use

1. Install Prereasoner and authorize the requested permissions.
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
| `script.external_request` | Lets server-side Apps Script use `UrlFetchApp` to authenticate through Firebase, send the user's question and bounded current-workbook data to the manifest-allowlisted Prereasoner service, save sidebar state, and retrieve previous conversations. Apps Script offers no narrower per-domain OAuth scope; `urlFetchWhitelist` restricts the destinations. |
| `script.container.ui` | Adds the Prereasoner Extensions menu, question-and-answer sidebar, and previous-conversations dialog inside Google Sheets. To show the analysis steps as they run, the sidebar also exchanges the user's Google access token with Firebase Authentication in the browser; the token stays in the sidebar's memory and is never sent to Prereasoner servers. No narrower Apps Script scope provides container UI. |
| `spreadsheets.currentonly` | Reads only the spreadsheet in which the user invokes the add-on: its visible, non-empty tabs, when the sidebar opens and before each question. The add-on does not write to it. |

### Reviewer test path

Use a spreadsheet with columns such as `city` and `amount`, then ask `What is the total amount in
France?`. Expand **Reasoning steps** to inspect the calculation. Ask a follow-up in the same sidebar.
Choose **Previous conversations** and open the saved conversation link. A reviewer account is not
required because the add-on uses the reviewer's Google identity through Firebase Authentication.

## Assets

- The Marketplace logos are rendered from the Prereasoner artwork (`docs/marketplace/logo-source.png`).
- `docs/marketplace/icon-32.png`, `icon-48.png`, `icon-96.png`, `icon-128.png`
- `docs/marketplace/card-banner.svg`, `card-banner-220x140.png`
- `docs/marketplace/review-1-ask.svg`, `review-1-ask-1280x800.png`
- `docs/marketplace/review-2-answer.svg`, `review-2-answer-1280x800.png`
- `docs/marketplace/review-3-previous.svg`, `review-3-previous-1280x800.png`
