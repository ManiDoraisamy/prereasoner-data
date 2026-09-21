# Prereasoner - Sheets Copilot — Marketplace release

## Store listing

- **Application name:** Prereasoner - Sheets Copilot
- **Sheets Extensions menu:** Prereasoner - Sheets Copilot
- **Category:** Office Applications
- **Pricing:** Free of charge trial
- **Short description:** Ask questions about your Google Sheets™ data. Check every answer with visible reasoning and source rows.
- **Developer:** MailRecipe LLC
- **Website:** https://chat.prereasoner.com/
- **Privacy:** https://chat.prereasoner.com/privacy
- **Terms:** https://chat.prereasoner.com/terms
- **Support:** https://chat.prereasoner.com/support
- **Marketplace promo video (unlisted):** https://youtu.be/uND51puaL8A

### Detailed description

Spreadsheet answers often require formulas, lookups, pivot tables and queries—and still leave
reviewers asking how the number was calculated. Prereasoner - Sheets Copilot lets you ask a question in plain
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

Prereasoner - Sheets Copilot reads visible, non-empty tabs in the current spreadsheet and can connect related records
across sheets.

➤ Look up missing context

Your sheet has cities, but your question asks by country or currency. Prereasoner - Sheets Copilot can add the exact
geographic or currency context needed to answer.

➤ Continue with follow-up questions

Ask another question without rebuilding the spreadsheet context. The conversation stays connected
to the original analysis.

➤ Reopen previous conversations

Choose Extensions → Prereasoner - Sheets Copilot → Previous conversations to open saved analyses directly in
Prereasoner - Sheets Copilot.

➤ Keep source data unchanged

The add-on reads the current spreadsheet only when you ask a question. It never edits spreadsheet
cells.

❇️ Why Prereasoner - Sheets Copilot?

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

Prereasoner - Sheets Copilot reads visible, non-empty tabs from the spreadsheet where the add-on is running. It sends
the question and bounded workbook data to Prereasoner - Sheets Copilot only after you submit a question. It does not
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

1. Install Prereasoner - Sheets Copilot and authorize the requested permissions.
2. Open a spreadsheet containing a header row and at least one data row.
3. Choose **Extensions → Prereasoner - Sheets Copilot → Ask a question**.
4. Ask a question and review the answer and reasoning steps.
5. Choose **Extensions → Prereasoner - Sheets Copilot → Previous conversations** to reopen saved work.

### OAuth scope justification

| Scope | Reason |
| --- | --- |
| `openid` | Identifies the signed-in Google user for Prereasoner authentication. |
| `userinfo.email` | Associates the user with their Prereasoner account and supports account communication. |
| `userinfo.profile` | Completes Google identity federation through Firebase Authentication. |
| `script.external_request` | Lets server-side Apps Script use `UrlFetchApp` to authenticate through Firebase, send the user's question and bounded current-workbook data to the manifest-allowlisted Prereasoner service, save sidebar state, and retrieve previous conversations. Apps Script offers no narrower per-domain OAuth scope; `urlFetchWhitelist` restricts the destinations. |
| `script.container.ui` | Adds the Prereasoner Extensions menu, question-and-answer sidebar, and previous-conversations dialog inside Google Sheets. No narrower Apps Script scope provides those container UI capabilities. |
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
- **Apps Script version:** `20`
- **Deployment ID:** `AKfycbxsTRCTIQ41Th_7T-CuHQ9bB5KzkU1SUeRInLH_EI7CksH7g8TzZd6pLeHih2nO7lxX`
- **YouTube channel:** `Prereasoner` (`UCcY6pYi3Pu-xbt5iH-CE-0Q`)
- **Previous reviewer video:** `uND51puaL8A` (Unlisted; replaced)
- **Replacement reviewer video:** https://youtu.be/hXyQ9CYCfBM

## Verification follow-up — September 20, 2026

- The privacy policy now documents concrete data-protection mechanisms and affirmatively states
  compliance with the Google User Data and Developer Policy, including Limited Use requirements.
- The policy states that raw or derived Google user data is not used or transferred to train or
  improve generalized or non-personalized AI/ML models.
- The add-on shows a concise data-use and no-generalized-training disclosure before the first
  question is submitted.
- The updated add-on was pushed with `clasp` and released as immutable Apps Script version `20`.
- Apps Script version `20` standardizes the canonical product name as
  **Prereasoner - Sheets Copilot** throughout the add-on UI and review materials.
- The Apps Script project title, OAuth consent branding, Marketplace application name,
  post-install menu path, card banner, and first listing screenshot now use the same canonical name.
- Marketplace App Configuration is saved against Apps Script version `20`. The Store Listing changes
  are saved as a draft; they have not been submitted for Marketplace review.
- OAuth branding was saved with the canonical name and attached to the verification request that is
  currently under review. Google may continue showing the unverified-app warning until approval.
- Google requested a replacement video that shows the complete expanded consent screen and the full
  user-facing operation of `script.external_request` and `script.container.ui`. Recording instructions
  are in `OAUTH_DEMO_SCRIPT_2026-09-20.md`.
- Do not resubmit OAuth verification until the replacement unlisted video URL has been added to the
  verification request and `OAUTH_REVIEW_RESPONSE_2026-09-20.md`.

## Previous submission status — September 15, 2026

- Marketplace App Configuration was previously saved with Apps Script version `18`, the developer contact,
  and the exact six OAuth scopes declared in `appsscript.json`.
- The Marketplace Store Listing draft is saved with the compliant English copy, five workflow
  screenshots at 1280 × 800, the unlisted reviewer video, icons, card banner, and post-install tip.
- OAuth branding is verified. OAuth data-access verification has been submitted and is under review.
- Do not submit the Marketplace review until OAuth data-access verification is approved. The saved
  Marketplace draft is otherwise ready for submission.
- Logo source: `C:\work\FormFacade\public\logo-full.png`. SHA-256:
  `C8663738280FBF368E3D1768F97FC70C8F1B1F2C8E04DF2684923466DEFFCD41`.
