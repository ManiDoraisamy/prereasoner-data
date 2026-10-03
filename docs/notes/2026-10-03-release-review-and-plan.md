# Release review and remediation plan — 2026-10-03

## Conclusion and evidence boundary

The release is blocked by both functional regressions and defects that can permit a wrong or incomplete answer. Adding more synonym aliases would not address these problems. The principal architecture change should be a shared, typed completeness check between question interpretation and execution, followed by faithful result transport and presentation.

This is a review of source HEAD `877eb36`, which was clean when reviewed. The latest complete candidate-image product run was Cloud Build `656fe09f-7724-4b58-94b7-f6c5c5011a90`, built from `ee7ef74`; it failed four suites. Subsequent `877eb36` changes address numeric-threshold coverage and sheet-context words, but have not passed another full image/product run. The known local planner/offline passes do not supersede that failure.

Review scope: request validation and ingestion; schema and source-value binding; candidate construction, execution and selection; calendar, rate and share calculations; own-data/world/composition/decomposition routes; SQL/Python execution and result envelopes; chat rendering; conversation persistence, quotas and retries; browser/Sheets flows; and build/deployment gates. This report enumerates the findings from that scope. It is not a claim that every possible input or latent defect has been discovered.

Evidence used: the complete failed build log; current source and fixtures; current production service metadata; and focused, credential-free probes calling the production selection/coverage/presentation functions with small independent fixtures. Those probes do not substitute for a real-model PostgreSQL or authenticated browser run. No production configuration or application code was changed during this review.

Verified production state during this review:

- API revision: `prereasoner-api-00138-mbg`, engine image digest `8c1927e0729d4dfa85ca55b9a06fba2cce1a3fd2b0e439c85bcd613dab470459`.
- Chat revision: `prereasoner-chat-00092-pr2`, chat image digest `d69d4a9d90180a6e632a8526dd37b5514e35090a0fd063ec801642cf11e70010`.
- External Gemini enabled; conversation cap 500; execution policy `auto`; Python row threshold 10,000.
- These are older services, not the failed candidate build. Recent source changes must not be described as fixes already verified in production.

Priority: **P0** means a wrong answer or a lost user constraint can pass; **P1** means a launch-blocking functionality, completeness, recovery or release-evidence problem; **P2** means a bounded improvement or policy clarification. Every item below has a preferred implementation, an alternative, and an acceptance condition.

## Complete observed failure inventory

The build reported **19 failing dataset checks covering 17 distinct dataset/question pairs**, because two opening prompts are repeated as follow-ups. Four other failed assertions bring the total to **23 reported failures across four suites**. These are test assertions, not 23 distinct root causes.

| Dataset/suite | Question | Expected | Observed / current status |
|---|---|---|---|
| `test_world` | cities with total sales over 100 | Osaka, total 200 (the assertion also accepts Osaka-only output) | Empty result. Structural threshold coverage changed in `877eb36`; full run pending. |
| `test_geo` | How many orders are in the current sheet? | 4 | No result. Sheet-context coverage changed in `877eb36`; full run pending. |
| `test_geo` | Count all non-empty Order ID rows below the header in the Customers sheet | 4 | No result. Sheet-context coverage changed in `877eb36`; full run pending. |
| `test_nongeo` | total transfers for hospitals named Mayo Clinic | 14 | Clarification falsely reporting `mayo` dropped. |
| `eval-formesign-assets-xls` | How many assets are listed? | 3 | Clarification. The opening cost-total prompt passed; it must not be reported as a failure. |
| `eval-formesign-procurement-xlsx` | How many purchase orders are listed? | 1,168 | Clarification, opening prompt and repeated follow-up. |
| `eval-formesign-termination-xlsx` | maximum notice_days | 180 | No executable candidate reported. |
| `eval-formesign-termination-xlsx` | minimum notice_days | 5 | No executable candidate reported. |
| `eval-neartail-supplier-report-xlsx` | What is the total amount paid to suppliers? | 2,128,324.96 | Clarification, opening prompt and repeated follow-up. |
| `eval-neartail-supplier-report-xlsx` | How many payments are listed? | 30 | Clarification. |
| `eval-neartail-supplier-report-xlsx` | What is the highest amount paid? | 225,766.34 | No executable candidate reported. |
| `eval-neartail-supplier-report-xlsx` | What is the total amount paid to PHOENIX SOFTWARE LTD? | 445,494.54 | Clarification. |
| `formesign-contracts` | How many contracts were signed before July 10, 2026? | 4 | Clarification. |
| `formesign-hospital-transfers` | total transfers signed in August | 61 | Clarification. |
| `formfacade-leads` | How many leads were submitted after August 10, 2026? | 5 | Clarification. |
| `formfacade-leads` | How many leads were submitted between August 4 and August 9, 2026? | 4 | Clarification. |
| `formfacade-leads` | What is the total budget from August 11 to August 15, 2026? | 37,000 | Clarification. |
| `neartail-orders` | What share of the total amount comes from Paris? | 0.6232 | Clarification. |
| `neartail-orders` | What percentage of orders are from Lyon? | 0.3, i.e. 30% | Clarification. |
| `payment-commissions` | total amount after subtracting commission | 1,082.41 | Selected route supplied no typed calculation evidence. |
| `payment-commissions` | total amount after subtracting commission for digital wallet payments | 226.09 | Same missing-evidence refusal. |

Two important corrections to interpreting the log:

- `total budget in Africa` correctly requested clarification because no rows matched; it is not a failed case.
- The `orders-tiers` result `983.87125` versus historical FX gold `~1018.08` passed the existing tolerance. Its discount-to-undiscounted ratio matches the historical fixture ratio: the numerical difference alone is not evidence of a discount bug. A `min_order_amount` field needs a documented business policy before treating it as a per-order eligibility predicate; it may describe how a customer earns a tier. Do not invent that policy from the field name.

## Findings and detailed fix plans

### 1. P1 — Different coverage gates disagree and reject valid plans

**Evidence.** `engine/tables.py:882` adds a vocabulary check after `_choose`; `engine/knowledge_query.py:723` separately checks words against rendered SQL and geography. Calendar and calculation verifiers already recognize typed AST evidence, but the new vocabulary check does not consistently consume it. Current probes produced correct `MAX(notice_days)`, `MIN(notice_days)`, an August filter, a July date comparison, exact `hospital = 'Mayo Clinic'`, a commission-subtraction expression, and a share with an unfiltered denominator. With an attached but disabled rewriter, selection discarded each. Thus several reported “no candidate” errors are actually “candidate rejected by coverage.”

**Plan A.** Introduce one request-interpretation/coverage record shared by own-data, world, composition and decomposition. Record each meaningful source span and its bound role: table/entity, measure, aggregate, output grain, predicate operator/value, date interval, ranking, calculation and unit. The selected AST must provide corresponding evidence. Calendar coverage consumes the complete date span after verifying its operator, boundary and column; extrema consume their aggregate cue; rate/share coverage consumes the complete verified calculation span. Exact cell values remain locally bound. Unknown meaning invokes the schema-only rewrite; unresolved meaning produces a precise clarification.

**Plan B.** First reuse the existing typed date/calculation/expression verifiers in both gates behind a common adapter, then migrate the remaining roles incrementally. Keep the old gate only for residual unknown spans; do not fix this by indefinitely growing an ignore-word list.

**Acceptance.** Re-run all 23 failures against the exact image in SQL, Python, verification and default modes. Add contrasts for a dropped date, a wrong date column, a real `listed`/`paid` status, a different named hospital and a calculation whose direction is reversed. A correct candidate survives without requiring Gemini; an unsatisfied constraint is not merely a ranking preference.

### 2. P0 — Comparison direction and exclusion are not proved by word coverage

**Evidence.** A current probe passed `total Amount greater than 100` with SQL filtering `Amount < 100` into `_query_has_unread_terms`; it returned `False`. Words such as `greater`, `less`, `before`, `after`, `not` and `excluding` are broadly exempt, and seeing a number somewhere in SQL does not prove its operator, operand or predicate scope. `877eb36` structurally checks `over`/`under`, but this does not cover all operator forms. `select_ranked_candidate` also falls back to undated candidates when no dated candidate qualifies.

**Plan A.** Make every recognized comparison, negation, exclusion, date interval, ranking cutoff and output-grain requirement mandatory against the AST. Match operand, direction, literal, scope and Boolean structure. Distinguish row filters from HAVING; a matching number in LIMIT or another column cannot satisfy a predicate. Preserve AND/OR semantics and subquery/set-branch scope.

**Plan B.** For shapes the validator cannot inspect, return a bounded unsupported/ambiguous outcome instead of serving a weaker plan.

**Acceptance.** Paired cases for `> / <`, `>= / <=`, `not / only`, included/excluded value, AND/OR, WHERE/HAVING and filtered/unfiltered denominator. Use fixtures where the alternatives yield different answers. Every wrong-direction plan is rejected.

### 3. P0 — Non-Latin text can disappear from the completeness check

**Evidence.** The coverage tokenizer uses `[A-Za-z0-9]+`. A probe supplied `total Amount for 東京` with an unfiltered SUM; the gate returned `False` for unread meaning. This is a loss of a filter, not a synonym miss. A multilingual question that yields some runnable candidate is especially exposed; existing Russian tests mainly cover unavailable/unsearchable readings.

**Plan A.** Preserve Unicode source spans, normalize consistently without deleting original text, and account for all meaningful text. If the local interpreter cannot account for a language/span, invoke schema-only normalization before accepting a candidate. Source values are bound locally with Unicode-aware exact matching. Preserve the original literal alongside any translation.

**Plan B.** Require clarification for unsupported mixed-language or non-Latin spans until the rewriter returns a validated, complete interpretation.

**Acceptance.** English, French, Spanish, Japanese, Chinese and mixed-language fixtures with positive/negative filters, accents, names, numbers and dates. Test the case where the original question already produces a runnable but incomplete candidate, not just a no-candidate case.

### 4. P0 — Rewrite validation can erase negation while preserving literal strings

**Evidence.** `_preserves_explicit_constraints` in `engine/sql_fallback.py` checks numbers, quoted phrases and locally found values. The current probe returned `True` for rewriting `total Amount not in Paris` to `total Amount in Paris`. It does not establish operator, aggregation, grouping, output currency or scope fidelity.

**Plan A.** Validate the original and rewritten interpretation records from item 1. Preserve recognized constraints and their roles, including polarity, comparator, top-N, aggregate, grouping, date boundary and unit. A changed schema label is allowed; a changed computation is rejected. Surface original and accepted canonical wording in reasoning. Gemini continues to receive schema metadata plus the user's question, and produces wording rather than executable SQL.

**Plan B.** Require user confirmation when a rewrite changes a known operator or when equivalence cannot be established. A number/value substring check remains an additional check, not the semantic certificate.

**Acceptance.** Rewrites dropping `not`, changing `sum` to `average`, switching a country filter into a currency filter, dropping grouping, swapping date directions or changing LIMIT fail. Legitimate schema-label substitutions pass.

### 5. P0 — An incomplete rewrite can be accepted when the original has no candidate

**Evidence.** `engine/tables.py:567` computes `rewritten_is_complete`, but rejects incompleteness only under a condition requiring an original selected candidate. A probe with an empty original pool accepted Gemini's `total Amount for nonexistentmeasure` as `gemini-rewrite`, serving an unfiltered SUM, while the same completeness function reported the rewritten question unread.

**Plan A.** Require every rewritten candidate to pass completeness regardless of original-pool state. Make acceptance one shared predicate used by all fallback paths and decomposition leaves.

**Plan B.** Refuse the rewrite with a structured unresolved-span explanation; do not reinstate an unread original candidate.

**Acceptance.** Test original pool empty/nonempty, original candidate complete/incomplete, rewritten pool empty/nonempty, and rewritten candidate complete/incomplete. Only a complete, faithful rewrite can supply an answer.

### 6. P0 — Coverage exceptions currently allow an answer through

**Evidence.** `engine/knowledge_query.py:1084` catches coverage exceptions and sets `dropped = []`, which makes inability to verify coverage behave like verified completeness.

**Plan A.** Fail safely with a structured verification-unavailable result, retaining diagnostic trace and a retry indication where appropriate. Split optional lookup failures from failures of the actual correctness check. Do not use a broad exception handler to certify an answer.

**Plan B.** Use a narrower validator that can certify the query without external lookups; otherwise request clarification.

**Acceptance.** Inject failures in encoding, world lookups and coverage construction. They must not produce an unverified numeric answer, leak raw data in logs or be described as a successful query.

### 7. P1 — Calculation and share evidence is inconsistent across routes

**Evidence.** The seeded commission cases refuse with no typed calculation evidence. A current own-data probe constructs the correct joined subtraction and marks it satisfied, then the later vocabulary gate discards it. A share probe also builds the correct filtered numerator/unfiltered denominator, but registered calculation detection does not classify that share. Therefore adding a second arithmetic implementation is not the first fix.

**Plan A.** Repair item 1 first; then replay the real-model full route to locate any remaining route that loses computation evidence. Every terminal route must publish the same typed evidence for amount, rate, join multiplicity, aggregate, output unit and grain. Certify share numerator and denominator scopes explicitly, including percentage display scale and zero-denominator handling. Preserve existing SQL construction and shared emitters.

**Plan B.** Route supported own-data calculation shapes through the existing typed planner as the authoritative terminal. If a composition route cannot produce the required evidence, abstain with a specific unsupported calculation message.

**Acceptance.** Independent Decimal gold for all payment rows (1,082.41) and digital-wallet rows (226.09); 0.6232 and 0.3 share gold; filtered denominator contrasts, grouped shares, duplicate lookup keys, missing rates and zero denominator. SQL/Python parity supplements these gold checks; it does not replace them.

### 8. P1 — Several final-result paths silently truncate to 50 rows

**Evidence.** Final `result.rows` uses `rows[:50]` in `engine/knowledge_tables.py:1026`, `engine/knowledge_query.py:662`, and `engine/knowledge_compose.py:445` and `:714`. These include deterministic world/composed/decomposed responses. A 50-row reasoning preview is appropriate, but truncating the authoritative result without pagination/count/truncation metadata loses answers. The persisted analysis also has a 1 MiB cap (`engine/conversations.py:489`), so merely removing slices can turn this into a persistence failure.

**Plan A.** Separate the complete result artifact from trace previews and lightweight saved UI state. Provide exact row count, bounded pages and a stable version-bound result/export handle. Preserve all result rows for every route; only previews are capped. Save the immutable plan, source fingerprint and artifact reference, with bounded preview rows.

**Plan B.** For a bounded first release, return an explicit result-size limit with a usable download/pagination path. Never silently claim the first 50 rows are the whole result.

**Acceptance.** Source, world and decomposition outputs of 49, 50, 51, 1,000 and 30,000 rows; compare all rows with independent gold, then reload/export. Exercise responses larger than 1 MiB and preserve order, precision, nulls and source-version consistency.

### 9. P0/P1 — Chat can present incorrect numbers despite a correct engine answer

**Evidence.** Current `_grounded_presentation` probes accepted all three: result `-120` with prose `Your profit is 120.`; scalar `120` with prose adding `999 orders`; and a two-row table with prose `Your total is 999999.`. `_stating_number` compares absolute magnitudes; scalar validation only needs one matching number; nonscalar prose bypasses numeric grounding. Raw presentation deltas are also streamed before final validation.

**Plan A.** Render factual values, signs, units, row counts and table summaries from engine-owned structured fields. If Gemini supplies prose, require explicit references to allowed result cells/verified metadata and validate every factual claim before rendering. Stream status/verified content while awaiting validation. Scalar, negative, zero, table and clarification replies share this rule.

**Plan B.** Use deterministic answer templates and tables for this release. Gemini remains the schema/wording assistant; arithmetic claims come from exact returned results. This is simpler than trying to certify arbitrary prose.

**Acceptance.** Negative profit, refunds, negative percentage changes, large exact decimals, stale history numbers, extra invented counts, table summaries and inconsistent units. Inspect streamed events as well as final HTTP replies: an incorrect value must not briefly appear before being replaced.

### 10. P1 — Saved analyses drop rewrite and selection attribution

**Evidence.** `_ANALYSIS_RESPONSE_FIELDS` (`engine/conversations.py:51`) omits `fallback` and `selection`; `complete_analysis` saves only that allowlist. An immediate answer can be labelled as Gemini-assisted while its saved authoritative revision lacks that explanation. A copied UI snapshot is insufficient as the durable audit source.

**Plan A.** Persist original/canonical wording, rewrite kind/model, selected-query evidence, coverage verdict and interpretation version in the immutable revision. Bind them to input/model/knowledge-release/config fingerprints. Restore the same labels from the authoritative revision.

**Plan B.** Persist a compact interpretation/selection manifest rather than a large candidate pool.

**Acceptance.** Rewrite-assisted answer → reload → open historical revision → export: wording, labels, SQL, values and provenance agree. Direct-model answers remain accurately labelled. No sensitive full-row payload is added to rewrite logs.

### 11. P1 — Conversation history and deletion hide backend failures

**Evidence.** `deleteConv` and `clearAllConvs` in `web/public/lib/workbook-conversations.js:113` and `:120` ignore HTTP status and catch network failures without feedback. They can navigate/reset the active chat even though the server did not delete it. `listConversations` at `:36` also maps HTTP/network failures to an empty list, so a service/auth failure looks like the user has no history. The trash SVG exists; correct icon appearance does not prove deletion works.

**Plan A.** Await and validate success before navigating/resetting; retain state on failure, restore controls, show an actionable error and allow retry. Prevent repeated clicks while pending. Apply the same contract to single deletion and delete-all. Distinguish an empty history from a failed history request and retain already loaded items during a failed refresh.

**Plan B.** Reconcile with a fresh ownership-scoped list after an uncertain response before changing local state.

**Acceptance.** Success, 401, 403, 429, 500, network loss and ambiguous timeout in browser fixtures; hosted deletion only on newly created disposable test conversations. Never delete the user's existing chats as a test.

### 12. P1 — Quota recovery and live configuration remain unresolved

**Evidence.** The user encountered the 500-conversation limit and a disabled New chat control. Production still has that cap. Updated source messages and Sheets state handling are not proof of hosted recovery; raising the cap alone only postpones the problem.

**Plan A.** Expose quota usage and a usable history/manage action in the relevant surface. Preserve answers when saving fails; distinguish transient save failures, expired sessions, storage exhaustion and conversation exhaustion. Ensure clear/start-new semantics are explicit and controls recover. Reuse existing conversation IDs for retries; avoid unnecessary durable conversations for unsuccessful attempts. Verify retention cleanup separately.

**Plan B.** A reviewed temporary cap increase while retaining safe recovery controls and storage/retention bounds. Never automatically delete existing chats to free space.

**Acceptance.** At-limit and just-below-limit tests, state-save refusal, reload, clearing sidebar state, opening an existing chat and deleting a disposable chat to free one slot. Hosted Chrome and real Sheets must show an actionable outcome and remain usable.

### 13. P1 — A 50,000-row upload limit does not prove a 30,000-row question works

**Evidence.** Current import tests cover admission around 50,000 rows and Apps Script has a 1,000,000-cell cap. The seeded HTTP CPU smoke uses only three orders. Inputs are represented repeatedly as grids, CSV, parsed rows, schema value arrays and a local SQLite candidate database; candidates can each execute up to 100 million VM steps. There is no demonstrated full 30,000-row Sheets → chat → planner → PostgreSQL → rendering/reload journey for the reported customer shape.

**Plan A.** Measure a representative 30,000-row subscription fixture with realistic width, strings, dates, nulls, formulas, mixed currencies and multiple tabs. Measure cold/warm first answer and follow-up, import time, planning/execution time, peak memory, result/state bytes and lock wait. Reuse canonical parsed data and schema profiles keyed by source fingerprint; avoid redundant copies and repeated work where profiling shows the cost. Give a visible progress/retry path for long requests and preserve all admitted rows.

**Plan B.** Stage larger uploads and process the verified source incrementally/server-side, with explicit limits and selected-tab scope. Set a measured supported envelope instead of only increasing the row constant.

**Acceptance.** Real deployed Chrome/Sheets runs at 30,000 rows; boundary admission at 50,000/50,001; narrow and wide cases; source-only and knowledge-join queries; exact sums/counts/filter gold and repeat/reload. Define and record latency/memory targets before accepting the performance result. The original customer workbook remains unverified unless accessible; representative fixtures must be labelled as such.

### 14. P1/P2 — Import limits and omitted-source scope need one clear contract

**Evidence.** Browser limits allow a 16 MiB workbook but Google import errors still say 8 MB (`web/public/lib/google-sheets-import.js:18,26`). HTTP body limits, per-table character limits, per-table row limits and aggregate cell limits differ by boundary. The Sheets add-on can omit extra tabs and unnamed columns with warnings; interpreting a question over a partial source needs explicit scope. Eight 50,000-row API tables have a different total workload from one such table.

**Plan A.** Define supported bytes/rows/columns/cells/table-count dimensions centrally and generate copies for lightweight runtimes. Validate expensive total workload before model work. Show the actual exceeded dimension. Carry included/omitted tabs and columns into request provenance; block a question requiring unavailable data rather than silently answer from a subset.

**Plan B.** Offer explicit tab selection and a smaller measured workload envelope; make partial import a user-visible choice.

**Acceptance.** UTF-8 versus character size, quoted newlines, wide rows, multiple tabs, hidden tabs, formatted trailing rows, missing headers and cross-tab requests. Error messages match the enforced limits and no accepted dataset is silently shortened.

### 15. P1 — Engine execution needs a whole-request deadline and cancellation

**Evidence.** `WORLD_LOCK` serializes model serving and has a 15-second queue wait, but the serving DB connection does not set request-specific `statement_timeout`/`lock_timeout`. Client/proxy/chat deadlines do not themselves cancel work inside the engine. The 25-candidate × 100-million-step eligibility bound is finite but is not a latency SLA. Shared mutable model state makes simply removing the lock unsafe.

**Plan A.** Propagate a deadline through queue admission, interpretation/rewrite, candidate checks, database upload/query and result persistence. Set transaction-local DB statement/lock budgets; cancel abandoned work and release locks in every path. Report retryable busy/timeout outcomes with the existing job ID. Optimize the measured bottleneck before increasing per-instance concurrency.

**Plan B.** A bounded job queue with durable job status and controlled workers for heavier queries. Retain the existing serialization until request state is genuinely isolated.

**Acceptance.** Slow query, lock contention, Gemini timeout, disconnect, CPU saturation and concurrent users. Each finishes or cancels within the budget, releases locks/connections, produces a terminal trace and permits the next request to run.

### 16. P1 — Replay is process-local and does not bind an ID to its payload

**Evidence.** `ResponseReplay` stores entries in a process dictionary. `engine/server.py:677` keys replay by route, verified user and job ID without a payload fingerprint. Across Cloud Run instances or restarts the same ID can run again; within one instance a reused ID with a changed question/data can retrieve an older response. Durable conversation quota locks do not provide durable turn idempotency.

**Plan A.** Store an ownership-scoped request/job record with payload and source fingerprints, lease/terminal status and immutable result reference. Reject a reused ID with different input. Deduplicate chat turns and engine work across instances; retries refer to the same job and conversation.

**Plan B.** Sticky job routing can reduce repeats temporarily but must not be presented as durable exactly-once behavior. Keep explicit input-conflict detection even with the in-memory cache.

**Acceptance.** Same ID/same body, same ID/changed body, two simultaneous instances, restart, response loss after completion and stale lease. One visible analysis/revision/result for a legitimate retry; changed input is not answered with an earlier result.

### 17. P1 — Test doubles and release environments do not consistently match production

**Evidence.** `_hermetic_planner` uses `sql_fallback=None`; production attaches a fallback whose client may be disabled. `select_query` returns a candidate unchanged when fallback is absent, but rejects unread candidates when an attached client is disabled. Current probes show identical questions pass one configuration and fail the other. The seeded build explicitly disables live orchestrator testing and has no configured external rewrite. Browser fixture tests mock auth, APIs and Apps Script; they are valuable UI tests, not proof of hosted accuracy. `chat:` cases are explicitly skipped in the direct dataset suite.

**Plan A.** Test the real serving factory and configuration matrix: no external helper, helper disabled, helper enabled with contract-shaped replies, unavailable/error replies, and separately live Gemini with real credentials. Canonical supported questions must pass with helpers off. Alternate-language/wording cases exercise schema-only rewriting. Run the complete real-model seeded route after focused fixes; require an authenticated hosted report for every public `?load=` base and ordered `chat:` follow-up.

**Plan B.** Use an attested staging deployment with the production configuration when external tests cannot run inside the isolated image build. Keep its results a separate required promotion gate; no fake reply counts as live Gemini evidence.

**Acceptance.** One report names source commit, image digest, model/data/config fingerprints, test lane, skips, per-case gold, SQL/Python/default outcomes and actual browser result. Unexpected skipped gates fail promotion.

### 18. P1 — Numerical gold can hide material business errors

**Evidence.** FX dataset grading permits 15% deviation (`tests/test_datasets.py:122,241`); that protects against changing daily rates but can also pass a wrong business calculation smaller than 15%. SQL/Python verification compares the same selected plan, so both can agree on the wrong interpretation. The older benchmark result at `60a55a3` does not measure `877eb36` and must not be relabelled as current accuracy.

**Plan A.** Pin FX date/release in regression fixtures or calculate independent Decimal gold from the exact attested rate release. Check contributing rows, currency, date policy, joins, arithmetic and output shape as well as final amount. Re-run the existing gold-blind benchmark on the final unchanged candidate, report answered-correct, answered-wrong and abstained, and inspect per-question regressions.

**Plan B.** Keep a broad FX health/drift check as an additional operational test, but add an exact fixed-release correctness test as the release authority. Defer expanding supported question families rather than representing older/triage scores as current evidence.

**Acceptance.** Deliberately wrong 5%/10% discount, wrong denominator, wrong rate date and duplicate join fail. Legitimate rate drift is attributed to a known release. Current benchmark artifacts name the exact commit/config/runtime and do not mix engine-only, rewrite-assisted, triage or oracle-table metrics.

### 19. P1 — Production promotion is not yet one verified release

**Evidence.** Engine, chat, Hosting and Apps Script are separately released. The candidate engine failed while companion/frontend work advanced; current backend revisions remain older. The engine build gates product suites by default, but independent publishing and manual Terraform image inputs can bypass a coherent release story. Hosting installs an unpinned global Firebase CLI and uses an unpinned Node image tag.

**Plan A.** Build a release manifest linking source commit, engine/chat digests, Hosting version, Apps Script version, model hashes, schema migration version, knowledge releases and configuration. Gate promotion on all required tests for that manifest. Use compatible staged rollouts, then verify both API and UI versions and preserve explicit rollback targets. Pin release tools. Expose a diagnostic build/version endpoint without secrets.

**Plan B.** An explicit reviewed promotion script validates the manifest and build statuses before each service update. Keep frontend/backend compatibility while rollout is partial; do not claim an atomic cross-service deployment where none exists.

**Acceptance.** No promotion of a failed/untested image, no skipped required lane, hosted base/follow-up/Sheets smoke against the promoted manifest, then rollback rehearsal against compatible retained revisions. Deployment does not erase customer chats or source sheets.

### 20. P2 — Business policies and determinism claims need precise boundaries

**Evidence.** Generic `rate` values in `[0,1]` can be treated as fractions by `_rate_scale`; a generic rate label is not sufficient to establish percent versus fraction. `min_order_amount` in the tier fixture has ambiguous business scope. The engine wording helper is stateless, while conversational chat still sends prior messages and computed answer data to Gemini (`orchestrator/orchestrator.py:227,819`). Current documentation should not imply every external-model call receives only schema or that a Gemini-assisted interpretation is repeatable solely from the original question.

**Plan A.** Require explicit unit/policy metadata or a user assertion for ambiguous rates and discount eligibility; retain that assertion in the analysis audit. Describe determinism relative to the accepted canonical interpretation plus pinned inputs/artifacts/releases. Document the actual external-data boundary. With deterministic presentation from item 9, reduce external model use to schema/intent normalization; resolve follow-up context from bounded explicit analysis state and validate the resulting interpretation.

**Plan B.** Clarify ambiguous policies and label the separate conversational service accurately while retaining its current behavior. Do not promise universal language comprehension or reproducible original training beyond the model card's documented provenance.

**Acceptance.** Ambiguous 0.8 rate, percent/fraction metadata, tier qualification versus per-order threshold, follow-up scope changes and model outage cases behave explicitly. Published claims and examples match the tested deployed configuration.

## Recommended implementation order

1. **Freeze promotion and capture baseline evidence.** Preserve the failed log, current source/production manifests and the independent fixtures. Turn the new incorrect-answer probes into meaningful regression tests. No cap increase or UI polish substitutes for a passed release gate.
2. **Repair semantic safety first: items 1–6.** Establish the shared interpretation/coverage record, validate every original and rewritten candidate, preserve Unicode and constraint direction, and handle verifier failures safely. This should eliminate many of the 19 functional failures while closing the newly reproduced wrong-answer cases.
3. **Re-run the real-model seeded matrix and finish calculations: item 7.** Trace any remaining failures by routing stage rather than adding another math engine. Certify rate/share scopes, lookup cardinality, null behavior, precision and units. Require independent gold and all execution modes.
4. **Repair answer delivery and audits: items 8–10.** Complete/paginate results, decouple previews and persistence, preserve rewrite provenance and use verified presentation. Add large-output/reload and streamed-value regressions.
5. **Repair everyday recovery: items 11–12 and 16.** Confirm deletion success, make quota/save errors usable, preserve IDs/state, and deduplicate retries across instances. Exercise destructive behavior only on disposable fixtures.
6. **Validate supported scale: items 13–15.** Set workload/deadline contracts, profile realistic 30,000-row cases and implement the measured improvements. Do not remove model serialization until the mutable request state has been isolated and tested.
7. **Strengthen evidence and promotion: items 17–19.** Configuration-faithful tests, exact gold, current benchmark, complete hosted Chrome/Sheets journeys and one release manifest. All public examples and ordered conversational follow-ups must have individual verdicts.
8. **Refactor and document the verified behavior: item 20 plus module cleanup.** Centralize the coverage contract, response shaping and limits. Remove redundant guards only after equivalence/contrast tests pass. Run focused checks after each change and the full release gates after the final refactor, then promote the exact tested artifacts and repeat the hosted release gate.

## Launch acceptance checklist

- All 23 failed assertions pass against the final candidate image; no test is weakened to hide a required behavior.
- Every new incorrect-answer probe is rejected or rendered faithfully, including reversed comparisons, missing Unicode filters, changed negation, incomplete rewrites and fabricated/unsigned prose.
- Full results survive paging, export and reload; preview limits never silently shorten the answer.
- Realistic 30,000-row end-to-end cases meet recorded latency/memory/size targets and exact answer gold.
- Hosted authenticated Chrome verifies all public `?load=` examples, full ordered follow-ups, source freshness, quotas/save recovery, restart/retry and disposable deletion; real Sheets verifies its distinct Apps Script/account path.
- Model-only and rewrite-assisted accuracy are measured separately on the exact final commit; parity and successful execution are not called semantic accuracy.
- Engine/chat/Hosting/Apps Script/configuration/data versions match the release manifest, with required lanes passed and rollback targets recorded.
- Residual unsupported cases have specific, recoverable outcomes and published scope; neither code nor documentation claims that every conceivable bug or question has been solved.


## Revised acceptance policy: useful answers from imperfect spreadsheets

This revision incorporates the user's 3 October feedback. Input irregularity is not a reason to
require workbook cleanup. Preserve data, make the chosen source scope visible, and clarify only
an ambiguity that affects the requested answer. Keep automatic knowledgebase joins for city/country
and currency/rates. Gemini may rewrite wording using schema context; it must not produce SQL or
receive cell values for question answering.

### Implementation and alternatives

| Change | Plan A implemented in the next candidate | Alternative if its acceptance gate fails |
|---|---|---|
| Typing during loading/answering | Sheets and Excel composers stay editable; send alone waits for readiness. Submitted text is cleared at submission; a failure restores it only if no newer draft exists. | Preserve the draft independently of the host iframe lifecycle; show a retry action for the failed turn. |
| Duplicate/blank headers | Keep every column. Assign stable column-letter names to blanks and positional suffixes to duplicates. A repeated field selected for arithmetic/filtering must be distinguished by the question. Counts and unrelated fields remain usable. | Present the repeated fields as a targeted choice, preserving the original cells and remaining analysis. |
| Shifted/missing headers | Keep all cells with neutral positional names when a header is unreliable; show a warning instead of refusing the entire sheet. | Ask which source column the requested measure refers to; do not guess a shifted monetary binding. |
| Merged data cells | Preserve stored values and blanks; never invent a fill-down value. | Offer an explicit, inspectable fill-down transformation for a selected field, with original cells retained. |
| Formula errors/missing cached values | Preserve error markers as text and warn. Observed nonnumeric values override a learned numeric label, so other fields/counts work without silently dropping invalid amounts. No formula execution. | Return a labelled partial calculation with counts of excluded/missing cells, only under an explicit user-selected policy. |
| Embedded totals | Separate rows explicitly labelled Total/Subtotal with numeric remainder into a summaries table. Detail values and the existing totals are both retained. | Ask about the affected data region when layout alone cannot distinguish summaries from records. Do not silently count both. |
| Unrelated oversized tabs | Default to the active sheet if the entire workbook exceeds the bounded request capacity. Display a scope selector and the selected sheet; all-tabs requests remain complete or produce a scope-specific capacity message. Source scope is stored with the conversation and sent as schema context. | Read selected tabs or selected data regions in bounded batches, retaining an included/omitted manifest. Never claim a workbook total from an unlabelled subset. |
| Audit and recovery | Persist bounded warnings/source scope. Keep correct answers if history saving fails. Keep the newer draft after a network failure. | Downloadable answer/source audit plus retryable persistence, using the same durable request identity. |

These are adaptations of items 8, 10, 12, 13, 14 and 17 above. They do not relax operator fidelity,
Unicode/polarity preservation, complete result delivery, or factual rendering. Unknown values are not
zero, a city need not have a user-supplied country master, and an unsupported interpretation is not
permission to answer a different question.

### Current evidence and remaining gates

- The previous `97dd830` image passed the earlier failed seeded dataset cases except the after-August-10 count; its remaining failures were that count, full-column scale projection and serving-role access to durable replay. Source `301b46e` addresses those three; a new full candidate build is required to establish that they pass together.
- The new forgiving-import changes pass 752 local Python tests, the complete web test script and 45 browser fixtures. These include draft recovery, source scope, duplicate/blank headers, formula errors, long Unicode field names and field/aggregate fidelity. Local tests do not certify production deployment or live Gemini/Apps Script behavior.
- The 1,034-question model-only Spider run for `97dd830` scored 225 strict and 274 lenient, with 370 answered and 664 abstained. It is neither the earlier 497 result nor evidence of final-candidate accuracy. Re-evaluate the frozen final candidate and publish answered-wrong versus abstained counts separately.
- Preserve the original independent numerical gold and all ordered follow-ups. Test expectations that required duplicate-header or merged-cell blanket refusals are replaced with value-preservation, ambiguity and draft-recovery assertions; arithmetic gold is not loosened.
- Production still needs the new image gates, migration/grants and serving-role smoke, exact artifact promotion, Hosting/Apps Script version checks, all 18 public examples with ordered follow-ups, real Sheets, realistic 30,000-row persistence/reload/export, and final review. Do not describe this source work as already deployed or launch-complete.
- Use the signed-in **in-app browser**, per the user's clarification; no separate Chrome connection is required.

### Additional final-candidate safeguards

- Raw CSV follows the same deterministic field naming contract as workbook imports. Uneven rows retain populated surplus fields; blank trailing fields do not create phantom columns. The unused NDJSON auto-detection path is removed, preventing bracketed CSV headers from being mistaken for JSON.
- Field identifiers are bounded to 63 UTF-8 bytes with stable positional suffixes. Duplicate fields require a targeted choice only when the requested operation depends on one of them. A known requested measure cannot be replaced by a different measure or row count.
- Hosting preserves the reference deployment's Google sign-in. Publish it with `_AUTH_PROVIDER=google` and `_CUSTOM_DOMAINS=chat.prereasoner.com,prereasoner.com`; Community deployments retain the anonymous default. Verify the destination site before publishing.
- The immutable engine build now runs the reviewed full web test script alongside Python checks and the live seeded product suites. Real 30,000-row host responsiveness remains an authenticated browser gate; fixture success is not a latency measurement.

### Candidate review iteration

- The `fcd9dab` engine build stopped at a calculation fixture that used an ambiguous generic rate while testing grouping. The grouped fixture now states a fraction unit explicitly; the generic-rate ambiguity guard remains intact. Run module entry points as well as pytest: several historical suites record failures in counters instead of raising assertions, so pytest alone is insufficient evidence.
- Live Gemini routing found three presentation failures: an ambiguity was stated without requesting a choice, a small tabular result omitted its values from chat, and a busy-engine message did not explicitly tell the user to resend. The shared deterministic renderer now provides those recovery/result details without invoking factual prose generation. Existing arithmetic and tool-loop tests retain their gold; display expectations reflect the new helpful text.
- The subsequent complete local module runner passed every invoked hermetic suite. The live external-model and rebuilt seeded-image lanes remain separate requirements; none of these changes has yet been promoted to production.

- The clean `5856348` model-only run completed at 214/1,034 strict and 274 lenient, with 363 answers and 671 abstentions. This remains a material release accuracy limitation. It also exposed valid DISTINCT readings rejected as unresolved wording. Relational instruction words are now consumed only when the selected AST proves distinctness or explicit ordering; wrong sort direction/field and missing distinctness have contrast tests. This changes the candidate and requires fresh image and accuracy gates.
- The final documentation review removed obsolete references to Gemini factual rendering and the deleted `engine/converse.py`/`engine/sql_fallback.py` modules. Factual presentation uses the shared deterministic renderer; schema-only wording assistance uses `question_rewrite.py`. Historical review evidence above retains the filenames used when those defects were recorded.
- The local Python 3.11 runtime uses SQLite 3.38, whose `ROUND(120.0, 20)` produces `119.9999999999999`. The binary-exact grouped conversion fixture now emulates PostgreSQL numeric rounding in that disposable SQLite connection and asserts the corrected primitive. Its original gold and exact stage-equality checks are unchanged; live PostgreSQL remains the arithmetic release gate.

- The `5856348` seeded image gate found two further role errors: “total amount paid to suppliers” rejected a correct scalar SUM because it demanded Supplier in the output; “show all rows ordered by id” projected only the sorting field. The shared contract now distinguishes unqualified plural recipient classes from named/qualified recipient filters, and complete-record requests preserve all columns in source order. Contrast tests retain recipient, grouping and sorting constraints. Actual frozen-model local reproductions return 2,128,324.96 for the supplier fixture and all 30,000 × 12 subscription records in 6.8 seconds. These are not the replacement PostgreSQL image gate.

### Shared schema-only starter questions and hosted add-in UI

| Change | Plan A | Alternative and acceptance gate |
|---|---|---|
| Three useful starter questions | Authenticated, separately budgeted `/chat/suggestions` sends only sheet names, column names, active sheet and source scope to Gemini. Its response selects supported operations and field indices; local templates produce the question text. No SQL, values, formulas, history or factual answers are sent. | On unavailable/malformed generation, return three deterministic schema questions. Reject unknown fields, excluded tabs, edits, formula audits and unsupported operations. |
| Shared sidebar behavior | Web home/rail, Sheets and Excel use `sidebar-suggestions.js` and its shared stylesheet. Selecting a question fills the editable composer, without executing it. An existing draft requires an explicit replacement action. Requests have stale-response guards and never block reading or submitting. | Hide optional suggestions on transport failure; normal questions remain available. Browser gates cover draft retention, click behavior, metadata-only payloads and outages. |
| Hosted add-in assets | Sheets keeps a stable HTML loader and the Apps Script read/authentication bridge; sidebar/history markup, CSS, JavaScript and shared modules load from Firebase Hosting. Excel's manifest/task-pane assets already point to Hosting. Hosting static assets revalidate; the Sheets asset path has public static CORS. | If an asset cannot load, preserve the draft and show a reopening/retry message. One initial Apps Script shell update is required; subsequent UI changes deploy through Hosting. Host permissions, bridge/API changes and manifest changes still require a platform release. |
| Real workbook import | The original Subscriptions XLSX passes the exact product worker with 9,741 NT, 1,362 SI and 12,648 FF rows; repeated NT headers are preserved with positional names and a warning. | Verify these source counts and targeted queries in the published sidebar. Imported report-only tabs are not represented as data tables; included source scope is visible. |

The new feature expands schema-only language assistance, not the set of deterministic engine operations.
An offered question is a proposed request, not a claim about unseen rows. Final image, production and
authenticated browser gates remain pending; no launch-ready claim is made by this implementation record.
