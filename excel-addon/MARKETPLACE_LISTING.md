# Microsoft Marketplace listing draft — Excel

## App name

Prereasoner

## Summary (96 characters; Microsoft maximum: 100)

Ask questions in plain language and inspect the source rows and calculations behind each answer.

## Long description (HTML draft)

```html
<p><strong>Ask questions about your Excel workbook in plain language. See the source rows and how each answer was calculated.</strong></p>

<p>Excel gives you formulas, lookups, and PivotTables for exploring data. Prereasoner lets you start with the question instead: ask about the workbook already open, review the result, and inspect the calculation steps and supporting data behind it. Use it to explore a workbook without first building a formula or translating your question into query code.</p>

<h2>Features</h2>
<ul>
  <li><strong>Ask in plain language.</strong> Ask questions such as “What were sales in France?” or “Which products had the highest sales?” without writing formulas or query code.</li>
  <li><strong>Inspect the calculation.</strong> Expand the reasoning steps to see how the answer was produced, including the operations used to filter, combine, group, or calculate data. Open the full analysis to inspect supporting rows and more detail.</li>
  <li><strong>Analyze related worksheets.</strong> Work across visible, non-empty worksheets in the current workbook and connect related records where the data supports it. Ask for totals, averages, rankings, comparisons, and other summaries across the data you provide.</li>
  <li><strong>Continue the conversation.</strong> Ask follow-up questions using the same workbook context, or reopen a previous analysis.</li>
  <li><strong>Keep the workbook unchanged.</strong> The add-in reads workbook data when you submit a question. It does not edit cells or write results into the source workbook.</li>
</ul>

<h2>Useful for</h2>
<p>Explore sales by country, product, customer, or period; summarize operations and inventory; check totals, ratios, and currency conversions; or connect related records across worksheets. For example, ask which products had the highest sales, compare order totals between regions, or summarize delivery performance. Review the calculation steps and, when you need to validate a result, open the full analysis to inspect the supporting rows.</p>

<h2>How it works</h2>
<p>Open Prereasoner from Excel and submit a question when you are ready to analyze the workbook. The add-in reads visible, non-empty worksheets in the open workbook for that request and sends the selected workbook data and question to the Prereasoner service to calculate an answer. The original workbook remains the source of truth: Prereasoner does not change cell values, formulas, formatting, or worksheet structure. You can continue with a follow-up question, start a new conversation, or reopen a saved analysis later.</p>

<h2>Account and pricing</h2>
<p>An internet connection and a Prereasoner account are required. The service offers 50 questions per month on its free plan. Pro plans start at $18 per month when billed annually. See <a href="https://prereasoner.com/excel-copilot/pricing.html">Excel Copilot plans and pricing</a>.</p>

<p>For details about data handling, see the <a href="https://chat.prereasoner.com/privacy">Privacy Policy</a>. Read the <a href="https://chat.prereasoner.com/terms">Terms of Service</a> or visit <a href="https://chat.prereasoner.com/support">Support</a>.</p>

<p>Excel is a trademark of Microsoft Corporation. This add-in is provided by MailRecipe LLC and is not affiliated with or endorsed by Microsoft.</p>
```

## Store metadata and assets

- Category: Office Applications; select the closest Excel/data-analysis categories offered in Partner Center.
- Privacy policy: https://chat.prereasoner.com/privacy
- Terms / EULA: https://chat.prereasoner.com/terms
- Support: https://chat.prereasoner.com/support
- Product page: https://prereasoner.com/excel-copilot/
- Pricing: https://prereasoner.com/excel-copilot/pricing.html
- Use the supplied FormFacade brand files staged in `store-assets/`: [`prereasoner-logo-96.png`](store-assets/prereasoner-logo-96.png) and [`prereasoner-logo-1024.png`](store-assets/prereasoner-logo-1024.png). Use the size accepted by the Partner Center logo field; keep the 1024px file as the high-resolution source. [`prereasoner-logo-32.png`](store-assets/prereasoner-logo-32.png) is the exact 32px source icon already used by the Excel manifest. Upload [`prereasoner-excel-copilot-screenshot.png`](store-assets/prereasoner-excel-copilot-screenshot.png) as the listing screenshot. Caption: “See how a workbook answer is calculated from its source rows.” The screenshot uses the supplied concept SVG, cropped only to fit the 1280×720 store image ratio. Do not use screenshots containing personal workbook content.
- Video is optional; no Marketplace video is required for this listing.

## Certification notes draft

1. Open the supplied synthetic Excel workbook and select **Home → Prereasoner → Ask a question** (or open the add-in from **My Add-ins**).
2. Sign in using the reviewer test account provided in Partner Center.
3. Ask “What were sales in France?” and confirm that the response includes an answer and expandable calculation steps.
4. Ask a follow-up question and verify that the conversation retains its workbook context.
5. Confirm that the source workbook cells are unchanged.
6. Open **Previous conversations** and select the saved analysis.

Before submission, provide working reviewer credentials and confirm whether the reviewer needs a paid entitlement. Do not put employee contact details or secrets in this draft.

## Submission checks (not customer-facing copy)

- Partner Center was checked on 2026-09-30. The signed-in account has no publishing workspace or Office Store enrollment yet; Microsoft’s Office Store enrollment page asks for a work account. The current personal `@live.com` sign-in cannot be used to populate an offer until the publisher account is enrolled.
- After enrollment, confirm the selected publisher name matches `MailRecipe LLC` in `excel-addon/manifest.xml`.
- Reserve `Prereasoner` in Partner Center and keep it identical to the manifest `DisplayName`.
- Confirm the advertised free/paid plan limits and entitlements match the live Excel Copilot pricing and sign-up flow.
- Capture at least one legible Marketplace screenshot with synthetic data.
- Run the current Microsoft manifest validator and complete web, Windows, and Mac host checks before submission.
