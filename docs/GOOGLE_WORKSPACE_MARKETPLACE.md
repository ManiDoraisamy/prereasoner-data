# Prereasoner — Marketplace release

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
| `script.container.ui` | Adds the Prereasoner Extensions menu, question-and-answer sidebar, and previous-conversations dialog inside Google Sheets. No narrower Apps Script scope provides those container UI capabilities. |
| `spreadsheets.currentonly` | Reads only the spreadsheet in which the user invokes the add-on. The add-on does not write to it. |

### Reviewer test path

Use a spreadsheet with columns such as `city` and `amount`, then ask `What is the total amount in
France?`. Expand **Reasoning steps** to inspect the calculation. Ask a follow-up in the same sidebar.
Choose **Previous conversations** and open the saved conversation link. A reviewer account is not
required because the add-on uses the reviewer's Google identity through Firebase Authentication.

## Assets

- The uploaded Marketplace logos use the Prereasoner artwork from
  `C:\work\FormFacade\public\logo-full.png` (SHA-256 recorded below).
- `docs/marketplace/icon-32.png`, `icon-48.png`, `icon-96.png`, `icon-128.png`
- `docs/marketplace/card-banner.svg`, `card-banner-220x140.png`
- `docs/marketplace/review-1-ask.svg`, `review-1-ask-1280x800.png`
- `docs/marketplace/review-2-answer.svg`, `review-2-answer-1280x800.png`
- `docs/marketplace/review-3-previous.svg`, `review-3-previous-1280x800.png`

## Release identifiers

- **Google Cloud project:** `prereasoner-inference` (`271377281957`)
- **Apps Script project:** `17TO27c1vtTAfHo66XhnbKmKFwO9t-Koihd7ZtCFC_lJnIjGQLhDWp2jL`
- **Apps Script version:** `21`
- **Deployment ID:** `AKfycbxsTRCTIQ41Th_7T-CuHQ9bB5KzkU1SUeRInLH_EI7CksH7g8TzZd6pLeHih2nO7lxX`
- **YouTube channel:** `Prereasoner` (`UCcY6pYi3Pu-xbt5iH-CE-0Q`)
- **OAuth reviewer video:** https://youtu.be/hXyQ9CYCfBM (provided for OAuth verification)
- **Marketplace promo video:** omitted from the Store Listing draft; three workflow images remain.

## Branding and screenshot resubmission — September 24, 2026

- Marketplace application name and Apps Script menu name are **Prereasoner**. References to
  Google Sheets in the listing use **Google Sheets™**, with the Google LLC trademark attribution.
- Apps Script project title, OAuth consent-screen app name, Apps Script `ADDON_NAME`, and hosted
  Prereasoner page titles and product labels use the canonical name **Prereasoner**.
- Apps Script version `21` was pushed with `clasp`; the existing Marketplace deployment now points
  to version `21`. Marketplace App Configuration is saved against version `21`.
- The saved, unsubmitted Marketplace draft uses the Prereasoner card banner and three 1280 × 800
  illustrations rendered from the actual add-on UI: question, answer with reasoning, and previous
  conversations. All use the approved logo source `C:\work\FormFacade\public\logo-full.png`.
- The OAuth branding name is saved as **Prereasoner**. Google marks the earlier verified branding
  as modified and requires re-verification before it displays the new name; no OAuth verification
  request has been submitted.
- The Marketplace listing is saved as a draft and has not been submitted for review.

## Verification follow-up — September 20, 2026

- The privacy policy now documents concrete data-protection mechanisms and affirmatively states
  compliance with the Google User Data and Developer Policy, including Limited Use requirements.
- The policy states that raw or derived Google user data is not used or transferred to train or
  improve generalized or non-personalized AI/ML models.
- The add-on shows a concise data-use and no-generalized-training disclosure before the first
  question is submitted.
- The updated add-on was pushed with `clasp` and released as immutable Apps Script version `20`.
- Apps Script version `20` was the earlier verification build; version `21` carries the corrected
  canonical name throughout the add-on UI and review materials.
- The OAuth consent branding was verified under its previous name. The updated OAuth brand name is
  saved as **Prereasoner** and requires re-verification before Google shows it on the consent screen.
- OAuth verification was approved on September 22, 2026 for
  `script.external_request` and `script.container.ui`. The approved scopes are the two sensitive
  Apps Script scopes; the identity and current-spreadsheet scopes remain in the app configuration.
- The replacement OAuth demo video was provided to Google at https://youtu.be/hXyQ9CYCfBM.
- The Marketplace Store Listing draft omits its optional YouTube promo video and retains the
  workflow images documented above.
- The prior Marketplace rejection requested trademark attribution and clearer representative
  screenshots. The listing uses **Google Sheets™** and includes the Google LLC trademark attribution;
  its three current images show the add-on workflow at 1280 × 800. They replace the earlier set
  of five low-legibility screenshots.
- Marketplace changes are saved in a draft and have **not** been submitted for review. OAuth
  data-access verification was approved September 22; only re-verification of the changed OAuth
  branding name remains pending, and that branding request has **not** been submitted.

## Previous submission status — September 15, 2026

- Marketplace App Configuration was previously saved with Apps Script version `18`, the developer contact,
  and the exact six OAuth scopes declared in `appsscript.json`.
- The earlier Marketplace Store Listing draft had five workflow screenshots at 1280 × 800, icons,
  card banner, and post-install tip. The screenshots and product naming have since been updated as
  described above.
- OAuth data-access verification was approved September 22, 2026. OAuth branding had been verified
  under the previous name; the changed **Prereasoner** name now awaits branding re-verification.
- Logo source: `C:\work\FormFacade\public\logo-full.png`. SHA-256:
  `C8663738280FBF368E3D1768F97FC70C8F1B1F2C8E04DF2684923466DEFFCD41`.
