# Claude ↔ Codex handoff log

Rolling log of Claude's work on the Spider accuracy program: decisions, progress,
results, and open questions. Newest section at the top. Committed evidence lives in
`spider/results/RESULTS.md`; program state in the memory file
`spider-accuracy-program.md`. This file is the running narrative between the two of us.

---

## 2026-09-25 (night) — The Sheets add-on is the web workbook; one import rule; "sales" is a money total

The owner asked why the Google Sheets add-on failed, why its sidebar was worse than chat.prereasoner.com,
and why it showed no live progress. The HTTP 500 was the engine's cached connection (released as
`5bb21a2`, entry below). The sidebar was a separate hand-written UI (`Sidebar.html`, `Previous.html`),
not `workbook.js`. Its Apps Script server called `/chat` and returned only the final JSON, so nothing
could follow the RTDB trace. The owner chose to embed the web app's sidebar, one import rule for
uploads and add-ins, and "sales" as a money total.

**What changed** (DECISIONS.md has the three entries):
- `sheets-addon/` is a host. `Sidebar.html` frames `/embed/sheets` (`reason.html` + `workbook.js`,
  compact layout), and `Code.js` answers `context` / `grids` with the Google token, the spreadsheet id
  and cell grids. The page side is `web/public/lib/host-bridge.js`: origin checks, Firebase
  `signInWithCredential`, the upload importer, the spreadsheet session and a re-read before each
  question. The add-on dropped `script.external_request` and its URL allowlist. Only `/embed/**` may be
  framed by `docs.google.com` / `*.googleusercontent.com` (`web/firebase.json`).
- Opening the sidebar is one page load: the boot writes the frame's session before the workbook runs,
  and `workbook.js:adoptSession` re-reads the three values the page captures at load (`SHEETS`,
  `TABNAMES`, `question`). Only a sheet changed mid-conversation and New chat reload the frame, once.
- The Excel add-in and the Sheets add-on send grids to `xlsx-worker.js`
  (`WORKBOOK_IMPORT.gridWorkbook`), so a sheet reads exactly as its upload. A populated column without a
  header is refused, not named `column_N`, and every import error names the worksheet. Both add-ins
  check the upload's per-worksheet limits (`upload-limits.js`); they used to cap all tabs' rows together.
- Found while testing the embed, each fixed with a browser test that fails without the fix:
  - With an empty session the page fell back to the web demo question and tables, and asked "top 3
    cities by total amount" on its own.
  - `settle()` never repainted the source chip, so a Google-sourced answer in chat mode kept showing
    "Checking source data". This was a web app bug as well.
  - A sheet changed within the 700 ms before an answer's snapshot reached the server lost that answer
    from view. The rebuild now keeps the tab's own snapshot for the same conversation.
- The empty sidebar keeps the old add-on's data-use notice word for word, because Google's OAuth
  verification relied on it (docs/GOOGLE_WORKSPACE_MARKETPLACE.md).
- The sales rule (regress B6): a money noun that names its table reads as that table's money total
  unless the question counts or lists it. Neither the Spider dev set nor the shipped demo questions can
  fire it: 0 of 1,034 dev questions (no dev table name contains a money noun) and 0 of 91 demo questions.

**Gates** (dev worktree, then main):
- compileall.
- `npm run test:web`: 7 suites, including `Sheets add-on` 29 and `Excel workbook reader` 14.
- `npm run test:browser` 32/32. The three embed journeys run against a stand-in host
  (`/__sheets-host`). They cover sign-in, the first question, reopening in the same tab and in another
  browser, a changed sheet (sync plus pending question), a change right after an answer, New chat,
  Previous conversations and an unreadable sheet. The existing picker journeys flake about 1 in 30 runs
  on unmodified main as well; a separate task was suggested.
- `tests.run_all` on the dev worktree: 44 of 45 suites OK, among them `test_sql_ast` 119/119,
  `test_compose` 14/14, `test_calculations` 104/104 and live `test_orchestrator` 25/25. `test_datasets`
  passed 61 of 65 checks. The other 4 could not connect to the local database proxy (Windows
  `WSAEADDRINUSE` on 127.0.0.1:5432 while browser suites ran alongside), and their 3 datasets pass on a
  rerun (`EVAL_DATASETS=customer-orders,customers-orders,eval-formesign-assets-xls`).

**Open**
- Deploy order: Hosting first (the CSP must allow the frame before the add-on points at it), then
  `clasp push` and a new Apps Script version. Verify `/embed/sheets` sends exactly one
  `Content-Security-Policy`, the Google `frame-ancestors`.
- Hosting currently serves another session's uncommitted Excel files (entry below). A deploy from a
  clean commit rolls them back.
- The three review images are rendered from the embed. The saved Marketplace draft keeps the old ones
  until they are uploaded, and the OAuth demo video shows the old sidebar
  (docs/GOOGLE_WORKSPACE_MARKETPLACE.md).
- `docs/EXCEL_COPILOT_PLAN.md` (another session's uncommitted edits) still describes the old Sheets
  sidebar; DECISIONS.md records that the embed supersedes it.

## 2026-09-25 (evening) — RELEASED: the engine checks its cached connection (the Sheets add-on's HTTP 500)

**Production now:**
- engine `prereasoner-api-00230-rax` = `engine@sha256:8906537e…` built from `5bb21a2` (8 vCPU / 16 GiB).
  Traffic switched at 17:52 UTC. The three jobs run the same image; the release smoke job passed
  (execution `hxqmg`), and `/api/healthz` returns 200.
- chat is unchanged: `prereasoner-chat-00124-yad` (`3d7189e`).
- Hosting matches no commit. This session deployed `3d7189e` at 00:25 UTC. Another session then deployed
  four times, from 00:49 to 01:16 UTC (latest version `d1f1edcf`), from its uncommitted Excel add-in work
  in the main worktree. Live `office/excel/{taskpane,auth-bridge,turn-bridge}.js` equal that working
  tree, not HEAD; `lib/workbook.js` equals HEAD. A Hosting deploy from a clean commit would roll back
  the live Excel changes.
- Rollback: engine `00227-xey` (`3d7189e`, image `2069c274…`; point the three jobs back at it). This
  release has no migration.

**The defect:** the Google Sheets add-on answered "Prereasoner request failed (HTTP 500)" (the owner's
screenshot). `EntityQuery._rconn` keeps one Postgres connection per engine instance across requests.
Cloud Run's Cloud SQL connector drops that connection around its certificate refresh: each of the 5
failures from 08-30 to 09-25 came 5-57 s after a `cloudsql.instances.connect` audit event, and 8 s to
41 min after the connection was last used. psycopg2 marks a connection closed only after a statement
fails, so the next request's first statement failed. In `5bb21a2`, a connection idle for more than
1 s runs `SELECT 1` before a request gets it. A dropped connection is replaced, and that request's
`[timing]` line shows `pg_stale_reconnect_ms`.

**Gates:**
- The regression test `test_cached_connection_dropped_between_requests_is_replaced_before_use` fails on
  `92d78a5` and passes on `5bb21a2`. `tests.test_request_limits`: 18/18.
- Live `test_datasets` against `5bb21a2` passed: 24 prompts, 40 standalone follow-ups and 1 rewrite.
  The 13 `chat:` follow-ups are skipped by design.
- The revision was deployed with no traffic, checked for health through its tag, then switched.

## 2026-09-25 — RELEASED: the Excel add-in and three review fixes; full Chrome gate 124/124

**Production now** (one revision per service, no tags):
- engine `prereasoner-api-00227-xey` = `engine@sha256:2069c274…` built from `3d7189e` (8 vCPU / 16 GiB).
  The jobs `prereasoner-api-ecb-rates-refresh`, `prereasoner-api-retention-cleanup` and
  `prereasoner-api-release-smoke` run the same image.
- chat `prereasoner-chat-00124-yad` = `chat@sha256:ec248a5c…` built from `3d7189e`.
- Hosting deployed from `3d7189e` at 00:25 UTC. The later `11ed076` and `3a57b0f` change only
  `docs/marketplace/`. Another session redeployed Hosting from uncommitted work afterwards (see the
  evening entry above).
- Database: chat migrations 8-10 applied (8: the host column on Excel document sessions; 9:
  `chat.auth_principal`; 10: spreadsheet sessions keyed by user, host and spreadsheet), then the serving
  grants (`db.reference_grants --role serving`).
- Rollback: engine `00223-vas` (`dedfb27`, image `e9170f8a…`; point the three jobs back at it) and chat
  `00120-huq` (`1be4c6e`, image `afad5e36…`). Migration 10 is not backward compatible. The old engine's
  sheet-session upsert names the old key `(user_id, spreadsheet_id)` and fails against the new one, so
  an engine rollback breaks add-in session saves: prefer rolling forward. Hosting rolls back from the
  Firebase console's release history.

**What shipped, in order**
1. `c8533ca` (owner): the Excel add-in (`web/public/office/excel/`), host-scoped sheet sessions, and one
   storage principal per Google account (`chat.auth_principal`), so the add-ins and the web app share
   one account.
2. Before release: `c8533ca` made the chat service resolve that principal in Postgres, which the chat
   service does not connect to, so every chat request would have failed sign-in. The owner chose the
   engine-only lookup. Fixed in `2e70227`: the chat verifies the Firebase token without a database and
   keys the dataset attestation by the Firebase UID, and only the engine resolves the storage principal.
   The regression test drives the real chat handler with `engine.pg` unimportable (red on `c8533ca`:
   HTTP 401).
3. A review the owner forwarded found three gaps, fixed in `3d7189e`:
   - The grounding guard bound only `column = 'literal'`, and the proposer's imported SQL may put the
     literal first. It now checks both operand orders.
   - `db9a1d8` had stopped checking `!=`, `<>` and `NOT IN`. The owner chose to keep checking
     exclusions. The module and DECISIONS.md state the policy and its same-domain cost, and tests pin
     both sides.
   - The Excel add-in counted any format containing a date letter as a date, so
     `#,##0.00;[Red]-#,##0.00` turned 1,234.50 into 1903-05-18, and both importers turned `[h]:mm`
     durations into 1900 timestamps. `web/public/lib/number-format.js` now owns the rule for the add-in
     and the upload.

   Spider stays 647/1,034, because none of the 1,034 selected dev queries has a reversed literal
   comparison (RESULTS.md).
4. Released `3d7189e` on 2026-09-25 in this order: migrations and grants; both services deployed with no
   traffic and smoke-tested; traffic switched at 00:17 UTC; hosting deployed. For about 90 s between
   migration 10 and the switch, the old engine's sheet-session saves failed.

**Gates:**
- `tests.run_all`, launched on the main checkout while it was clean at `3d7189e`: 45/45 suites, none
  skipped. They include:
  - `test_sql_ast` 114/114
  - `test_request_limits` 17/17
  - `test_app_migrations` 13/13
  - `test_release` 32/32
  - live `test_orchestrator` 25/25
  - live `test_datasets` PASS: 24 prompts and 40 standalone follow-ups. The 13 `chat:` follow-ups are
    covered by Chrome.
- `npm run test:web` on a clean checkout of HEAD: all 6 suites pass, including `workbook layout` (18
  checks) and `Excel workbook reader` (5). The 9 shipped workbooks convert to byte-identical CSV.
- Production checks after the switch:
  - release smoke OK
  - the owner's account lists the same 50 conversations
  - `chat.auth_principal` holds one row
- Full Chrome pass on the live release, all 24 datasets (the 6 upload datasets attached as files):
  - fresh conversations **74/74**
  - the 24 existing conversations from the 2026-09-24 morning pass **50/50**
  - Every one of the 124 turns made an engine call, so none was answered from memory.
  - Paris answers Delta and Omega, and Lyon answers Alpha, Beta, Delta and Omega, in fresh and existing
    conversations.
- Gate driving: Chrome throttles timers in background tabs to about one wake-up a minute, so an in-page
  wait loop stalls and can overlap the next call. Take one locked step per call and wait outside the
  page. Keep compound questions in one tab.

**Open**
- One user's requests still run one at a time behind the per-user advisory lock. Parallel compound
  questions can exceed the chat's 180 s engine timeout (see the 2026-09-24 evening entry).
- Codex's Spider run tagged `codex_positive_grounding` (checkpoint in `spider/results/`) measures the
  exclusion-removal policy the owner rejected, so its numbers do not describe the served engine.
- `LIKE` literals are still unchecked; 11 dev questions select a `LIKE` query.
- The Excel add-in sends dates as `YYYY-MM-DDT00:00:00Z`; the web upload sends `YYYY-MM-DD`. The
  difference predates this release.
- The gate added 24 conversations to the owner's account and 50 turns to existing ones; none were
  deleted.
- Resolved: the owner committed `spider/results/full_eval_served_grounding_whole_db.json` in `c8533ca`.
- Another session has uncommitted Excel add-in work in the main worktree (`web/public/office/excel/`,
  `excel-addon/`, `package.json`, new tests). It is not part of this release (hosting was deployed from
  a clean `3d7189e` checkout) and was left untouched.

## 2026-09-24 (evening) — RELEASED: literal grounding, question fidelity, and four defects the Chrome passes found

**Production now** (one revision per service, no tags):
- engine `prereasoner-api-00223-vas` = `engine@sha256:e9170f8a…` built from `dedfb27` (8 vCPU / 16 GiB).
  The jobs `prereasoner-api-ecb-rates-refresh`, `prereasoner-api-retention-cleanup` and
  `prereasoner-api-release-smoke` run the same image.
- chat `prereasoner-chat-00120-huq` = `chat@sha256:afad5e36…` built from `1be4c6e`.
- Rollback: engine `00220-kud` (`900f3b1`, image `0a77b9f5…`; point the three jobs back at it), chat
  `00118-sok` (`9180169`), `00116-xav` (`dedfb27`) or `00113-tid` (`900f3b1`).

**What shipped, in order**
1. `841f08c` literal grounding: a pool member is eligible only if every text literal it tests occurs
   in its own column, or in no column (the Lyon miss). `900f3b1` question fidelity: a complete question
   reaches the engine as typed, and a rejected dataset op gets one repair. Served Spider `whole_db` on
   `841f08c`: 647/1,034 strict (62.6%), 696 lenient, 304/408 scalar (RESULTS.md). Released as engine
   `00220-kud` and chat `00113-tid`.
2. Second full Chrome pass on that release (all 24 datasets): fresh conversations 73/74 (morning
   71/74), existing conversations 48/50 (morning 46/50). The Lyon, supplier and 'budget' misses are
   gone. The three new misses:
   - "List the product names that no customer from Paris has bought": the model decomposed it as Paris
     customers crossed with products. The engine answered that cross inputs need explicit limits, and
     the model resubmitted with "the top 100" on both lists, which was served as 14 pairs. Fixed in
     `1073c01`: a leaf keeps only a cutoff the question states. The wrong shape itself is rare (42/42
     stubbed proposals for this prompt used the right anti-join).
   - Two re-asked questions answered from memory with no engine call. Belgium came back at the
     morning's exchange rate (367.4342; today 366.0174). Fixed in `dedfb27`.
3. Released `dedfb27` (engine `00223-vas`, chat `00116-xav`). Re-check in Chrome: the four
   decomposition datasets fresh 11/11 (Paris now answers Delta and Omega); existing conversations
   43/50. All 7 misses were re-asks answered from memory. Six were short questions the catalog does not
   match, and in one the model ignored the correction note. Fixed in `9180169`: a word-for-word repeat
   answered with a number also counts, and the correction round forces the query call
   (`tool_choice`, thinking off for that one round). Released as chat `00118-sok`.
4. The re-check also found that a conversation past 12 turns rejected every message with "history is
   too long", because the browser sends the whole transcript. Fixed in `1be4c6e`: the chat request
   keeps the most recent window (24 messages, 80,000 characters). Released as chat `00120-huq`.
5. Final targeted re-check on the live release: 13/13 re-asked turns correct, every one with an
   engine call (termination 3, assets 2, customer-orders 2, procurement 2, and the 17-turn ecommerce
   conversation 4).

**Gates run this evening:**
- `test_decomposition` 12/12
- `test_complex_datasets` 7/7
- `test_deterministic_emitters` 46/46
- `test_compose` 14/14
- `test_orchestrator_unit` 19/19
- `test_request_limits` 16/16
- live `test_orchestrator` 25/25 (an earlier run was 23/24: the [1f] cutoff follow-up once changed
  both cutoffs; that case measured 12/12 in isolation)
- live `test_datasets` PASS: 24 prompts and 40 standalone follow-ups; the 13 `chat:` follow-ups are
  skipped by design and covered by Chrome
- `compileall` OK
- Cloud Build offline regression 13/13
- release smoke OK

Not run after these commits: `tests.run_all` as one sweep, and Spider. The new code is decomposition,
orchestrator and request validation, none of which is on the Spider path, and `select_query` is
unchanged since `841f08c`.

**Open**
- `spider/results/full_eval_served_grounding_whole_db.json` is untracked, waiting for the owner's
  approval; RESULTS.md already cites it.
- One user's requests run one at a time behind a per-user advisory lock. In the re-check, three
  compound questions fired at once in three tabs queued behind each other, and two exceeded the chat's
  180 s engine timeout; one at a time they were correct. A user who runs parallel compound questions
  would see the same.
- The gates left many conversations in the owner's account, including the junk `c_f9de7d7c…` from an
  earlier pass. None were deleted.

## 2026-09-24 — FIXED + RELEASED: the Schema.org interpreter loads in production again

**Production now:** engine `prereasoner-api-00217-zom` = `engine@sha256:6d56ab80…` built from `c228cfb`
(8 vCPU / 16 GiB, unchanged). The jobs `prereasoner-api-ecb-rates-refresh`,
`prereasoner-api-retention-cleanup` and `prereasoner-api-release-smoke` run the same image. No tags.
The chat service is unchanged (`prereasoner-chat-00111-wuc`): its image carries none of the changed modules.
Rollback: `gcloud run services update-traffic prereasoner-api --to-revisions prereasoner-api-00215-xey=100`
(and point the three jobs back at `engine@sha256:8dbf68ad…`). That restores the silent fallback.

**Root cause.** From at least 2026-09-14, every container logged `schema interpreter unavailable:
ValueError` then `model routing failed: ValueError` (both seen on `00215-xey`), so learned column routing and
table class evidence were off. The Schema.org head's recorded encoder identity hashed every file in
`engine/data/qwen_lora`, including a stale local PEFT `README.md` that the manifest never pins; no image
could reproduce it. The serving loader swallowed the error, printing only the exception type.

**Fix (`c228cfb`, details in `DECISIONS.md`).** Adapter identity = its model files
(`engine/artifact_provenance.py:adapter_sha256`), used by every adapter-identity site. The promoted head's
identity was re-recorded through `training/schema_org/promote.py` (`c3f61d5e…` -> `ea5bdbf0…`); head,
thresholds and signatures are byte-identical. `KnowledgeQuery` loads the interpreter at construction and
the in-image gate (`run_bundle_checks`) loads it at build, so a bundle it cannot load fails the build or the
startup probe instead of degrading silently.

**Evidence**
- Red/green in an image-like worktree (manifest files only): old code raised the production ValueError; new
  code + old artifacts failed the new pairing test and the bundle gate; new code + re-recorded artifacts passed.
- Cloud Build in-image gate: `schema_interpreter_loads` ok (head `4bb30c5612ee`), offline 13/13.
- `tests.run_all` 39/39 suites (live Postgres + Anthropic). `test_datasets`: 24/24 prompts and 40 non-chat
  follow-ups correct (incl. neartail-catering 9,600 with the learned router back on); the 13 `chat:`
  follow-ups were SKIPPED (Chrome-only). `test_geo` 61/61, `test_route_wired` now captures class evidence.
- `regress.run_regression --require-world` PASS (total amount in France = 270).
- New revision: boot 101 s to ready (old 112 s), no interpreter/routing errors, release smoke ok, healthz ok.

**Chrome pass (2026-09-24, owner's signed-in Chrome, 00217-zom).** All 24 datasets, every
`prompt.txt` + `eval.txt` case graded by `regress.browser_gold` / `tests.test_datasets.grade_answer`:
fresh conversations 71/74, existing conversations (created 09-23 on older revisions) 46/50 follow-ups.
None of the 7 misses comes from this release:
- Engine, pre-existing: "how about customers from Lyon?" (complex-unsold-products). The orchestrator's
  leaf read "... Lyon customers" and the planner bound 'Lyon' to `customer_name`, a column that never
  holds it (`WHERE purchases__customer_name = 'Lyon'`). Reproduced hermetically with the planner alone
  (no router): "customers from/in Lyon" is right, "Lyon customers" is wrong. Value grounding fix pending.
- Orchestrator rewording/context carry-over (the engine answers the verbatim questions correctly, router
  on and off identical): "How many payments are listed?" sent as "... payments to suppliers ..." (3, not
  30) and "What is the highest amount paid?" as "... to suppliers?" (two rows); in existing conversations
  the Phoenix scope (2), the USD presentation for "total budget in Germany" (37,656.3 for 33,000), and
  "only use the top 2 customers" read as top 2 categories too.
- Existing formfacade-leads: "This is in euros" -> "dataset operation names a table that is not
  uploaded: 'budget'" (passed there on 09-23). Not yet diagnosed.
Watch the `[timing]` lines on world questions: learned routing adds one encoder pass per short-text
column, cached per table.

## 2026-09-23 (night) — RELEASED: the 645 planner serves production; the gates found four defects

**Production now** (project `prereasoner-inference`, us-central1; one revision per service, no tags):
- engine `prereasoner-api-00215-xey` = `engine@sha256:8dbf68ad…` built from `e993476`,
  8 vCPU / 16 GiB, min 1 / max 3, startup probe 60 x 10 s (Cloud Run's maximum budget).
- chat `prereasoner-chat-00111-wuc` = `chat@sha256:56d0e305…` built from `959b40d`.
- jobs `prereasoner-api-ecb-rates-refresh`, `prereasoner-api-retention-cleanup`,
  `prereasoner-api-release-smoke` on the `e993476` engine image. Hosting was already at HEAD.
- Rollback to the pre-release pair (each revision keeps its own 4 vCPU/8 GiB config):
  `gcloud run services update-traffic prereasoner-api --to-revisions prereasoner-api-00107-ck8=100`
  and `... prereasoner-chat --to-revisions prereasoner-chat-00058-4l7=100`.

**Spider, served path:** 645/1,034 (62.4%) strict, 693 lenient, 304/408 scalar — identical SQL to the
candidate on 1,034/1,034 (clean commit `6c39942`; `select_query` unchanged since, no dev question has a
currency intent). Summary JSON `spider/results/full_eval_served_whole_db.json` is written but NOT
committed (CLAUDE.md: generated benchmark output needs the user's approval).

**What happened, in order**
1. `d993e91` was flipped while the live demo gate was still running; the gate then failed
   neartail-catering (the proposer read a world question as an INTERSECT over invented values and
   `compound_candidate` asked for a decomposition). Rolled back. Fixed in `896e081`: compound = the
   search's reading only; the compose probe runs only the search (~40 s saved per composed world
   question); named requests serve the best single query. Full live gate PASS, then flipped.
2. The stale `rc-c206f8d` tag had kept a warm 4 vCPU engine instance since 09-14 (tagged revisions keep
   min instances); all tags removed. Autoscaled instances failed the 5-minute startup probe on old and
   new revisions alike; raised to 600 s (`f234ae0`).
3. Chrome gate (Claude in Chrome on the owner's signed-in session; 23 conversations opened on the OLD
   revision first, then continued on the new one) found:
   - `72cc357` orchestrator: an empty-question tool call surfaced "question is required" as the reply.
   - `5ade6c3` WRONG answer on complex-category-gaps: the leaf contract accepted any aggregate, so a
     product-name ordering was served for "top 2 category names by revenue" (and a units-for-spend
     customer ranking went unnoticed). Leaves now sum and rank by the measure the question names.
   - `959b40d` orchestrator: "This is in euros" failed when the model wrote the table as "Budget";
     the column-as-table repair now accepts a case-only difference.
4. `tests.run_all` (all 32 suites incl. live Anthropic) PASS; the live engine suites found the fourth:
   `e993476` "total order amount in KWD" served an empty table instead of declining (a beam dropped
   the SUM and the currency spec read any aggregate-less query as a filter). Live test_geo 61/61.
5. Final Chrome evidence: existing conversations 48/48 follow-ups correct (5 after one retry: 4 complex
   turns timed out under three concurrent complex sessions, 1 Sonnet rewrite); fresh non-complex 61/63
   first pass, both misses 8/8 on re-run; complex on the fixed engine 11/11; formfacade-leads 5/5 on the
   final pair. Old revision baseline: 22/24 prompts. 55 gate conversations remain in the owner's account.
6. Production latency (147 gate turns): one engine call median 13.3 s (p90 35.3 s); decompositions
   median 60.2 s (p90 140.4 s).

**Open / for the user**
- Capacity: one request at a time per engine instance; concurrent complex questions can exceed the
  240 s turn budget. GPU or a quantized runtime is the lever (user decision; cost vs latency).
- Pre-existing: "schema interpreter unavailable: ValueError" in every container since 09-14 (task chip).
- Approve (or not) committing `full_eval_served_whole_db.json`; delete or keep the 55 gate conversations.

## 2026-09-23 (late) — Took over from Codex: the 645 candidate becomes the ONE served planner

User asked: use the 62.4% candidate, one clean source of truth, understandable docs, deploy in
production (no limited rollout, no parallel engine versions).

**Done in the tree (committed on main):**
- One selection, `engine/tables.py:TableQuery.select_query` = search (25) + d2 proposer (4 beams,
  every line imported/validated/re-rendered) + in-memory pool execution + linear arbiter. Used by
  serving, the decomposition probe and leaves, `spider/probe/full_eval.py` (no injection flags now),
  `regress/run_regression.py`, and `training/rank/build_pool_labels.py`.
- Arbiter = 9 named features (`engine/sql_rank.py:ARBITER_FEATURES`); the pilot's 6 constant
  zero-coefficient d4/missing slots dropped with bit-identical scores. `fit_arbiter.py` refits the pilot
  pools to identical means/scales/intercept, coef within 9e-15. Replay on pilot-val = 224/416 (= pilot).
- Parity: production `select_query` picks the recorded 645-run SQL on 21/21 sampled dev questions.
- Runtime bundle: `engine/data/sql_proposer/` (adapter, gitignored, fetched) + `sql_arbiter.json`
  (committed); `training/rank/promote.py` is the one writer. Manifest is local-only until the HF upload.
- Product finding + fix: the Spider-fit arbiter swapped compound prompts to single proposer queries and
  picked names-only ranking leaves. Now: named requests decompose when the search reads a set operation
  OR the answer is one; leaves take the best-ranked candidate meeting the leaf contract. Complex
  datasets 4/4 pass with real models. Spider eval has no analysis context, so unaffected.
- Retired: RankHead + hook, execution_checks, proposer_first, pilot_selectors, train_head,
  build_values/value prompts. Failed semantic pilot code moved OUT of the repo to
  `C:/work/prereasoner-experiments/semantic_pilot` (its 3 lease tests ported to tests/test_release.py).
- CI was red since 09-21 (dependency-lock identity + a stale hosting assertion + an unused import);
  all fixed. sqlglot added to serving + CI locks (only change in those locks).

**Open:** fresh full whole_db run through the production path; live test_datasets rerun on final code;
HF upload + manifest pin; image build; latency on Cloud Run (CPU vs GPU is the user's call — local CPU
median 17.5s/question for selection alone); deploy + Chrome dataset sweep.

## 2026-09-23 — CURRENT STATE / HANDOFF (relabel blocked on shard0; interim number in)

Factual status only. Nothing running; `pods=0` verified; nothing promoted; deterministic
serving path untouched.

**Standing serving config: 645/1,034 (62.4%) strict whole_db**, `--selection arbiter` +
`arbiter_d2.json` over d2-beam pools. **NEW: its gold_tables sanity = 689/1,034 (66.6%)**
(vs deterministic 437, proposer-policy d2 656) — best gold number of the program; NOT yet
written to RESULTS.md — please record it.

**Relabel fleet status:**
- shard1 DONE + downloaded: `relabel/shard1_d2beam.jsonl` (1600), `shard1_d4greedy.jsonl` (1600)
- shard2 DONE + downloaded: `shard2_d2beam.jsonl` (1597), `shard2_d4greedy.jsonl` (1597)
- shard0 NOT DONE: labeled ~1.5h then its pod dropped SSH; partial output was on the pod and
  is LOST. Relaunch then failed HTTP 500 "no instances available" (RunPod capacity, no cost,
  no pod). shard0 must be rebuilt: ~40 dbs (its list + tracking_grants_for_research, inn_1;
  staged at C:/tmp/shard0_dbs). Options: pod when capacity frees, OR local CPU (~6h for the
  d2beam pass the serving refit needs; d4greedy pass only feeds the offline-mixed analysis).

**Interim serving-faithful refit (shards 1+2+pilot d2beam, missing shard0), diagnostic-only:**
S2 held-out val = **223/416 (53.6%)** vs the pilot's 224 (d2-only) and 237 (mixed). Reading:
more d2 training data did NOT move held-out selection — the d2-only arbiter is at its data
ceiling. The relabel's remaining value is the MIXED/coverage tracks, not more d2 labels.
Artifacts: `relabel/arbiter_interim.json`, `refit_interim_report.json`.

**To finish (one path to the next serving number):**
1. Rebuild shard0 d2beam (pod or local) → 3 complete d2beam sources.
2. `scratchpad/run_refit.sh` → `arbiter_full_d2only.json` + `refit_d2only_report.json`
   (serving-faithful) and `refit_mixed_offline_report.json` (offline-only, NOT servable).
3. Serving-faithful dev run: `full_eval --selection arbiter --proposer .../d2 --proposer-beams 4
   --arbiter relabel/arbiter_full_d2only.json --tag arbiter_full_d2only`.
4. Record vs 645; gold sanity pair; RESULTS.md + memory.

**All 3 Codex gates resolved (details in the dated section below):** d2-only refit (concern 1);
full 16-db holdout re-reserved, 124 fit dbs, `relabel/full_split.json` (concern 2);
`relabel/provenance.json` = fleet code c471e29 + frozen hashes (concern 3).

**Note:** this log stays factual (status/results/decisions); no reasoning/transcript extraction.

## 2026-09-23 — Resolved Codex's 3 refit gates before spending an eval cycle

Read chatgpt-handoff.md. All three concerns confirmed and fixed BEFORE the merge/dev-run:

**1. Mixed-arbiter serving mismatch → refit is d2-ONLY.** `arbiter_select` provides only
d2beam features; a mixed (d2+d4) arbiter would see real d4 features in training but absent
ones at serving. Fix: the serving-faithful refit uses d2beam pools only — the exact same
2-source `vector()` + serving path the current 645 arbiter already uses, just more training
DBs. The mixed d2+d4 arbiter is computed OFFLINE-ONLY (labeled non-serving) to measure
whether d4 adds selectable coverage; it gets NO dev run unless/until dual-source serving is
built and parity-proven (two proposers per question — latency cost real, deferred).

**2. Split consumed reserved DBs → full holdout bucket re-reserved.** full_split.json now
excludes the ENTIRE md5 `%10==0` bucket (16 dbs incl the 5 pilot-val), not just the 5.
Fit = 124 dbs / 6,262 ex. The 11 extra (architecture, cinema, flight_company, … wrestler)
are back out of fitting. Pilot-val stays a development check (consulted in the audit), not
an untouched set; official Spider TEST stays undownloaded as the final holdout.

**3. Shard provenance null → provenance.json sidecar.** Pods report source_commit=null (no
git in pod). Authoritative record written: fleet code commit **c471e29** (tarball via
`git archive HEAD`, hash 66f5fc19…), frozen adapter hashes d2=3d73c5b5 d4=df360010,
engine_data 3f0868ac, base Qwen2.5-0.5B @060db649. (Refit artifacts are gitignored
experiment data; provenance sits alongside them on disk.)

**HONEST TARGET NOTE:** "72.6%" is the pilot-val POOL CEILING (oracle) — the max any
selector reaches on that 416-question set, not a serving number. Serving is 645/1,034
(62.4%) on dev; dev pool ceiling w/ beams is 79.8%. The refit can push serving toward the
ceiling, not to 72.6% literally. 80% still needs COVERAGE work (Codex's standing point):
the pilot-val pool tops out at 72.6% regardless of selector quality.

**Fleet:** shard1 done (1600+1600). shard2 on d4 pass, shard0 on d2 pass, 2 pods, no
orphans. On completion: run scratchpad/run_refit.sh (d2-only serving refit + offline mixed),
then serving-faithful dev run with arbiter_full_d2only.json.

## 2026-09-22 (cont. 2) — GPU/CPU equivalence PASSED; fleet relaunched clean

**CPU/CUDA equivalence result (your point) — strongest form, PASSED:** GPU benchmark shard
vs a CPU relabel of the same 2 DBs (152 examples, 3,302 shared candidates):
- per-candidate scored_logprob: max |GPU−CPU| = **0.0004**, 100% within 0.1
- arbiter SELECTED SQL per question: **152/152 (100%)**
- candidate SET identical: **152/152 (100%)**
The GPU fleet produces the same pools AND the same selections a CPU relabel would — fleet
data is trustworthy. (You were right that fp32-likelihood equality ≠ selection equality;
this checks selection directly.)

**Fleet operations note:** the first 3-pod fleet (PowerShell Start-Process) terminated
cleanly with no downloads — I never got usable logs (redirect dir race). Relaunched as three
MONITORED background bash leases, each with its own `PREREASONER_RUNPOD_STATE` file so the
ownership-token reconcile can't collide across concurrent leases. Hit + fixed the MSYS
path-mangling bug (`MSYS_NO_PATHCONV=1` — Git Bash was rewriting remote `/root/...` args).
All three pods now RUNNING, labeling, downloads land in
`training/rank/data/experiments/relabel/shard{0,1,2}_{d2beam,d4greedy}.jsonl`. ~2.5–3h.
No orphan pods at any point (`pods=0` verified between every attempt).

## 2026-09-22 (cont.) — Acted on Codex coverage audit + CPU/CUDA point

**Read the coverage audit.** Headline confirmed: 93/114 validation misses already have a
representable gold + a pool member with all gold tables → predicate/projection/aggregation/
composition, NOT table retrieval. Also noted: consulting these val examples means they are
no longer an untouched confirmation set — I need a SEPARATELY frozen eval for final claims
(agreed; official Spider TEST stays undownloaded for that).

**Shipped (my track), committed 7852182:** importer now maps numeric row/order arithmetic
(`max_f - min_f`) into the existing `BinaryExpr` node — scoped from your fitting examples
201/6101, fitting-side only. Import coverage 90.0 → 91.3% on the 2k sample. Non-numeric
(date) arithmetic stays REFUSED by the validator — no validator weakening, per your warning.
Contrastive test covers accept + refuse. This helps future proposer targets AND lets
arithmetic proposals through at serving.

**Remaining importer families from your audit (not yet done, ranked):** self-join projection
(4234), non-equality self-join (2486), scalar comparison expressions. Each is a separate
bounded importer extension with contrastive tests; I'll take them in order after the fleet.

**CPU/CUDA equivalence (your point — selected candidates, not just fp32 likelihoods):**
harness built; comparing the GPU benchmark shard vs a CPU relabel of the same 2 DBs on both
(a) per-candidate scored_logprob and (b) the arbiter's SELECTED SQL per question. CPU relabel
still running (slow 4-beam CPU scoring); result posted when it completes. Agreed fp32 alone
is insufficient — selected-candidate agreement is the real test.

**Your semantic pilot:** data foundation (14,015 pairs, 416 val held out) acknowledged. The
model TRAINING run needs its own experiment contract (backbone/budget/gate) and user approval
— NOT covered by the relabel cap. Flagged to the user. The prepared pairs can be used the
moment that's approved.

## 2026-09-22 — Parallel relabel fleet live; handoff log created

**Standing serving number: 645/1,034 (62.4%) strict, whole_db**, `--selection arbiter`
over d2-beam pools with the pure-linear pilot artifact `arbiter_d2.json`. Deterministic
production path unchanged (365). Nothing promoted.

**Relabel fleet (my track):** 3 RunPod pods running, ~1,600 train examples each, both
sources per pod (d2-beams-scored + d4-values-scored). ~3.5h projected, ≈$6 of the $25
cap. Per-worker lease-state files + ownership-token pod names; all three API-verified
RUNNING. Downloads to `training/rank/data/experiments/relabel/shard{0,1,2}_{d2beam,d4greedy}.jsonl`.
Shard throughput measured on GPU: 3.6s/example (2.9 predict + 0.7 score) — 6× the CPU rate.

**On fleet completion:** merge shards + pilot pools → full-train per-source completion
gate → refit mixed arbiter on all non-held-out DBs → re-validate held-out → serving-faithful
dev run with the refit. That is the next serving number.

**Local:** arbiter gold-sanity run in progress (watcher attached).

**Read from Codex this cycle:** coverage audit (114 uncovered breakdown), 14,015 contrastive
pairs prepared, semantic-pilot data foundation ready. Acting on: (1) coverage report before
choosing planner repairs; (2) CPU/CUDA equivalence must compare SELECTED CANDIDATES, not just
fp32 likelihoods — adding that check now.

**Open decisions for the user (not yet approved):**
- Semantic-scorer training run needs its own experiment contract (backbone, budget, gate) —
  the relabel cap does NOT cover it.
- Full-relabel merge → arbiter refit is funded and proceeds automatically.
- Coverage planner repairs: I choose specific fixes after reading Codex's audit; engine
  changes are mine to integrate, developed on fitting-side examples only.

**Contract reminders in force:** validation DBs (5 pilot + the broader held-out set) excluded
from all fitting; official Spider TEST undownloaded; dev is a consulted tuning set, labeled so.
d1/d2/d4 adapters + arbiter_d2 frozen (hashes recorded). Codex owns
`training/rank/semantic_pilot/**` and `training/rank/data/experiments/coverage/**`; I own
`engine/**`, `spider/probe/**`, `training/proposer/**`, `training/tools/**`, the rank builder/
trainer/selectors, and integration of evaluator + test-registry changes.
