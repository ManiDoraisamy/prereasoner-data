# Review of Claude's fixes and fresh Spider accuracy

Prepared: 2026-10-08. Reviewed checkout: `30a8cb106db074fde1fd7f307eec3da872f7431d`.

## Assessment

Claude's completeness changes improve the recorded benchmark while preserving deterministic AST construction. They fix useful cases such as split schema names and question words that belong only to an unrelated table. Removing the dormant profile-search implementation also consolidates ownership without replacing the active planner.

The fixes are not sufficient to conclude that the implementation is correct. The review reproduces a serious remaining measure-substitution defect, plus three incomplete semantic checks. Passing the existing regression suite does not cover these contrasting cases. The fresh full evaluation confirms **315/1,034 strict (30.5%)**, **381/1,034 lenient (36.8%)**, and **524/1,034 answered (50.7%)**. This review keeps production code and the promoted model bundle unchanged so the measurement evaluates Claude's checkout exactly.

## Scope and evidence

Reviewed changes from `97ac4ff5178a54e81babbb9e26927e0a0f542c6b` through `30a8cb106db074fde1fd7f307eec3da872f7431d`, covering Claude's two commits:

- `8b8a7ee`: completeness/name reading, removal of inactive structural-profile code, first-install currency refresh, empty-calculation explanations, and concurrent principal creation.
- `30a8cb1`: malformed-measure refusals, unmatched-world-row disclosure, row-noun grouping, empty-amount conversion coverage, and fixed chat generation settings.

Inspected the affected production owners, their callers, changed tests, and [recorded results](C:/work/prereasoner-data/spider/results/RESULTS.md). Additional checks exercise the production coverage and selection functions, not a copied validator. Imported SQL in these checks is a deliberately contrasting test candidate; no model-generated query was introduced into serving.

The initial Git status contained only the existing untracked `release-review/` directory. This task adds this report and, after completion, a measured-results entry. It does not train, change weights, make paid model calls, commit, push, or deploy.

## Confirmed findings

### F1 — High: a mostly malformed requested measure can still be replaced with a world attribute

**Owners:** [unreadable_cells](C:/work/prereasoner-data/engine/query_contract.py:45) and [ComposeEngine.plan](C:/work/prereasoner-data/engine/compose.py:389).

The new compose guard depends on `unreadable_cells`, which returns `None` when at least half of the filled cells are not numbers. That threshold is useful for recognizing an otherwise numeric column, but it is not proof that an explicitly requested aggregate operand may be replaced. Consequently the new guard stops firing precisely when the malformed-data case becomes more severe.

**Reproduction:** use `tests.test_compose._orders_sheet()` and its `ORDERS_WORLD`; replace the first 12 amount cells with `not-a-number`; call the existing `_run` for `total amount of GBP orders in Europe`. The five GBP orders all have invalid amounts. The result is:

```text
answer.columns = ['amount', 'population']
answer.rows = [['not-a-number', 43998640]]
clarify is absent
plan = ['filter', 'world_join', 'world_filter', 'group_agg']
```

`43,998,640` is five times London's population, not the requested order amount. Replacing every amount cell gives the same wrong computation. Replacing only the first amount cell correctly produces the new refusal. This contrast shows that the one-bad-cell regression test is insufficient. The wrong computation is reproduced in the current production compose owner with the encoder-free fixture used by its test suite; a deployed HTTP replay remains unverified because the configured world database was unreachable.

**Required correction:** bind the requested aggregate operand before choosing a replacement numeric measure. Once an explicit SUM/AVG operand is identified, any nonnumeric participating cell must produce the existing refusal; the proportion of numeric cells must not authorize another operand. Preserve separate handling of a column that merely names a grouping, recipient, or filter. Extend the existing binding/contract owners rather than adding a planner.

**Regression cases:** minority, exactly-half, majority, and all malformed values; clean amounts; COUNT over the same rows; a requested population aggregate with an unrelated bad amount; a text grouping; and a malformed amount outside the explicitly filtered population. Verify both the compose owner and the served world route.

### F2 — Medium: alphabetical ordering is accepted without proving the requested field or direction

**Owners:** [ordering_requested](C:/work/prereasoner-data/engine/sql_expansion.py:43), [SQLSearcher._order_choices](C:/work/prereasoner-data/engine/sql_search.py:1463), and [read_question](C:/work/prereasoner-data/engine/query_contract.py:697).

The new reading check treats every ordering word as read whenever the query has an order. Existing direction checks protect explicit `ascending`/`descending` requests, but do not establish the meaning of `alphabetical`.

With the existing `PEOPLE_TABLE`, both contrasting candidates are marked complete for `List the names in alphabetical order.` and accepted by `TableQuery.select_query` when supplied as the searched pool:

```sql
SELECT Name FROM people ORDER BY Age;
SELECT Name FROM people ORDER BY Name DESC;
```

An alphabetic order needs the requested text field; its default direction is ascending. A descending alphabetical request needs its explicit direction. An unrelated numeric order cannot discharge that requirement.

There is also a candidate-generation gap: `ordering_requested` recognizes `alphabetically`, but not `in alphabetical order`; `_order_choices` likewise only checks the adverb for text projection targets. A normal hermetic serving request for the adjective form is refused and its pool contains only unordered Name projections. The equivalent `List the names ordered alphabetically.` succeeds with `ORDER BY Name ASC`.

**Required correction:** recognize both grammatical forms in the existing shared ordering cue owner; construct the text-field order in the existing search; validate the bound field and requested/default direction in the shared request contract. Avoid making any ORDER BY sufficient evidence.

**Regression cases:** both grammatical forms, correct ascending text order, unrelated numeric order, reversed default direction, explicit reverse alphabetical order, multiple projected text fields with an explicit target, and a data field/value named `Alphabetical`.

### F3 — Medium: a spelled ranking number can be discharged by an unrelated SQL number

**Owner:** [read_question](C:/work/prereasoner-data/engine/query_contract.py:694).

The new rule reads a spelled number whenever its numeric equivalent appears anywhere in the rendered SQL. It does not prove which operation consumes that number.

For `List the names of the two people with the largest Age, excluding Person_ID 2.`, the following candidate is complete and accepted through the existing selection gate:

```sql
SELECT Name FROM people
WHERE Person_ID != 2
ORDER BY Age DESC LIMIT 3;
```

The ID predicate correctly consumes `2`, but the request also asks for two ranked results. Extend `PEOPLE_TABLE` with Dan (ID 4, Age 50) and Eve (ID 5, Age 60): the accepted query returns Eve, Dan, and Cara; the requested top two are Eve and Dan. This is an executed wrong result, not just a SQL-text discrepancy. The newly added test only contrasts LIMIT 2 against LIMIT 3 without another occurrence of 2, so it misses this bypass. In ordinary hermetic search, the original wording without the exclusion instead produces LIMIT 1 candidates and is refused. Treat that construction gap separately from the validator's false acceptance.

**Required correction:** attach the number to its semantic role. A ranked-count requirement needs the corresponding cutoff in the relevant AST scope; a threshold needs its corresponding comparison. Reuse the existing numeric/extrema parsing owners and consume their scoped evidence, rather than adding another SQL-text presence check.

**Regression cases:** correct cutoff, wrong cutoff with the same number in a predicate or string literal, correct threshold with no ranking request, a ranking plus an independent threshold, repeated numbers in different scopes, and the normal serving path.

### F4 — Medium: a participle/value pair does not prove the relationship's operand

**Owners:** [value_participles](C:/work/prereasoner-data/engine/closed_class.py:144) and [read_question](C:/work/prereasoner-data/engine/query_contract.py:706).

The new participle rule verifies that the object's words occur among schema names and SQL literals. It does not prove that the participle's relationship uses the correct column or predicate.

For a flights fixture with opposite routes APG→ASY and ASY→APG, this query is complete and accepted by the production selection gate for `List the flight numbers of flights departing from APG.`:

```sql
SELECT FlightNo FROM flights WHERE DestAirport = 'APG';
```

That query finds arrivals. Ordinary hermetic search happens to choose SourceAirport correctly for the departure request, and DestAirport correctly for the arrival request. The finding is specifically an unsound acceptance rule, not a claim that those two normal serving requests currently fail. It matters when ranking, schema ambiguity, or changed column names produce a contrasting candidate.

**Required correction:** bind relationship evidence to the column/predicate and scope which realizes it. Extend the existing deterministic role/value linker and share its result with the contract. If that relationship cannot be established, preserve the unresolved reading instead of discarding the participle.

**Regression cases:** departures versus arrivals with the same airport value in both columns; owned-by versus owned-to relationships; a participle that is a source status; omitted predicate; swapped operand; and nested/negated scope.

### F5 — Evaluation limitation: compose refusals do not follow the same control flow in the evaluator

**Owners:** [ComposedKnowledgeQuery._run_engine](C:/work/prereasoner-data/engine/knowledge_compose.py:421), [its serving decision](C:/work/prereasoner-data/engine/knowledge_compose.py:620), and [compose_predict / predict](C:/work/prereasoner-data/spider/probe/full_eval.py:511).

The new serving code returns compose's `clarify` response before normal ownership selection, and the caller explicitly accepts that response. `compose_predict` omits `clarify` and `reason`, reports `ok=True`, and the world-less evaluator then delegates to AST because the world dependency is absent.

This is a real control-flow difference for malformed measures and matters when using Spider as a claim about full serving correctness. The evaluator's existing module documentation already discloses omission of the live clarification gate and describes its result as an upper bound. The current run's numbers should therefore be described as the established served-AST benchmark contract, not a deployed world-data or full HTTP accuracy measurement. This review does not infer a numerical overestimate on the current DEV dataset without a replay of the affected cases.

**Required correction:** use one shared production decision for clarification/ownership and have the existing evaluator consume it. Add a regression in which compose refuses a named measure while an AST candidate remains available. Keep the current scorer and gold-blind inputs unchanged, and label any changed evaluation contract when comparing historical results.

## Review of the fixes that hold up

| Change | Review outcome | Evidence / limit |
|---|---|---|
| Restrict schema-word readings to relevant tables and normalize split names | Useful improvement, but still lexical rather than a complete semantic witness | New positive/contrastive tests pass; remaining operand/scope gaps above |
| Read two-word names and spelled numbers | Two-word fixture passes; numbers need scoped evidence | F3 |
| Read ordering words | Reduces refusals, but incomplete semantic proof | F2 |
| Read value-attached participles | Handles documented departure fixture; relation binding remains incomplete | F4 |
| Remove inactive profile search and its tests | Consolidates the active planner | No live callers found for deleted profile symbols/configuration in engine, tests, or evaluator sources |
| Refuse one malformed amount and name the cell | Correct for the covered minority-error case | F1 documents the more severe case that still fails |
| Keep row nouns from becoming groupings | Covered renamed-sheet and explicit-grouping contrasts pass | Compose suite passes |
| Disclose unmatched world rows | Hermetic typo/unknown-name cases pass | Deployed world/converted path unavailable for independent verification |
| Empty measures do not require an exchange rate | Implementation targets participating non-null measures; calculation regressions pass | Live currency/world route not independently replayed |
| Refresh ECB rates after initial seed import | Correct placement after seed bootstrap; deployment regressions pass | No new deployment performed |
| Principal insert tolerates unique conflicts | Change uses existing owner and handles conflict without a second path; focused tests pass | Synthetic database test does not independently prove concurrent behavior on live PostgreSQL |
| Chat temperature/seed | Passed through the existing LLM client and covered by tests | Hosted replies are still not guaranteed reproducible by those settings; no hosted calls in this run |

## Validation performed

The following existing suites completed successfully with external LLM access disabled:

- `tests.test_query_contract`: 43 registered tests passed.
- `tests.test_sql_ast`: 192 passed, 0 failed.
- `tests.test_compose`: 33 passed, 0 failed.
- `tests.test_calculations`: 179 passed, 0 failed.
- `tests.test_request_limits`: 27 passed, 0 failed.
- `tests.test_llm`: 9 passed, 0 failed.
- `tests.test_community_deploy`: 26 passed, 0 failed.
- `tests.test_orchestrator_unit`: 48 passed, 0 failed.
- Python compilation of `engine`, `db`, `training`, `tests`, `orchestrator`, and `mcp_server` passed.

`tests.test_world` initially self-skipped because its entry point checks the environment before loading repository configuration. An explicit configuration-loaded retry failed connecting to the configured PostgreSQL host. Neither attempt is a live-world pass. No full repository-wide suite, deployed browser demo gate, hosted rewrite evaluation, training, or deployment was performed.

Additional contrasting checks reproduce F1–F4 using production owners. The [disposable reproduction script](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-30a8cb1-20261008/review_reproductions.py) and its [executed results](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-30a8cb1-20261008/review-reproductions.json) are saved with the evaluation artifacts. They are review evidence, not additions to the repository's regression suite; the measured source tree remains unchanged.

## Fresh full Spider measurement

The independent run uses the existing evaluator, all 1,034 DEV questions, `whole_db`, `served`, SQL backend, compose routing enabled, a 5,000-row table cap, and the existing 12-second soft prediction budget. External rewriting is disabled. Gold and prediction use the same capped database inputs. Strict/lenient definitions are the existing `spider_eval.compare` definitions; lenient containment is a generous measure and does not prove full answer correctness.

Output directory: [fresh review artifacts](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-30a8cb1-20261008).

Reproduction command from the repository root (use a fresh tag/output directory for a new source tree):

```powershell
$env:EXTERNAL_LLM_ENABLED = '0'
$reviewRun = Join-Path $env:TEMP 'prereasoner-spider-review-30a8cb1-20261008'
.venv\Scripts\python.exe spider\probe\full_eval.py `
  --data spider\data --dbs spider\data\dbs --out $reviewRun `
  --config whole_db --selection served --backend sql `
  --cap 5000 --timeout 12 --tag review-30a8cb1 --checkpoint-every 25
```

### Completed results

All 1,034 unique DEV indices were evaluated; all routed to the AST path. No predictions were resumed from an earlier run. There were no gold-execution errors and no predictions over the 12-second soft budget.

| Metric | October 7 engine baseline | Fresh current checkout | Change |
|---|---:|---:|---:|
| Strict, all questions | 247/1,034 (23.9%) | **315/1,034 (30.5%)** | +68; +6.6 percentage points |
| Lenient, all questions | 315/1,034 (30.5%) | **381/1,034 (36.8%)** | +66; +6.4 percentage points |
| Answered | 414/1,034 (40.0%) | **524/1,034 (50.7%)** | +110; +10.6 percentage points |
| Strict among answered | 247/414 (59.7%) | **315/524 (60.1%)** | +0.5 percentage points using unrounded fractions |
| Lenient among answered | 315/414 (76.1%) | **381/524 (72.7%)** | −3.4 percentage points |
| Legacy scalar-gold metric | 149/408 (36.5%) | **185/408 (45.3%)** | +36 |
| Refused/error | 620/1,034 (60.0%) | **510/1,034 (49.3%)** | −110 |

Percentages and differences are rounded independently from the underlying counts. The among-answered measures exclude refusals and must not replace the all-question headline. The scalar-gold metric retains the existing value-membership scorer; it is not a new shape-exact scalar metric. Lenient also requires nonempty gold values, so strict-correct empty results are not necessarily lenient-correct: these two counters are not universal subsets of one another.

| Difficulty | Questions | Strict correct | Lenient correct | Answered |
|---|---:|---:|---:|---:|
| Easy | 248 | 127 (51.2%) | 132 | 154 |
| Medium | 446 | 140 (31.4%) | 167 | 234 |
| Hard | 174 | 30 (17.2%) | 51 | 83 |
| Extra hard | 166 | 18 (10.8%) | 31 | 53 |

The paired strict ledger against the October 7 baseline contains **69 wins, 1 loss, 246 unchanged correct, and 718 unchanged incorrect**. There are 120 newly answered questions, of which 68 are strict-correct; 10 previously answered questions are now refused, including one previously strict-correct answer. One previously served strict-incorrect answer becomes strict-correct. The loss is DEV 990, `How much does each charge type costs? List both charge type and amount.` The narrower completeness check leaves `costs` unresolved rather than reading it from an unrelated table.

Against Claude's saved `accuracy3-97ac4ff-dirty` run, every SQL, answered/refused status, and strict/lenient grade matches: **zero wins, zero losses**. Raw averages at DEV 435, 436, and 725 differ at approximately 10⁻¹⁴, without any score change. This confirms benchmark reproducibility at the SQL/scorer level, not bit-for-bit numeric reproducibility across runs. Deterministic numeric execution still deserves its independent exact-arithmetic tests.

Prediction latency: median **1.408 s**, p90 **3.138 s**, p95 **3.955 s**, maximum **7.852 s**. The desktop also ran focused tests and review probes during parts of this measurement. Because the compared messy-data artifact has the same input/source hashes apart from its commit marker, this timing difference is not evidence of a code performance regression or improvement.

### Provenance and remaining loss ledger

The result records commit `30a8cb106db074fde1fd7f307eec3da872f7431d` and `worktree_dirty=true`; the initial untracked review directory makes the Git worktree dirty, while tracked production sources were unchanged. After completion, **137 source/input fingerprints were recomputed with zero changes**, and the runtime bundle was revalidated as `b11056f8ce8bf16a44da69bf76fea238149df439d7fde52d5e1f4747a5f536c7`. The run's hashed inputs and code match Claude's saved `planted-8b8a7ee-dirty` artifact; only the source-commit marker differs.

Among 510 refusals/errors, **379** have a member marked executable-and-grounded eligible, **122** have a pool without an eligible member, and **9** have no pool. Eligibility here is the recorded pre-completeness diagnostic; it does not prove semantic correctness or full production admission. Among answered questions, **209** fail strict scoring, including **81** accepted only by lenient scoring and **128** accepted by neither metric. Fifteen strict-correct empty-result cases fail the nonempty-gold lenient rule. A further 335 questions hit a returned pool size of 25; this saturation alone does not prove pruning caused their failures.

A separate offline diagnostic against Claude's matching predictions finds **17** strict-failing outputs whose normalized row multisets match gold after a consistent column permutation. Four of those were newly answered by the completeness fix. This isolates an output-order comparison issue; it does not establish semantic equivalence on other database instances and does not change any headline score. The remaining correctness gap cannot be explained by column ordering alone.

Saved evidence:

- [Full summary](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-30a8cb1-20261008/full_eval_review-30a8cb1.json).
- [All per-question predictions and grades](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-30a8cb1-20261008/full_eval_per_example_review-30a8cb1.json).
- [Paired comparison, loss ledger, and fingerprint verification](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-30a8cb1-20261008/review-analysis.json).
- [Column-order diagnostic](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-30a8cb1-20261008/column-order-diagnostic.json).

The complete measurement is also recorded in the canonical [Spider results](C:/work/prereasoner-data/spider/results/RESULTS.md). No additional production accuracy gain was introduced by this review.

## Remaining implementation priorities

Claude principally addressed W04 in [sql-accuracy.md](C:/work/prereasoner-data/release-review/sql-accuracy.md), plus unrelated release correctness issues. The change does not implement the report's full semantic-requirement and scoped-binding plan.

1. Make requested operands and relationships explicit before accepting a candidate. Repair F1–F4 in the existing binding/search/contract owners, with positive, same-profile contrasting, and negative tests.
2. Fix operation-versus-column binding: DEV 4 and 17 still bind AVG to `stadium.Average` rather than the requested singer Age or stadium Capacity.
3. Preserve repeated aggregate operands, count populations, grouping grain, and query scope; do not relax lexical rejection globally to chase coverage.
4. Measure admissible correct-candidate recall separately from ranking and refusal. The recorded pre-completeness-change eligible-pool recall of 550/1,034 is historical evidence, not a fresh current-checkout pool-recall measurement.
5. Close the evaluation control-flow gap and broaden independent regressions to malformed inputs and contrasting databases. Keep legacy-score comparisons labelled when the contract changes.

All proposed corrections remain within deterministic typed-AST construction and named evidence. None requires a next-token SQL/AST decoder, a second planner, an opaque answer arbiter, or restoration of the removed 7B SQL proposer.

## Final workspace state

```text
 M spider/results/RESULTS.md
?? release-review/
```

The tracked change is this task's new canonical measurement entry. `release-review/` was already untracked; its previous reports are preserved, and this task adds `claude-fixes-review.md`. Reproduction scripts, logs, and benchmark artifacts are outside the repository in the linked temporary output directory. Tracked-diff and new-report whitespace checks passed. Production code and the promoted runtime bundle were not changed.
