# Prereasoner - Excel Copilot — implementation plan

Status: **Tenant-independent code and local verification complete; integration/release blocked on external setup**, September 24, 2026. The local task pane, Office manifest, bounded workbook reader, auth dialog/session bridge, conversation history, and host-aware session persistence are implemented. Focused frontend/backend suites pass. The manifest requests `ReadWriteDocument` because Excel's application-specific APIs require it, while the add-in itself remains read-only. No production deployment or workbook sideload has been performed. Microsoft/Firebase provider configuration and sign-in verification require app-registration access; applying database migrations/grants and deploying the compatible service version require the production operator path; live Excel host validation requires a sideloaded add-in. Marketplace enrollment and certification remain later release work.

## 1. Product and release scope

Build an Office.js task-pane add-in that brings the existing Prereasoner conversation and reasoning experience into Excel. Proposed product name: **Prereasoner - Excel Copilot**. Use the existing Prereasoner logo artwork from `C:\work\FormFacade\public`, with the same identity across the manifest, sign-in, sidebar, website, and listing.

First release:

- Ribbon commands: **Ask a question** and **Previous conversations**.
- Analyze visible, non-empty worksheets in the current workbook after the user sends a question.
- Show the answer, expandable calculation steps, compact results, and links to the full analysis in Prereasoner.
- Continue a conversation with follow-ups; clearly indicate when workbook data has changed.
- Fetch previous conversations from the authenticated user's database and open `/reason/<conversation_id>` in the browser.
- Preserve spreadsheet cells, formulas, formatting, and worksheet structure. Initial design also avoids writing add-in metadata into the workbook.
- Support Excel for Microsoft 365 on Windows, Mac, and the web, subject to the API/build compatibility matrix validated during the first milestone.
- Use the existing account and entitlement model across the web app, Sheets, and Excel. Verify where production subscription enforcement lives before claiming billing reuse is complete.

Defer cell/formula writing, custom worksheet functions, automatic background uploads, offline inference, mobile-specific UX, Graph-based drive browsing, and organization-wide shared conversations. These are separate features, not prerequisites for the existing product experience.

## 2. What is already reusable

| Existing owner | Reuse | Required work |
| --- | --- | --- |
| `web/public/lib/turn-renderer.js` | Already shared by the web workspace and Sheets: turn ordering, escaped Markdown, reasoning disclosure/tree | Consume directly; inject host-specific actions and CSS. Do not create another renderer. |
| `web/public/lib/result-wire.js` | Response/trace decoding | Reuse directly and retain compatibility fixtures. |
| `sheets-addon/Sidebar.html` | Composer, conversation state, progress, retry, recalculation, restore UX | Extract host-independent controller incrementally; replace `google.script.run`, Google error messages, and Apps Script template bootstrap with adapters. |
| `sheets-addon/Code.js` | Workbook limits, request/response shape, reasoning normalization | Extract pure normalization into shared browser-compatible functions. Replace SpreadsheetApp, UrlFetchApp, and Google token exchange. |
| `web/public/lib/workbook-conversations.js`, `sheets-addon/Previous.html` | History presentation and database query pattern | Extract a small paginated history component with explicit dependencies; existing web file relies on workbook globals and cannot be imported wholesale. |
| `web/public/lib/firebase-init.js` | Firebase initialization and owner-scoped live trace subscriptions | Separate initialization, provider selection, and subscriptions. Current sign-in automatically redirects to Google and cannot run unchanged inside a task pane. |
| `web/public/lib/workbook-import.js`, `xlsx-reader.js`, `upload-limits.js` | Existing import normalization, layout validation, dates, limits | Reuse pure functions where possible. Office.js supplies live cell values, so the add-in does not need to export and parse XLSX files. |
| `orchestrator/server.py`, existing engine routes | `/chat`, calculations, provenance, named analyses, source sync | Retain contracts; add durable request recovery if required by transport tests. |
| `engine/conversations.py` | Ownership, listing, snapshots, retention, source hashing | Reuse; add the Excel source kind to frontend categorization where needed. |
| `engine/sheet_sessions.py`, spreadsheet session routes | Server-stored active conversation and sidebar state | Generalize host/document binding without changing existing Google bindings. |
| `engine/auth.py` | Firebase token validation and RTDB UID separation | Add stable principal resolution before Microsoft account linking. |
| Firebase Hosting / Cloud Run / RTDB | Hosting, APIs, trace delivery, deployment | Add Office pages, correct embedding/cache headers, validate CORS and timeout behavior. |
| Existing privacy/support/terms and branding | Base documentation and assets | Extend factual product disclosures for Microsoft identity and Excel; preserve approved Google branding/configuration. |

The analysis engine is largely reusable. The main new engineering is the Excel host adapter, authentication integration, durable workbook binding, and cross-client validation. Code-reuse percentages would be misleading until extraction is complete.

## 3. Architecture and responsibility boundaries

```text
Excel ribbon command
        |
        v
Office task pane: /office/excel/taskpane.html
        |
        +-- shared conversation controller + turn renderer
        |       +-- question, answer, steps, follow-up, history
        |
        +-- ExcelHost adapter (Office.js)
        |       +-- capability checks, workbook snapshot, local change signal
        |       +-- document locator and external-browser navigation
        |
        +-- Auth adapter
        |       +-- Firebase session, Microsoft/Google login, account linking
        |
        +-- API/trace adapter
                +-- Firebase bearer token -> existing /chat and /api routes
                +-- Firebase UID -> existing RTDB trace subscriptions
                           |
                           v
                 Stable account principal
                           |
                 Existing engine + owned conversations
```

Host methods return plain data, never Office proxy objects. API code does not know about Excel ranges. The renderer does not read workbooks, obtain tokens, or decide account ownership.

Proposed interfaces:

```text
HostAdapter:
  initialize() -> capabilities
  describeWorkbook() -> name, sheet summaries, document locator
  readSnapshot() -> normalized tables, local source map, fingerprint
  watchChanges(callback) -> dispose
  openExternal(url)

AuthAdapter:
  signIn() / signOut()
  getSession() -> Firebase UID, display identity
  getIdToken() -> fresh Firebase bearer token

ConversationService:
  list(cursor), restoreBinding(documentKey), saveBinding(...), clearBinding(...)
  ask(question, snapshot, conversationId, turnId), recoverTurn(turnId)
  subscribeTurn(turnId, callbacks), syncSources(...)
```

`recoverTurn` is proposed new work, not an existing API. Keep shared code as small JavaScript modules compatible with the repository's current frontend. Do not require a React rewrite. Use a deterministic packaging step for Apps Script if extracting modules requires generating its HTML includes; generated copies must have one source of truth.

## 4. Workbook reading and normalization

1. Await Office initialization and check required API sets. Inventory visible worksheets and used-range dimensions locally. Avoid loading entire columns or a formatting-inflated used range.
2. On Send, read bounded ranges using batched Office.js operations. Retain the current initial limits: eight non-empty worksheets, 10,000 total data rows, two million characters per table, six million combined. Add explicit column/cell-count and UTF-8/request-byte guards after auditing backend validators; current Apps Script constants count characters, not bytes.
3. Use values-only used-range discovery where supported. Preserve the source range's offset so data beginning at C5 does not get mislabeled as A1. Prefer a structured Excel table's header/data body when it unambiguously represents the sheet's data. Exclude its totals row. If a worksheet has multiple independent tables or an ambiguous layout, show a specific selection/error state instead of silently combining them.
4. Keep numbers as numbers, leading-zero text identifiers as text, booleans explicit, and formula results as their calculated values. Use cell types/number formats and the workbook date system to distinguish date serials from ordinary numbers; preserve timezone-free date semantics. Handle Excel error cells, duplicate/blank headers, merged headers, empty rows, and locale-specific display text explicitly.
5. The default scope is visible worksheets; all rows within those worksheets are analyzed, including filtered-out and manually hidden rows. This matches the existing worksheet-level model. State this in the data-scope disclosure. A future visible-rows-only mode must preserve row provenance and have explicit selection semantics.
6. Keep worksheet ID, range address, and source row offsets in a local provenance map. Only expose a source-cell navigation action when the engine's evidence can be mapped back exactly; otherwise link to the stored source rows in Prereasoner. Never infer a source row from a displayed result position.
7. Batch metadata reads and chunk value reads conservatively. Office's payload limit is separate from our upload limits; validate on actual hosts and reduce chunks on oversized-payload errors. Microsoft documents a 5 MB request/response limit for Excel on the web. [Performance guidance](https://learn.microsoft.com/en-us/office/dev/add-ins/excel/performance)

Read values only on an explicit question/sync action. Change events should mark the local snapshot stale, not upload it. Use focus/send-time fingerprint checks as a fallback where events are unsupported or missed. During a read, detect edit activity and boundedly retry; do not claim an atomic workbook snapshot across multiple Office batches. If edits continue, ask the user to finish editing and retry. Manual-calculation workbooks need a clear warning about cached formula values; do not force recalculation silently.

## 5. Identity and sign-in

### Stable account ownership comes first

The current `_verify_principal` returns a Google provider ID when present and otherwise the Firebase UID. RTDB uses the Firebase UID separately. Therefore a Microsoft-only account that later links Google could switch database ownership under the current implementation. This is a real code-level risk, not merely a sign-in UX issue.

Add a server-controlled registry mapping verified Firebase UID to an immutable storage principal:

- Existing Google users retain their current Google-based storage principal and existing data.
- New Microsoft-only users receive a stable principal that never changes when providers are linked.
- Resolve/backfill known existing Firebase users before enabling linking. For an unmapped UID, perform a controlled lookup using verified identities and existing ownership records; ambiguous historical ownership requires reconciliation, not a new empty account.
- Enforce uniqueness and transact first-use creation. Derive all ownership from verified tokens; never accept a principal, email match, or tenant ID from the client as proof.
- Link providers only after proving control of both identities. If two populated accounts already exist, do not automatically merge their conversations, references, or billing records. Keep the current account usable and offer an explicit reconciliation path.
- Preserve RTDB paths keyed by Firebase UID. Audit storage quotas, reference ownership, subscription checks, and deletion so they all resolve the same stable principal.

### Production baseline: Firebase login in an Office dialog

Enable Microsoft's Firebase OAuth provider and retain Google login for existing customers. Open an Office dialog from an explicit sign-in click; its initial page is on the task-pane origin. Complete the provider flow in that dialog and return to a same-origin callback. The web app must support Microsoft login too, otherwise Microsoft-only users cannot open their analysis links.

Do not assume cookies/storage are shared between the dialog, pane, and external browser. Establish a task-pane Firebase session using a short-lived, single-use authorization handoff bound to a pane-generated challenge. Exchange the returned code server-side for a Firebase custom token for the already verified UID; then use Firebase's normal token refresh. The handoff requires expiry, replay prevention, origin checks, rate limits, and sign-out handling. Do not put bearer/refresh tokens in URLs or workbook settings. [Office dialog guidance](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/dialog-api-in-office-add-ins)

Firebase explicitly does not support taking an arbitrary Microsoft OAuth access token and treating it like a Google provider credential. Use the provider's complete authorization-code flow for this baseline. [Firebase Microsoft authentication](https://firebase.google.com/docs/auth/web/microsoft-oauth)

### Optional Office SSO optimization

Microsoft now lists nested app authentication (NAA) as generally available for Excel web, Windows, and Mac. Detect support at runtime and preserve the dialog fallback. NAA can remove repeated sign-in steps, but its tokens must be bridged deliberately into our Firebase/account model; it is not a replacement for account ownership. [NAA support](https://learn.microsoft.com/en-us/javascript/api/requirement-sets/common/nested-app-auth-requirement-sets)

If implemented in the first release, request a token intended for our registered API, validate its audience, issuer/tenant, signature, expiry, and authorized client, then resolve a server-controlled Microsoft-identity mapping to the existing Firebase UID. Never accept a Graph token intended for another resource. Establish that mapping through proven account linking; otherwise fall back to Firebase provider login. This is an additional integration milestone and should not delay a functional cross-platform dialog-based release.

Configure the Entra registration for organizational and personal Microsoft accounts. Register exact development/production callbacks; store any provider secret in managed server-side configuration. No Graph file permissions are needed to read the open workbook through Office.js. [NAA registration guidance](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/enable-nested-app-authentication-in-your-add-in)

## 6. Workbook identity and conversation history

History always comes from `/api/conversations` under the authenticated principal. Do not introduce a browser-local list of conversations.

Generalize the server binding to `(principal, host, document_key)` with versioned sidebar state. Keep existing Google route payloads/keys as a compatibility path. Namespace Excel bindings explicitly and enforce ownership on every restoration/save. Do not blindly apply the existing Google session module's content-hash fallback to Excel: two different workbooks can contain identical data.

Default document identification:

- For saved documents with a usable locator, derive an opaque key from the canonical locator with a namespaced hash. Treat it as a locator, not a guaranteed permanent Microsoft file ID. Avoid uploading raw local filesystem paths.
- Path/URL changes, renames, Save As, and copies can produce a new binding. Offer **Resume a previous conversation** when automatic identification is uncertain.
- For unsaved documents or hosts without a reliable locator, use an in-memory session key. After reopening, history remains available from the database; automatic workbook restoration is not promised.
- A content fingerprint detects changed data and can suggest owned candidate conversations. It cannot uniquely identify a workbook or authorize access.
- Do not store a workbook UUID in custom properties/settings by default: that modifies the file and copies inherit the identifier. If seamless restoration across rename/move becomes mandatory, explicitly choose between document metadata and Microsoft drive identity access as a separate tradeoff. [Office persistence behavior](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/persisting-add-in-state-and-settings)

Opening an older analysis displays its stored data and results. It must not silently replace that analysis with the current workbook snapshot. **New chat** persists a blank binding so reopening does not resurrect the previous conversation.

## 7. UX and visual design

Use Excel's native task-pane title, a quiet neutral surface, readable typography, and restrained Prereasoner purple for interactive accents. Reuse the real renderer and source-backed calculations. Do not add a landing page, marketing cards, fake verification badges, or a second product header inside the host title.

```text
Excel ribbon: Prereasoner
  [Ask a question]  [Previous conversations]

Task pane: Prereasoner - Excel Copilot
  + New chat                         2 sheets
  ------------------------------------------
       What is the total amount in France?

  Your total is ...
  ▸ Reasoning steps for France total
      Filter France orders
      Calculate the total
      Open calculation in Prereasoner ↗

  [conversation scrolls in this area]
  ------------------------------------------
  Ask a follow-up...                      ↑
```

- First open: sign-in action if needed, one concise instruction and data-use disclosure, focused question box once ready. Sheet count can reveal the included sheets and the all-rows scope.
- Progress: truthful states such as reading workbook or calculating, derived from actual request/trace state; keep the question visible. Preserve typed text after recoverable errors.
- Completed turn: use the same answer/reasoning ordering as the shared renderer. Open detailed calculation sheets in the full Prereasoner browser view; do not create tabs inside the source Excel workbook.
- Changed data: show **Workbook changed** and an explicit **Update analysis** action. Old answers remain labeled as based on the earlier data until successfully recalculated.
- Previous conversations: a compact server-backed list with question and date, pagination, loading/error/empty states. Selecting one opens its direct Prereasoner URL. A ribbon command can open the same pane in history mode.
- Support keyboard navigation, Enter to send/Shift+Enter for newline, visible focus, accessible disclosure controls, screen-reader announcements, reduced motion, high contrast, and narrow/resized panes. Test long questions, long result cells, and translated/large text.
- Validate layouts around 320, 400, and 600 CSS pixels. Keep the composer visible without allowing the keyboard or error blocks to cover conversation actions. [Task-pane design](https://learn.microsoft.com/en-us/office/dev/add-ins/design/task-pane-add-ins)

## 8. Hosting, manifest, and request reliability

Use `/office/excel/` for add-in pages; `/excel` already serves the existing upload landing page. Start with the production-supported XML add-in-only manifest, Office.js from Microsoft's hosted CDN, a stable add-in GUID, versioned asset URLs, and runtime capability checks. Select the minimum Excel API requirement set from the actual APIs used during the spike rather than declaring a speculative version. [Manifest guidance](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/add-in-manifests)

The manifest must declare `ReadWriteDocument` for Excel-specific APIs even though product code does not write source data. Explain the actual behavior accurately in consent/help text. [Permission requirement](https://learn.microsoft.com/en-us/office/dev/add-ins/develop/requesting-permissions-for-api-use-in-content-and-task-pane-add-ins)

Repository-specific deployment changes:

- `web/firebase.json` currently sets `X-Frame-Options: SAMEORIGIN` globally. Scope those rules so Office pages can be framed by supported Excel web hosts. Set and test appropriate `frame-ancestors` restrictions for the full embedding ancestor chain; do not send contradictory X-Frame-Options headers. Preserve other application pages' protections.
- The same file marks SVG/JS/CSS assets as noncacheable. Give versioned Office icons cacheable headers, as required by Microsoft's manifest guidance. Verify actual response headers after deployment, including clean-URL paths.
- Existing Apps Script bypasses the Firebase Hosting `/chat` timeout by calling Cloud Run directly. The Excel browser adapter must likewise use a tested transport: direct authenticated Cloud Run requests with the exact add-in origin allowed by CORS, or a durable asynchronous API. Verify preflight behavior and do not expose service credentials to the browser.
- Preserve `turnId` across retries. Existing trace IDs do not by themselves prove request deduplication. Add an authenticated durable turn record/idempotency contract if missing, so a timeout/reopened pane can recover status and final output without repeating paid inference. Scope idempotency to principal plus turn ID and reject a reused key with a different payload.
- If the current synchronous worker cannot survive the selected transport's lifetime, use a durable job runner and authenticated status endpoint before shipping. Do not treat a client-side abort as cancellation of server inference.
- Keep release assets compatible with the submitted Sheets version. Version shared bundles or retain backward-compatible entry points; changes to shared web files can otherwise affect the installed Google add-on immediately.

## 9. Proposed repository changes

| Location | Planned responsibility |
| --- | --- |
| `excel-addon/manifest.xml`, `README.md` | Office registration, local sideload instructions, supported client matrix |
| `web/public/office/excel/taskpane.html`, `taskpane.css` | Minimal Office shell and theme |
| `web/public/office/excel/host.js` | Office capability detection, batched reads, changes, locators, external navigation |
| `web/public/office/auth/` | Dialog entry/callback and task-pane authentication adapter |
| `web/public/lib/copilot/` | Shared controller, history view, normalization, API transport interfaces |
| Existing `turn-renderer.js`, `result-wire.js` | Continue as canonical rendering/wire utilities |
| `engine/auth.py` plus principal registry module/migration | Immutable ownership mapping with legacy compatibility |
| Workbook session module and routes | Host/document namespace, state versioning, owned bindings |
| Auth handoff routes and durable turn routes, if required | Secure Firebase session establishment and retry recovery |
| `web/firebase.json` | Office framing, routing, cache rules |
| `excel-addon/tests/`, focused engine tests | Host fixtures, account ownership, restoration, Office integration evidence |
| `docs/EXCEL_MARKETPLACE.md` | Actual release identifiers, support matrix, listing, reviewer instructions |

These are proposed locations. Use existing naming conventions if equivalent owners already exist when implementation begins. Keep deployed URLs, client IDs, and supported builds in release configuration, not scattered UI literals.

## 10. Implementation milestones and estimates

Estimates are engineering working days for one developer familiar with this repository, assuming account access is available. External account enrollment and review time are additional. The earlier 2–3 week estimate is optimistic given the identity and hosting findings.

| Milestone | Deliverable / exit condition | Estimate |
| --- | --- | --- |
| 1. Excel integration spike | Sideloaded pane on Windows/web, read a bounded sample workbook, renderer displays a fixture, sign-in/cookie/header viability established; Mac support checked early | 2–3 days |
| 2. Shared UI extraction | Sheets and Excel run the same renderer/controller contracts; existing Sheets behavior and browser tests pass | 2–3 days |
| 3. Identity and sessions | Stable principal mapping, Microsoft/Google sign-in, proven linking, browser deep-link access, host-aware binding | 3–5 days |
| 4. Complete analysis flow | Real multi-sheet request, live trace, answer, follow-up, stale-data handling, previous conversations, reliable retry/recovery | 3–4 days |
| 5. Cross-platform release hardening | Windows/Mac/web matrix, accessibility, representative workbook fixtures, privacy and permission copy, signed-in reviewer flow | 3–4 days |
| 6. Submission package | Validated manifest, versioned deployment, icons/listing, clear reviewer instructions, final smoke test | 1–2 days |

Expected total: **14–21 engineering days** for a submission-ready release. A demonstration with fixture/simple sign-in can be available in **3–5 days**, but is not release evidence. Optional NAA-to-Firebase SSO adds approximately **2–4 days**, subject to tenant configuration and identity mapping tests. A required durable job infrastructure redesign or account-data reconciliation would require revising the estimate after milestone 1/3 evidence.

## 11. Verification and release gates

Automate meaningful boundary checks and supplement them with actual Excel testing; a browser mock cannot prove Office API or consent behavior.

| Area | Required evidence |
| --- | --- |
| Workbook correctness | Multi-sheet joins, used-range offsets, leading-zero IDs, dates including 1904 mode, locale formats, formula errors, totals rows, hidden sheets, filtered rows, empty/large/ambiguous workbooks |
| Read-only behavior | No cell/formula/format/worksheet writes, no added metadata, no unexpected dirty-workbook state across normal operation |
| Identity | Existing Google user's history survives linking; Microsoft-only user retains data after later linking; wrong-account/tenant tokens rejected; expired handoff and replay rejected; RTDB remains UID-isolated |
| Conversation lifecycle | Follow-up context, New chat, reopen, Save As/rename, two open workbooks, no-locator fallback, saved-analysis links, user switching, server-side pagination |
| Recovery | Slow run beyond Hosting timeout, lost connection, pane closed mid-run, duplicate send/retry, RTDB unavailable, token expiry, edited data during reading |
| UX | Narrow/large panes, keyboard and screen reader, high contrast, readable long content, visible composer, understandable authentication and limit errors |
| Hosts | Supported Windows and Mac builds; Excel web in Edge/Chrome and applicable Safari; personal and work/school accounts; local/OneDrive/SharePoint files where supported |
| Regression | Existing shared-renderer/wire tests, `sheets-addon/tests`, relevant web browser journeys, backend conversation/session/auth tests |

Record exact builds and outcomes. Validate the manifest with Microsoft's tooling and sideload the release manifest against production-like HTTPS. Restrict declared requirements/availability to tested supported behavior and Microsoft's certification rules; do not imply every historical Excel build is supported.

Stage the new routes and identity mapping first, then private sideloading, then a small user beta. Monitor auth failures, workbook read duration, payload limits, duplicate requests, and timeout recovery without logging raw cells, questions, or tokens. Feature-gate Microsoft sign-in and retain previous assets/schema compatibility for rollback. Do not remove additive account mappings during rollback.

Publish through Microsoft Marketplace/Partner Center when the release gates pass. Prepare accurate product name, existing logos, support/privacy/terms links, simplified representative listing images, data-access explanation, pricing, and reviewer navigation/account access. Approval is separate from Google's approval. [Distribution options](https://learn.microsoft.com/en-us/office/dev/add-ins/publish/publish)

## 12. What is needed from the owner

No further input is required to complete this plan or begin the local scaffold. External integration/release needs:

1. **Microsoft organization/account:** identify the Microsoft 365/Entra tenant to own the application and an account allowed to create app registrations. Use the signed-in console or assigned access; do not send passwords in chat.
2. **Marketplace publisher:** identify an existing Partner Center publisher for the business, or complete enrollment/legal identity verification if none exists. Confirm the desired public publisher name.
3. **Testing access:** a licensed Windows Excel installation and a work/school test account; access to Mac Excel or a tester before claiming Mac support. A personal Microsoft account covers the other sign-in path.
4. **Microsoft auth configuration:** allow configuring the chosen Entra registration and Firebase Microsoft provider when implementation reaches integration. These credentials/configuration are separate from existing Google approvals.
5. **Commercial ownership:** identify the production subscription/usage service if it lives outside this repository, so Excel uses the same account entitlements and pricing.

Defaults unless changed: product name **Prereasoner - Excel Copilot**, existing Prereasoner backend and hosting, same pricing, no source-file modification, server-authoritative history, Microsoft and Google sign-in, and Windows/Mac/web as target clients. Automatic restoration after moving/renaming a workbook is best effort under the no-file-modification default; history remains available in all cases.

## 13. Completion criteria

A new user can install/sideload, sign in, ask a question about a real Excel workbook, inspect the source-backed calculation in Prereasoner, ask a follow-up, close/reopen the pane, and retrieve their previous conversations. Existing Google users retain their data. Source workbooks remain unchanged. Supported host builds pass the recorded matrix. Deployment and listing materials describe that tested behavior exactly.
