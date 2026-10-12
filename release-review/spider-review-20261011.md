# Claude fix review and matched Spider comparison — 2026-10-11

## Latest independent review: committed fix `6263502`

**Fresh full result: 386/1,034 strict (37.3%), 444/1,034 lenient (42.9%), 590/1,034 answered (57.1%).** All 141 recorded provenance fields stayed unchanged. The independent predictions exactly reproduce Claude's `r123-dirty` run: no SQL, strict, lenient, or answered-status changes across the full set.

**Assessment:** Claude repaired the previous R1/R2/R3 counterexamples and made a small, real DEV accuracy improvement. Four strict answers improve, none regress against `3ef82c4`; the remaining review nevertheless reproduces a wrong zero-inclusive range answer, three count/membership verification gaps, unbounded count-proof work, and an arbitrary ranking-number exclusion. The passing regressions do not establish correctness of these entire families.

Reviewed commit: `6263502f7cdd3c0639208f13e49daa4563bfaae0`, committed while the initial checkout inspection was in progress. The existing managed review worktree was reused at that clean commit for the full run and tests. Claude's saved run used `8164601` with uncommitted fixes; every runtime/data/model/scorer fingerprint matches this commit, with only the commit and dirty-state provenance fields differing. This review changes documentation only.

### Matched comparison on all 1,034 questions

| System | Strict / 1,034 | Lenient / 1,034 | Answered / 1,034 |
|---|---:|---:|---:|
| **Deterministic engine, fresh run** | **386 (37.3%)** | **444 (42.9%)** | **590 (57.1%)** |
| Claude Sonnet 5.5 | 840 (81.2%) | 897 (86.8%) | 1,023 (98.9%) |
| Gemini 3.8 **Flash** | 872 (84.3%) | 904 (87.4%) | 1,034 (100.0%) |

All systems use the same question/database identities, capped execution data, independently executed gold answers, and repository comparator. Engine configuration: `whole_db`, `served`, SQL backend, compose enabled, cap 5,000 rows/table, 12-second soft prediction budget, `EXTERNAL_LLM_ENABLED=0`, unchanged promoted bundle. No gold tables or gold SQL inform serving.

**Reference freshness:** October 7 Sonnet/Gemini SQL predictions were re-executed and regraded during this review; no reference grades changed. No new model API calls occurred. Gemini is Flash, not Pro. This matches task, execution data and scorer, but not identical input presentation or inference budgets: references received DDL, FKs and three sample rows/table; the engine inspects capped tables. Sonnet used the saved non-thinking configuration; Gemini used temperature 0 and LOW thinking. These are results of those specific configurations.

**Metric contract:** strict compares normalized row multisets, preserving duplicate multiplicity and column positions while ignoring row order. Lenient checks gold cell-value containment and can admit extra rows, extra columns, and wrong associations. Answered means successful prediction/execution, including successful empty results. These are repository execution metrics, not official Spider SQL exact-match or test-suite accuracy.

The strict gaps are **454 answers / 43.9 percentage points to Sonnet**, and **486 / 47.0 points to Gemini Flash**.

| Paired strict outcome | Engine vs Sonnet | Engine vs Gemini Flash |
|---|---:|---:|
| Both correct | 359 | 366 |
| Only engine correct | 27 | 20 |
| Only reference correct | 481 | 506 |
| Neither correct | 167 | 142 |

On the **same 590 engine-answered questions**, strict correctness is **386/590 (65.4%)** for the engine, **496/590 (84.1%)** for Sonnet, and **505/590 (85.6%)** for Gemini. Engine lenient correctness among its answers is **444/590 (75.3%)**. On the 444 engine-unanswered questions, Sonnet is correct on 344 and Gemini on 367. The net strict gaps decompose as **344 + 110 = 454** and **367 + 119 = 486**: roughly three quarters arise on unanswered questions, with substantial returned-answer quality gaps as well.

### Measured effect of Claude's fixes

Compared with `3ef82c4`: **382 → 386 strict, 440 → 444 lenient, 591 → 590 answered**. Strict transitions: **4 wins, 0 losses, 382 unchanged-correct, 648 unchanged-wrong**. Seventeen SQL records change: **124, 175, 178, 243, 244, 333, 334, 371, 477, 478, 526, 551, 642, 860, 922, 932, 942**.

| DEV cases | Before → after | Interpretation |
|---|---|---|
| 333/334 | Wrong/refused → strict-correct template type codes | Unrequested document/paragraph joins disappear; the related-count plan returns `CV` |
| 477/478 | Wrong empty answers → strict-correct country codes | Counts players directly, removing the unrequested match join |
| 175/526 | Wrong answers → refusals | Useful hardening, not recovered correct answers |
| 243/244 | Strict-correct → strict-correct | Zero-inclusive count construction uses an anti-membership plan; no headline gain on these data |
| Other changed SQL | Correctness unchanged | Smaller join/output changes do not themselves establish accuracy gains |

The four wins are **two question pairs, all in the easy stratum**. There is one newly answered correct question (334) and two formerly wrong answers now refused (175/526), so coverage falls by one. Strict-wrong returned answers fall from 209 to **204**.

The disjoint partition is **386 strict-correct, 77 answered lenient-only, 127 answered incorrect under both, 444 unanswered**. Unanswered stages: **362 completeness, 73 constraints, 9 AST search**. Nineteen strict-correct empty-gold cases remain lenient-false; lenient-incorrect is therefore not a clean semantic failure bucket.

| Difficulty | Questions | Strict correct | Answered |
|---|---:|---:|---:|
| Easy | 248 | 137 (55.2%) | 162 |
| Medium | 446 | 170 (38.1%) | 259 |
| Hard | 174 | 53 (30.5%) | 101 |
| Extra hard | 166 | 26 (15.7%) | 68 |

The earlier country-count gold defects at DEV 161/162 remain unchanged: the requested country count is four, while gold returns maker counts within four countries. The common gold/scorer is retained for every system; semantic adjudication is separate from the headline. Compared with the earlier `3817b34` measurement, the cumulative transition is four wins and those two gold-label losses, not six semantic changes attributable to this latest fix.

### Previous findings rechecked

- **R1 original zero-count cases:** fewer-than-two counts/listings now include Germany; exact-zero returns one country; the positive more-than-one control still returns one. The electric-maker child-filter regression in the registered suite passes. Zero-inclusive ranges still fail, as S1 shows.
- **R2 original membership cases:** Manager_ID and arithmetic projections are rejected, wrong set projections are rejected, and correct ID membership/set controls pass. Row-collapsing aggregate projections still bypass the population proof, as S4 shows.
- **R3 original ranking cutoff:** top 100/top 101 return the expected number of rows on the 150-person fixture. The new 120-oldest regression passes. Cutoffs in the year-like numeric range still refuse in another supported phrasing, as S6 shows.

### S1 — High: a zero-inclusive count range still drops zero-related entities and passes as complete

**Owners:** [query_contract.counts_as_asked](C:/work/prereasoner-data/engine/query_contract.py:1409), [the comparison walker](C:/work/prereasoner-data/engine/query_contract.py:1079), and [without_failing_groups](C:/work/prereasoner-data/engine/sql_constraints.py:246).

On the same five-country fixture, ordinary serving answers `How many countries have between 0 and 2 car makers?` with **3**, expected **4**. `Which countries have between 0 and 2 car makers?` returns country IDs **2/3/4**, omitting zero-maker Germany and the requested country display. Both answers are marked valid. The positive `between 1 and 2` control returns Japan/Korea/Korea correctly.

The checker accepts grouping the child table alone as sufficient zero-inclusive population evidence. That enumerates only countries present in child rows. Meanwhile the correct parent anti-membership query with `HAVING COUNT(*) < 0 OR COUNT(*) > 2` is rejected: `_comparisons` drops the OR, even though `without_failing_groups` constructs an OR when complementing a conjunctive range.

**Repair:** prove the requested entity population, not merely the presence of a grouped counted table. Validate the entire typed Boolean count formula and its complement, including impossible bounds, rather than flattening away disjunctions. Preserve the requested entity projection and independently verify scalar count/list agreement. Test zero/one/many related rows, ranges admitting/excluding zero, child filters, repeated entity labels, and correct anti-membership versus incomplete child-only grouping.

Evidence: [ordinary-serving range and correct-candidate probes](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/range-count-probes.json).

### S2 — Medium: a count of the wrong entity satisfies the child-count requirement

**Owner:** [query_contract.counts_as_asked](C:/work/prereasoner-data/engine/query_contract.py:1395).

For `Which countries have more than 1 car maker?`, a typed query grouped by country with `COUNT(DISTINCT countries.CountryId) > 1` passes completeness and returns **empty**, expected **USA**. The `COUNT(*) > 1` control over the correctly joined maker population passes and returns USA.

The proof checks that the counted table is referenced, then compares bounds as if every COUNT measured that table. A count of distinct parent IDs is not a count of makers. **Repair:** bind every aggregate to the requested counted population, key/distinctness, filters, and grouping grain; establish when COUNT(*) counts one child per joined row and when a column count excludes missing values. Test parent versus child keys, COUNT/COUNT DISTINCT, nullable attributes, and multiplying joins. This is a verification counterexample, not evidence that ordinary search selects this particular bad candidate.

Evidence: [count operand and membership probes](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/review-family-probes.json).

### S3 — Medium: identical bounds on different populations collapse into one requirement

**Owner:** [sql_expansion.count_thresholds](C:/work/prereasoner-data/engine/sql_expansion.py:710) and [counted_thresholds](C:/work/prereasoner-data/engine/sql_expansion.py:718).

`Which countries have more than 1 car maker and more than 1 airport?` produces only the car-maker count requirement. A candidate joining both relations but checking only `COUNT(DISTINCT car_makers.Id) > 1` passes completeness and returns **A/C**, expected **C**: A has three makers but only one airport. The control checking both distinct child counts returns C. Changing the airport bound to two causes the omitted-condition candidate to be rejected.

Both the threshold deduplication and the final mapping use numeric predicates as identity, losing separate occurrences/entities. **Repair:** preserve occurrence, counted entity/relation, filters and scope; deduplicate only proven equivalent requirements. Require an explicit witness for every requested population. Test equal bounds on different relations, repeated equivalent wording, conjunction/disjunction, different filters of the same relation, and crossed counts. These are checker probes, not a claim that this exact candidate is selected by ordinary serving.

Evidence: [multiple-count positive and negative probes](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/multiple-count-probes.json).

### S4 — Medium: MAX/MIN projection preserves a value's type but can discard requested people

**Owner:** [query_contract._binds](C:/work/prereasoner-data/engine/query_contract.py:1140).

For Ann/20, Bo/40, Cy/60, `List names of people whose age is above 35` is deemed complete for `Person_ID IN (SELECT MAX(Person_ID) ... WHERE Age > 35)`. It returns **Cy**, expected **Bo/Cy**. Plain ID projection returns both. Unwrapping MAX/MIN to its operand proves the value's kind, not preservation of the requested qualifying row set.

**Repair:** carry row-population/cardinality evidence through membership projections. Accept an aggregate projection only with a justified requested extremum or a proof that grouping preserves the required identities. Do not blanket-reject legitimate extrema; add ordinary population, MAX/MIN collapse, grouped-identity preservation, and explicitly requested-extremum contrasts. This is a coverage-checker defect; ordinary selection of this bad candidate was not established.

Evidence: [aggregate projection and plain-ID control](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/review-family-probes.json).

### S5 — High: count verification work grows with an arbitrary number in the question

**Owner:** [query_contract.counts_as_asked](C:/work/prereasoner-data/engine/query_contract.py:1401).

The new `range(0, int(max(bounds)) + 3)` tests each possible count up to the stated number for each relevant query. A direct production-owner probe takes about **0.001 s at 100** and **2.165 s at 1,000,000**; the **1,000,000,000** probe did not finish within three seconds and was terminated. Initialization is excluded from those timings. Even the rejected candidate pays the scan cost. This is proportional to the literal's magnitude, not the table size, and the evaluator's soft budget does not interrupt Python verification.

**Repair:** prove integer-count predicate equivalence using exact interval algebra or the finite boundary partition induced by all predicates, including zero, integer neighbors/fractional-bound transitions and the final unbounded interval. Support the typed Boolean formula from S1. Do not truncate the proof to an arbitrary maximum or reject a valid large threshold to hide the cost. Test large bounds and complements with a deterministic operation-count/bounded-work regression, plus semantic boundary contrasts. The headline DEV run itself had no soft-budget exceedances.

Evidence: [bounded subprocess performance probes](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/count-threshold-performance.json).

### S6 — Low: year-like numbers are still excluded from legitimate ranking cardinalities

**Owner:** [sql_extrema.ranking_cutoff](C:/work/prereasoner-data/engine/sql_extrema.py:831).

`List the names of the 101 oldest people` and the 120 version serve correctly. The **1200** version refuses because `1000 <= value <= 2999` suppresses the cutoff, blaming unknown “1200” wording. On the three-person contrast fixture the requested result is simply the same three ranked people; no large output is needed.

**Repair:** distinguish a cardinality from a year through its grammatical role and bound field, rather than a global numeric interval. Retain the room-111, field-name-number and comparison-value contrasts. Test 999/1000/1200/2999/3000, explicit year fields, and real output-budget refusal. This is a remaining coverage limitation outside DEV, not a newly measured strict regression.

Evidence: [ranking phrasing contrasts](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/review-family-probes.json).

### Next work, tests and reproducibility

Extend the existing shared count reading and contract together: entity-bound requirements, correct zero-inclusive populations, typed Boolean complements and bounded exact proof. Then close membership cardinality evidence and the remaining ranking role ambiguity. After those admission checks are sound, make safe recovery of the **444 unanswered questions** the main accuracy program; retain the **204 strict-wrong returned answers** as a separate correctness workstream. The [existing improvement guide](C:/work/prereasoner-data/release-review/sql-accuracy.md) remains the single implementation plan. No new training or SQL-generating serving path is proposed.

**Validation:** SQL AST 205, query contract 54, compose 33, enrichment 37, deterministic emitters 64, LLM client 10: **403 passed, zero failures**. Compilation passes. These are focused suites and production-owner probes, not the full repository/live Postgres/deployed Chrome release gate. The headline uses the SQL backend; a full Python parity census and fresh candidate-pool census were not run.

Prediction latency: median **1.646 s**, p90 **3.696 s**, p95 **4.484 s**, maximum **9.769 s**. Other jobs and brief probes shared the machine; workload conditions differ from earlier runs, so this is not a controlled speed comparison.

Artifacts are in `C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011`:

- [Full summary](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/full_eval_review-6263502.json) and [all predictions](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/full_eval_per_example_review-6263502.json).
- [Final provenance verification](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/final-provenance-verification.json), [paired comparison](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/paired-comparison.json), and [changed-query analysis](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/review-analysis.json).
- [Sonnet regrading](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/claude-sonnet-5-5-regraded.json) and [Gemini regrading](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/gemini-3.8-flash-regraded.json).
- [Original finding replay](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/extended-reproductions.json) and [top-101 replay](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-6263502-20261011/further-reproductions.json).

All references have 1,034 unique indices with matching question/database/gold identities. Runtime bundle SHA-256: `b11056f8ce8bf16a44da69bf76fea238149df439d7fde52d5e1f4747a5f536c7`; DEV SHA-256: `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4`. Complete source/schema/database/scorer fingerprints are saved with the results. Diagnostic SQL candidates above are human-authored counterexamples converted through existing test helpers to typed ASTs; no model writes serving SQL.

## Historical independent review: committed fix `3ef82c4`

**Fresh full result:** **382/1,034 strict (36.9%), 440/1,034 lenient (42.6%), 591/1,034 answered (57.2%)**. All 141 recorded provenance fields stayed unchanged, with 1,034 unique questions and a clean source checkout. This independently reproduces Claude's `final-dirty` predictions: no selected-SQL or correctness-label changes across the full set.

**Assessment:** the original F1–F5 counterexamples are addressed, but the implementation is not fully correct. The broader review reproduces a zero-related-row count error, incomplete membership binding checks, and a new top-N regression above 100. Passing the new regressions is evidence that the original examples are fixed; it does not close those entire semantic families.

Reviewed commit: `3ef82c4066141f44300a14bda775c0a1358aece2`. Claude committed the initially uncommitted changes while this review started; the full evaluation and focused tests used an isolated checkout of that resulting commit. This task changes review/results documentation only and preserves the measured implementation and promoted bundle.

### Current apples-to-apples comparison

All systems are scored on the **same 1,034 Spider DEV questions**, matching database identities, the same capped execution rows, independently executed gold SQL, and the unchanged repository comparator. Engine settings are `whole_db`, `served`, SQL backend, compose enabled, cap 5,000/table, 12-second soft prediction budget, external rewriting disabled.

| System | Strict / all 1,034 | Lenient / all 1,034 | Answered / all 1,034 |
|---|---:|---:|---:|
| **Deterministic engine, fresh run** | **382 (36.9%)** | **440 (42.6%)** | **591 (57.2%)** |
| Claude Sonnet 5.5 | 840 (81.2%) | 897 (86.8%) | 1,023 (98.9%) |
| Gemini 3.8 **Flash** | 872 (84.3%) | 904 (87.4%) | 1,034 (100.0%) |

Sonnet and Gemini are saved October 7 SQL predictions re-executed and regraded during this review. Neither reference's grades changed. No new model API calls occurred. Gemini is Flash, not Pro, and these references generate SQL rather than rewrite the engine's question.

This matches the task, execution data, and scorer. It does not impose identical input presentation or inference budgets: the references received DDL, foreign keys, and three sample rows per table; the engine inspects capped tables. Sonnet used the saved non-thinking configuration; Gemini used temperature 0 and LOW thinking. The counts measure those configurations, not a provider-wide maximum.

The strict gaps are **458 answers / 44.3 percentage points to Sonnet** and **490 / 47.4 points to Gemini Flash**.

| Paired strict outcome | Engine vs Sonnet | Engine vs Gemini Flash |
|---|---:|---:|
| Both correct | 355 | 362 |
| Only engine correct | 27 | 20 |
| Only reference correct | 485 | 510 |
| Neither correct | 167 | 142 |

Among the engine's 591 answered questions, correctness is **382/591 (64.6%) strict** and **440/591 (74.5%) lenient**. On those **same 591 questions**, Sonnet is strict-correct on **495 (83.8%)**, Gemini on **505 (85.4%)**. On the 443 engine-unanswered questions, Sonnet is correct on 345 and Gemini on 367. Coverage still accounts for about three quarters of the net gap, while returned-answer quality also needs improvement.

### What changed in the measured run

Relative to the independently reviewed `3817b34` baseline: **384 → 382 strict, 442 → 440 lenient, 593 → 591 answered; 0 strict wins, 2 losses, 382 unchanged-correct, 650 unchanged-wrong**. Six selected-SQL records change: DEV **125, 126, 161, 162, 175, 277**. Claude's five-query count uses the intervening leads baseline; six is the comparison against the earlier independent review.

- **161/162:** the engine now correctly counts four qualifying countries as `[[4]]`. Gold instead returns maker counts inside countries, `[[4],[4],[3],[5]]`; the frozen scorer marks the corrected country count wrong under both metrics. These are the only strict losses. Keep the common gold/scorer unchanged and report semantic adjudication separately.
- **125/126:** wrong counts over unrelated grouping/fields are now refused at the constraint stage. This improves refusal behavior, but does not recover a correct answer. Their Horsepower data is text, so numeric interpretation still needs an explicit, validated data policy.
- **175:** SQL changes and remains incorrect; multiple counted populations and output fields are still unresolved.
- **277:** an unrequested hiring join disappears while the answer stays strict-correct.

The disjoint partition is **382 strict-correct, 77 answered lenient-only, 132 answered incorrect under both, 443 unanswered**. There are 209 strict-wrong returned answers. The unanswered stages are **365 completeness, 69 constraints, 9 AST search**. No soft-budget exceedances occurred. Nineteen strict-correct empty-gold answers remain lenient-false, so lenient-incorrect is not a clean semantic failure bucket.

| Difficulty | Questions | Strict correct | Answered |
|---|---:|---:|---:|
| Easy | 248 | 133 (53.6%) | 161 |
| Medium | 446 | 170 (38.1%) | 260 |
| Hard | 174 | 53 (30.5%) | 101 |
| Extra hard | 166 | 26 (15.7%) | 69 |

### Verification of the original fixes

| Original finding | Independent replay outcome | Remaining qualification |
|---|---|---|
| F1: exclusion identity | Repeated local FK and repeated missing-major fixtures retain the student without a cat | This verifies the entity population; mixed-type/missing cells still follow existing ingestion normalization |
| F2: qualifying-entity count | The two-country fixture returns one qualifying country, not two makers | Zero-related entities remain wrong; see R1 |
| F3: membership scope | Basic ID-to-age IN and uncorrelated EXISTS are rejected; correct ID-to-ID IN passes | Different ID roles and computed/set projections still bypass the proof; see R2 |
| F4: repeated aggregates | Both the one-cue and repeated-cue distance/price fixtures return `[[200,60]]` | This does not establish full multiword-operand or separate-population support |
| F5: ranked cardinality | New registered serving tests pass for spelled/digit cutoffs, exclusions, and ranked display | A previously supported cutoff above 100 now refuses; see R3 |

### R1 — High: countries with zero makers are omitted, and an exact-zero count can compare the wrong field

**Owner:** [ConstraintQueryExpander._count_having_candidates](C:/work/prereasoner-data/engine/sql_constraints.py:122), its shared structures in `sql_expansion.py`, and the count/number contract.

The new fixture contains USA with three makers, Japan with one, two distinct countries named Korea with one each, and Germany with **zero**. Ordinary serving answers:

- `How many countries have fewer than 2 car makers?` → **3**, expected **4**.
- `Which countries have fewer than 2 car makers?` → Japan/Korea/Korea, omitting Germany.
- `How many countries have exactly 0 car makers?` → **0**, expected **1**; the selected SQL compares `car_makers.Country = 0` rather than counting makers per country.
- Positive contrast: `more than 1 car maker` correctly returns **1**.

The first two queries use an inner join rooted in makers; no later outer count can restore countries already dropped from that population. The exact-zero case additionally lacks a witness binding the number to a maker count. All three wrong answers are reported valid and coverage-complete.

**Repair:** construct the country population before evaluating related counts. When zero satisfies the requested threshold, retain countries with no matches using a typed left relation and `COUNT` of a matched child key, or an equivalent correctly correlated count. Do not use `COUNT(*)` to count the synthetic empty left-join row. Preserve child filters at the proper scope so countries with zero *qualifying* children are also handled correctly. Verify exact/equality count thresholds against the counted entity, not an unrelated numeric foreign key.

**Tests:** zero/one/several children; `<`, `<=`, `= 0`, positive `>` thresholds; repeated parent labels; explicit nonzero qualifications; child filters; nullable child attributes; and ordinary `which` versus `how many` agreement. Derive expectations independently. The decision's observation that Spider gold uses inner joins is not sufficient justification for excluding zero-child entities from a general engine.

Evidence: [extended ordinary-serving reproductions](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/extended-reproductions.json).

### R2 — Medium: membership binding still accepts a shared name token or an unproved projection

**Owner:** [query_contract._binds](C:/work/prereasoner-data/engine/query_contract.py:1120).

For people Ann/20, Bo/40, Cy/60, the question is `List names of people whose age is above 35.` The correct ID membership returns Bo and Cy. Three wrong typed candidates also pass the production completeness checker:

| Inner projection tested against outer `Person_ID` | Actual rows | Why the proof fails |
|---|---|---|
| `Manager_ID` with inner `Age > 35` | Ann | The names share `ID`, but identify different roles |
| `Age - 39` with inner `Age > 35` | Ann | Non-column projection takes the helper's unconditional-true branch |
| An INTERSECT of two `SELECT Age ...` branches | Empty | Non-SelectQuery source takes the unconditional-true branch |

The helper returns true for unfamiliar shapes and treats any common name word as sufficient binding evidence. The positive Person_ID INTERSECT control passes and returns Bo/Cy. The wrong UNION control is rejected by existing alternative-scope handling, so this report does not claim that all set-query cases are accepted.

**Repair:** carry an explicit outer-operand to inner-projection/entity mapping, with aliases and quantifier scope. A shared word such as `ID` is evidence for schema linking, not proof of the requested relation. Unknown expression/set forms need an explicit supported witness rather than automatic acceptance; validate each relevant set branch and preserve correctly justified computed membership. A declared FK must still be aligned with the question's requested role.

**Tests:** Person_ID versus Manager_ID, same-type unrelated keys, real FK membership, aliased/self relations, legitimate calculated membership, wrong arithmetic projection, and correct/wrong INTERSECT/UNION/EXCEPT mappings. These probes inject human-authored typed counterexamples into the existing checker; they establish a verification defect, not that ordinary search selects all these bad candidates.

Evidence: [membership probes](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/extended-reproductions.json), [set-membership contrasts](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/set-membership-reproductions.json).

### R3 — Medium: shared ranking parser regresses cutoffs above 100

**Owner:** [sql_extrema.ranking_cutoff](C:/work/prereasoner-data/engine/sql_extrema.py:823), shared with base search and the contract.

On 150 people with distinct ages, `top 100` serves 100 rows. `top 101` was served as `LIMIT 101` by the previous engine, but now refuses with `I couldn't tell what “101” means in your sheets`. The new shared parser discards every cutoff above 100; the formerly broader base ordering path now inherits that bound.

**Repair:** read the stated positive integer independently of resource admission. Use the existing execution/output budget to enforce any actual supported limit, and report that limit explicitly if exceeded. Do not silently turn a valid cardinality into unknown wording. Preserve the distinction between a ranking cutoff, a numeric filter, and a number in a field name.

**Tests:** 100, 101, and larger bounded fixtures; explicit output-budget refusal; spelled counts; unrelated filter value 101; and field-name numbers. This is a reproduced coverage regression outside the current Spider questions, not a measured DEV strict loss.

Evidence: [previous engine](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/topn-before.json), [reviewed engine](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/further-reproductions.json).

### Remaining work and validation limits

Close the zero-count and membership-proof gaps before broad refusal relaxation, then recover complete, correct refused plans and missing construction families through the same planner. The old candidate census is still a diagnostic starting point; it was not refreshed in this review, and its raw eligibility/finalization caveats still apply.

Exploratory multiword-aggregate probes (`average engine temperature and average engine speed`) also refuse, but the corresponding one-cue control refuses too. Their cause was not isolated; they are not presented as a new regression caused by this fix. Add a schema/operand-binding diagnosis rather than another lexical exception. The [existing improvement guide](C:/work/prereasoner-data/release-review/sql-accuracy.md) remains the one implementation plan.

**Tests run on this commit:** SQL AST **205**, query contract **54**, compose **33**, enrichment **37**, deterministic emitters **64**, LLM client **10**: **403 passed, zero failures**. Python compilation and diff checks pass. Original and extended production-owner probes provide the semantic evidence above. This is not the full repository/live Postgres/deployed Chrome release gate, and the fresh headline uses the SQL backend rather than a complete Python-backend parity census.

Prediction latency: median **0.861 s**, p90 **1.863 s**, p95 **2.290 s**, maximum **5.296 s**. Workload conditions differ from the earlier review, and brief focused tests shared the machine; these numbers do not establish a controlled speedup.

### Reproducibility and evidence

Artifact directory: `C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011`. The isolated checkout remains available for this review's evidence; no production code, weights, generation, promotion, commit, or deployment was performed by this task.

- [Fresh full summary](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/full_eval_review-3ef82c4.json) and [all per-question predictions](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/full_eval_per_example_review-3ef82c4.json).
- [Final artifact verification](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/final-provenance-verification.json), [paired comparison](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/paired-comparison.json), and [changed-query analysis](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/review-analysis.json).
- [Sonnet regrading provenance](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/claude-sonnet-5-5-regraded.json) and [Gemini regrading provenance](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/gemini-3.8-flash-regraded.json).
- [Original-case replay](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/review-reproductions.json).

Runtime bundle SHA-256: `b11056f8ce8bf16a44da69bf76fea238149df439d7fde52d5e1f4747a5f536c7`; DEV SHA-256: `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4`. Complete database/source/schema/scorer fingerprints are in the saved summaries. All 1,034 reference question/database/gold identities match the current DEV inputs.

## Historical independent review of `3817b34`

**Status (2026-10-11, later):** The original F1–F5 counterexamples are fixed in their owners with regression tests; the broader follow-up above records remaining gaps. See `DECISIONS.md` "The review
of 2026-10-11" and the measured entry at the top of `spider/results/RESULTS.md` (DEV 382 strict, losing only
DEV 161/162 as predicted below; held out 246 → 249).

**Fresh full run verified:** 384/1,034 strict (37.1%), 442/1,034 lenient (42.7%), and 593/1,034 answered (57.4%). All 141 recorded artifact/provenance fields stayed unchanged. Claude's fixes improve the benchmark, but five implementation findings remain below.

## Scope and assessment

Reviewed source: `3817b34378d0133e272d75619d53bfe66a2e8e2c`, the clean checkout at the start of this request. The SQL changes include `29a97c3`, `12a5dc8`, `f9c9d10`, and `eed8d9d`; the subsequent spreadsheet wording and client changes are also present. This review extends the existing [accuracy review](C:/work/prereasoner-data/release-review/sql-accuracy.md).

Claude's changes address real implementation defects while preserving deterministic typed-AST construction. They improve comparison scope, exclusion identity, relationship direction, and grouping of distinct entities that share a name. They do not establish complete semantic correctness: this review reproduces two wrong answers through ordinary hermetic serving, an unsound subquery check, and aggregate/ranking construction gaps.

Production code and the promoted model bundle were left unchanged by this task to measure the reviewed implementation. Another session started editing the shared checkout during the first evaluation. That partial run was stopped and excluded. The replacement uses an isolated checkout of the reviewed commit; additional uncommitted edits are discussed separately below.

## Apples-to-apples accuracy comparison

The comparison uses all **1,034 Spider DEV questions**, the same question/database identities, the same 5,000-row-per-table inputs, independently executed gold answers, and the same repository scorer, [spider_eval.compare](C:/work/prereasoner-data/spider/probe/spider_eval.py:113). No system receives gold SQL or gold-selected tables as prediction input.

| System | Strict correct / all questions | Lenient correct / all questions | Answered / all questions | Generation status |
|---|---:|---:|---:|---|
| Deterministic engine, external rewriting off | 384/1,034 (37.1%) | 442/1,034 (42.7%) | 593/1,034 (57.4%) | Fresh complete run from the reviewed commit |
| Claude Sonnet 5.5 | 840/1,034 (81.2%) | 897/1,034 (86.8%) | 1,023/1,034 (98.9%) | October 7 SQL predictions re-executed and regraded today |
| Gemini 3.8 **Flash** | 872/1,034 (84.3%) | 904/1,034 (87.4%) | 1,034/1,034 (100.0%) | October 7 SQL predictions re-executed and regraded today |

The hosted references are SQL-generating baselines. They are distinct from the engine's optional Gemini **question-rewriting** fallback. No hosted generation was performed in this review, and regrading changed no saved correctness labels. The recovered records each contain all 1,034 unique indices; their questions, database IDs, and gold SQL match the current DEV file.

The comparison matches the task, rows, and scoring. It does not impose identical input presentation or a common inference budget: the hosted models received DDL, foreign-key information, and three sample rows per table; the engine inspects the capped tables through its existing serving path. Sonnet used the saved run's default non-thinking settings; Gemini used temperature 0 and LOW thinking. Results therefore describe these specific configurations, not each provider's maximum possible accuracy. The saved Gemini model is Flash, not Pro.

The engine trails Sonnet by **456 strict-correct answers / 44.1 percentage points** and Gemini Flash by **488 / 47.2 points**. Compared with the saved `own2-dirty` engine run, every selected SQL and correctness label is identical: zero wins, zero losses, zero SQL changes. Compared with `plan-base-de0cfba`, strict rises from 314 to 384: 70 wins, 0 losses, 314 unchanged-correct and 650 unchanged-wrong; 127 served SQL strings change. These are frozen-label benchmark transitions; the gold-label caveat below still applies.

### Paired results on the identical questions

| Strict outcome across 1,034 questions | Engine vs Sonnet | Engine vs Gemini Flash |
|---|---:|---:|
| Both correct | 355 | 362 |
| Only engine correct | 29 | 22 |
| Only hosted model correct | 485 | 510 |
| Neither correct | 165 | 140 |

These are per-question joins, not differences inferred from aggregate totals. The hosted-only counts exceed the net advantage because the engine also wins some questions. All indices are in the [paired comparison](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/paired-comparison.json).

### Coverage versus answer reliability

| System | Answered denominator | Strict among its answers | Lenient among its answers |
|---|---:|---:|---:|
| Engine | 593 | 384/593 (64.8%) | 442/593 (74.5%) |
| Sonnet 5.5 | 1,023 | 840/1,023 (82.1%) | 897/1,023 (87.7%) |
| Gemini 3.8 Flash | 1,034 | 872/1,034 (84.3%) | 904/1,034 (87.4%) |

Different answered denominators can obscure difficulty. On the **same 593 questions answered by the engine**, strict accuracy is 384/593 (64.8%) for the engine, **495/593 (83.5%)** for Sonnet, and **506/593 (85.3%)** for Gemini. Thus the gap includes weaker returned answers, not only refusals.

On the engine's 441 unanswered questions, Sonnet is strict-correct on 345 and Gemini on 366. The net strict gaps decompose into 345 + 111 = 456 for Sonnet and 366 + 122 = 488 for Gemini: about three quarters of each gap comes from questions the engine does not answer. Recovering those answers must preserve the semantic checks; F4/F5 show why simply allowing more candidates is insufficient.

### Current engine failure partition

| Outcome | Count / 1,034 |
|---|---:|
| Strict-correct | 384 (37.1%) |
| Answered, lenient-correct but strict-incorrect | 77 (7.4%) |
| Answered, incorrect under both metrics | 132 (12.8%) |
| Unanswered | 441 (42.6%) |

Among the 593 answers, 209 are strict-incorrect. The 441 unanswered cases are **365 completeness refusals, 67 constraint refusals, and 9 AST-search failures**. None is a dropped soft-budget timeout in this run. Nineteen strict-correct empty-gold cases are lenient-false, explaining why 384 + 77 does not equal the lenient total 442.

| Difficulty | Questions | Engine strict | Engine answered |
|---|---:|---:|---:|
| Easy | 248 | 133/248 (53.6%) | 163 |
| Medium | 446 | 172/446 (38.6%) | 260 |
| Hard | 174 | 53/174 (30.5%) | 101 |
| Extra hard | 166 | 26/166 (15.7%) | 69 |

### What the metrics mean

- **Strict** requires equality of normalized row multisets, including duplicate multiplicity. It ignores row order, lowercases text, and rounds numbers to three decimal places. It is the repository's denotation metric, not official Spider SQL exact-match or test-suite accuracy.
- **Lenient** requires every normalized gold cell value to appear somewhere in the prediction. It can accept extra rows, extra columns, and wrong associations between values. It is useful diagnostically and does not prove a fully correct answer.
- **Answered** means prediction/execution succeeded under the evaluator contract. A successful empty answer is answered; a refusal or failed prediction is unanswered. These still count as failures in all-question accuracy.
- **Among answered** changes the denominator to answered questions. It measures reliability conditional on returning an answer and must be shown together with coverage.
- The scorer reports empty-gold results as lenient-false even when strict-correct. Thus lenient-correct is not a perfect superset of strict-correct.
- The existing scalar-gold metric checks whether the gold scalar value appears in the prediction's flattened values; it does not require a scalar prediction of exactly that shape.

### Verified gold-label problem: counting countries

DEV 161 and 162 ask for the number of countries with more than two car makers. Their gold SQL instead returns the maker count inside each qualifying country. Re-executing it on the shared inputs produces `[[4], [4], [3], [5]]`. There are four qualifying countries, and Gemini's outer-count query returns `[[4]]`, the requested country count. The unchanged scorer marks Gemini strict-incorrect and the engine's inner-count query strict-correct.

These two cases contribute to the engine's apparent wins over Gemini, even though the engine repeats the unit error described in F2. They also illustrate why a strict gain is a benchmark gain, not automatic proof of improved question semantics. The [executed label checks](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/gold-count-unit-cases.json) record both results.

The headline comparison retains these gold labels for every system; it has not selectively regraded the engine. Fixing F2 can lose these two benchmark matches while improving the actual requested computation. Track that loss transparently alongside independently derived semantic regressions, rather than teaching the planner to return the wrong output unit.

## Confirmed remaining findings

### F1 — High: a repeated foreign key or repeated NULLs can make an exclusion remove the wrong entity

**Owners:** [SchemaGraph.identifies](C:/work/prereasoner-data/engine/sql_schema.py:224), [identifying_column](C:/work/prereasoner-data/engine/sql_schema.py:241), [difference expansion](C:/work/prereasoner-data/engine/sql_extrema.py:545), and [recursive exclusion](C:/work/prereasoner-data/engine/sql_recursive.py:226).

The new exclusion fix correctly uses the student's identity when the requested `(Major, Age)` values repeat. Its identity helper nevertheless returns true when a column references a unique column in another table. A unique `Majors.MajorID` proves that a major is identifiable; it does not prove that each student has a different major. The same helper removes NULLs before testing uniqueness, so a column containing multiple NULLs and one non-null value also appears identifying.

**Ordinary serving reproduction:**

| Student ID | Name | Major | Age | Pet |
|---:|---|---:|---:|---|
| 1 | Ann | 600 | 19 | cat |
| 2 | Bo | 600 | 19 | none |
| 3 | Cy | 520 | 20 | dog |

Ask: `Find the major and age of students who do not have a cat pet.` Without a `Majors` lookup table, the new regression passes and returns `(600, 19)` and `(520, 20)`. Add `Majors(MajorID)` with values 600 and 520 and a legitimate `Student.Major -> Majors.MajorID` foreign key. The same serving path switches to projected-value `EXCEPT`, returns only `(520, 20)`, and reports a valid answer. Bo is incorrectly removed because Ann shares the projected values.

Changing Ann and Bo's major to NULL reproduces the same population loss without the lookup table: Bo's `(NULL, 19)` is removed. The fixture's text affinity also renders Cy's major as a string; the defect is the missing Bo row, independently of numeric normalization.

**Correction:** distinguish the identity of the source row from the identity of an entity referenced by that row. Do not infer a source-row key merely from the uniqueness of a foreign-key target. Require complete, non-null local key evidence for operations that need row identity; prefer a declared key where available. Keep entity exclusion on that key unless the requested result explicitly concerns distinct projected values. Treat nullable anti-membership carefully: use a non-null key or a correctly correlated anti-existence operation rather than assuming `NOT IN` has null-safe semantics.

**Regression cases:** repeated local foreign key to a unique lookup key; several NULLs; a genuinely unique non-null key; composite identity; duplicate display names; an explicitly distinct projected-value exclusion; and ordinary serving with the extra lookup table. The existing fixture without a lookup table is a necessary positive case, but does not cover the defect.

### F2 — High: counting qualifying entities returns the inner group's count

**Owners:** [ConstraintQueryExpander._count_having_candidates](C:/work/prereasoner-data/engine/sql_constraints.py:86), its selection/projection helpers, and the count/comparison contract in [query_contract.py](C:/work/prereasoner-data/engine/query_contract.py).

Use two countries, USA and Japan. USA has two car makers and Japan has one. Ask: `How many countries have more than 1 car maker?` The expected answer is **1 country**. Ordinary hermetic serving returns **2**, marks coverage complete, and serves this shape:

```sql
SELECT COUNT(*)
FROM car_makers
JOIN countries ON car_makers.Country = countries.CountryId
GROUP BY countries.CountryName
HAVING COUNT(*) > 1;
```

This query counts makers inside each qualifying country. It does not count the qualifying countries. The existing contract regression also considers the unjoined version of this query to have no violations; that expectation accepts the wrong output unit.

**Correction:** represent the two count roles separately: the inner count tests the related makers per country; the outer count counts qualifying countries. Construct the qualifying entity-key relation using the existing typed AST, then count its rows, or use an equivalent typed membership predicate on countries. Group by entity identity and choose the counted relation's identity when joins can multiply rows. Extend the existing expander and contract rather than adding another planner.

**Regression cases:** zero, one, and several qualifying countries; `Which countries ...?` versus `How many countries ...?`; `How many makers per country?`; countries sharing a display name; duplicate join paths; zero related rows; and distinct versus ordinary count. Reject a per-country count as satisfying an outer country count.

**Benchmark caveat:** DEV 161/162 contain this same output-unit mistake in their gold SQL. Those two frozen benchmark labels must not become serving rules; see the verified label checks above.

### F3 — Medium: a condition inside IN/EXISTS is accepted without proving that it filters the returned entity

**Owners:** [_tested_subqueries](C:/work/prereasoner-data/engine/query_contract.py), [_select_queries](C:/work/prereasoner-data/engine/query_contract.py), and [_scoped_match](C:/work/prereasoner-data/engine/query_contract.py).

The new scope record distinguishes UNION alternatives, negation, and scalar subqueries. That fixes the previously reported UNION/EXCEPT/scalar examples. It does not retain the membership mapping or correlation between a nested condition and the returned row. Consequently an inner age condition is accepted even when it places no age condition on the outer person.

For people Ann aged 20, Bo aged 40, and Cy aged 60, ask `List names of people whose age is above 35.` Both contrasting ASTs are marked complete:

```sql
-- Returns all three people, because some person is older than 35.
SELECT Name FROM people
WHERE EXISTS (SELECT ID FROM people WHERE Age > 35);

-- Returns no people: person IDs are incorrectly tested against age values.
SELECT Name FROM people
WHERE ID IN (SELECT Age FROM people WHERE Age > 35);
```

The expected answer is Bo and Cy. The positive control `ID IN (SELECT ID ... WHERE Age > 35)` is correctly accepted and executed. These checks exercise production coverage and execute the ASTs, but do not establish that ordinary search currently chooses these two bad candidates. This is a demonstrated acceptance defect, distinct from F1/F2's ordinary serving failures.

**Correction:** carry entity identity, aliases/scope, projected membership keys, and correlation witnesses through the existing contract traversal. An IN condition discharges a returned-entity requirement only when its outer operand and inner projection establish the appropriate identity/relation mapping. An EXISTS condition needs a relevant correlation; an uncorrelated global existence test cannot prove the outer row's age. Preserve the existing rules for explicit alternatives and negated related-entity predicates.

**Regression cases:** correct key membership; wrong projected operand; unrelated but same-typed keys; correlated versus uncorrelated EXISTS; aliases and self-relations; negation; AND versus OR; and a correct condition inside a scalar subquery that does not filter the outer row.

### F4 — Medium: repeated aggregate words still collapse distinct requested computations

**Owner:** [aggregate cue deduplication](C:/work/prereasoner-data/engine/sql_search.py:709) and [_coordinated_targets](C:/work/prereasoner-data/engine/sql_search.py:1615).

The new rule constructs two aggregates for `average distance and price`. It still collapses repeated functions to their earliest cue. The coordination helper permits only articles and `and` between operands, so the second explicit `average` prevents that repair from applying.

With Los Angeles flights having `(distance, price)` values `(100, 50)` and `(300, 70)`, the first wording returns `(200, 60)` correctly. `What is the average distance and average price for flights from Los Angeles?` is refused. Its error misleadingly suggests that price contains nonnumeric/error cells, although every price is numeric. The actual problem is missing construction of the second aggregate.

**Correction:** preserve aggregate occurrences together with their bound operands and phrase scopes. Deduplicate equivalent function-plus-operand requests, not every occurrence of the same function. Keep the existing one-word coordination rule and support separate explicitly coordinated occurrences through the same owner. Avoid consuming coordinated filter values as aggregate operands.

**Regression cases:** shared versus repeated aggregate word; two and three operands; repeated wording for the same operand; distinct aggregate functions; separate filters/scopes; and field names that themselves contain an aggregate word. Test both candidate construction and the final served answer. Report a construction failure separately from malformed numeric input.

### F5 — Medium: validating a spelled ranking cutoff does not yet construct its plan

**Owners:** [base ordering/cutoff construction](C:/work/prereasoner-data/engine/sql_search.py:1518) and [extrema cutoff parsing](C:/work/prereasoner-data/engine/sql_extrema.py:800), consumed by the shared question contract.

The previous review's wrong `LIMIT 3` candidate for `the two people ... excluding Person_ID 2` is now correctly rejected, and a supplied correct `LIMIT 2` candidate is accepted. However ordinary hermetic serving still refuses `List the names of the two people with the largest Age.` With the five-person fixture, Eve aged 60 and Dan aged 50 should be returned. The search reports three candidates but selects none. Adding a numeric-ID exclusion (2, 3, or 5) or excluding Spain is also refused; the numeric-exclusion versions report five candidates and `planner: no executable AST candidate`.

The [raw candidate pool](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/topn-pool.json) confirms the construction gap: it contains `LIMIT 1` and a scalar maximum, but no requested `LIMIT 2` plan. With the numeric-ID exclusion, additional candidates mistakenly compare `Age = 2` and maximize Person_ID; the corrected gate refuses all of them.

This is the still-open construction side of the earlier finding, not a claim that all top-N queries fail. The [cutoff/exclusion contrasts](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/topn-exclusion-contrasts.json) and [positive supplied-candidate check](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/previous-review-replay.json) separate generation from verification.

The extrema helper only adopts a number within two tokens of its cue or after `top/bottom`; the base ordering path defaults these superlatives to one. The expanded `two people with the largest Age` phrase therefore loses its cutoff during construction.

**Correction:** have both existing construction callers consume the same bound output-cardinality reading that validates the cutoff. Recognize the ranked entity phrase across its ordinary connecting words, and keep the cutoff separate from an independent filter's number. Build the supported typed order/limit/filter combination; do not weaken the now-correct rejection of a wrong cutoff or add another independent cutoff parser.

**Regression cases:** spelled and numeric counts; short and expanded noun phrases; with and without exclusions; the same number serving a cutoff and a filter; different numbers in those roles; an explicit correct supplied candidate; and ordinary serving. Retain the repository's established tie policy and document any deliberate difference from a gold `LIMIT`.

## Fixes that hold up in review

The October 8 review's 11 original probes were replayed on this commit. The majority/all-malformed amount cases now refuse rather than substituting world population; wrong alphabetic field/direction, wrong spelled-number cutoff, and swapped departure operand are rejected. Both alphabetic wording forms serve ascending Name order, and departure/arrival requests serve their corresponding airport field. The correct supplied `LIMIT 2` candidate is accepted; ordinary construction remains open as F5 above. Evidence: [previous-review replay](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/previous-review-replay.json).

| Claude change | Evidence | Remaining limit |
|---|---|---|
| Comparisons checked in their query scope | New UNION/EXCEPT/scalar contrasts pass | F3: membership/correlation mapping still missing |
| Nearest numeric cue and negated comparative bounds | Registered contract/planner regressions pass | Does not by itself establish the entity or output unit |
| Entity-key exclusions | Original repeated `(Major, Age)` fixture passes | F1: foreign-key and NULL identity evidence is unsound |
| One aggregate word over two fields | Shared-word two-operand fixture passes | F4: repeated aggregate words are still collapsed |
| Owner versus target side of a relation | Relationship-direction contrasts pass | A soft ranking rule is not a general relation witness |
| Group entities separately when names repeat | Two Jordans stay separate; category contrasts pass | Identity/grain must remain tied to the requested entity |
| Spreadsheet total/ranking and average-per-row wording | Covered owner-question tests pass | F2 shows that nested count roles remain unresolved |
| Bounded chat-round timeout | LLM client regressions pass | No new hosted-latency or deployed-chat test was run |
| Compose refusals follow shared routing | The evaluator now passes refusal to `route` and handles `Route.CLARIFY` | No independently rerun deployed-world/HTTP gate |

The additional uncommitted edits seen during evaluation extend `most/fewest` row nouns for a one-table workbook and change chat-deletion batching. The newly registered workbook regression passes: most/fewest leads uses row counts, most employees remains a measure order, and an unknown noun with multiple tables still asks for clarification. These edits are outside the frozen benchmark source; this review does not label that moving working tree fully evaluated. The client deletion loop already repeats pages until the list is empty, so its page limit does not alone imply that older chats are left behind.

## Follow-up priorities within the existing accuracy design

1. **Fix the served population/unit defects first:** F1 and F2 return wrong answers while claiming completeness. Extend the existing schema, AST expander, and request contract; add the positive and contrasting regressions above.
2. **Make scope checks carry semantic witnesses:** F3 demonstrates why finding an operator somewhere in the AST is insufficient. The witness must bind operator, operand, entity, grain, and scope to the requested result.
3. **Recover provably answerable refusals:** separate missing named readings from constraint failures and missing plans. F4/F5 show where relaxing the gate would conceal a construction failure; build the requested aggregate and ranking plans instead.
4. **Measure construction and selection separately:** refresh the existing evaluation-only pool census through the same serving transformations. Rank only candidates that satisfy the corrected contract; use deterministic named ranking features and stable tie-breaks.
5. **Validate generalization:** run the established held-out database sample and workbook contrasts for each coherent fix. Preserve the scorer and freeze code/bundle/input hashes. Record strict wins, losses, newly answered correctness, and coverage; do not treat a coverage increase alone as accuracy improvement.

The latest recorded ledger has 384 served-correct questions; 62 served-wrong with a correct eligible candidate; 26 served-wrong with a correct candidate rejected by a check; 121 served-wrong with no correct candidate built; 199 refused with a correct candidate built; and 242 refused with none built. Its raw pool contains a correct candidate for 671 questions (64.9%) and a violation-free one for 618 (59.8%). These are previous census diagnostics, not measurements rerun in this task or certified serving ceilings: final tie transformations can differ. They nevertheless show why selection changes alone are unlikely to close the gap to 840/872 strict-correct hosted answers. Construction must improve alongside verification and ranking.

No SQL-generating model is proposed for production. The hosted SQL outputs remain benchmark references; every engine query in the fresh run is built by the existing deterministic AST owner.

### Performance opportunities to measure after correctness fixes

`SchemaGraph.repeats` already memoizes row-duplication checks, but `identifies` repeatedly scans column values and foreign-key endpoints, and `identifying_column` repeats that work across candidates. Once the identity semantics in F1 are corrected, cache non-nullness/uniqueness and local identifying-key facts on the existing request-scoped graph. Invalidate them with that graph's input; do not carry observed uniqueness across changed data. This can remove repeated work without reducing the candidate pool or weakening the contract.

The scope correction should similarly build one candidate's semantic traversal and reuse it for its existing reading/constraint checks, rather than traversing every nested SELECT again for each graded word. Profile these changes with the existing `engine/request_timing.py` collector, including self-time and uninstrumented time, and stratify by schema width and candidate count. These are code-inspection opportunities, not measured speed gains from this task. The concurrent CPU-thread probe produced identical SQL/grades on 25 questions but was slower, so the original thread setting was retained; it is not evidence for changing serving defaults.

## Validation and provenance

Focused checks completed on the reviewed source before the later edits:

| Check | Result |
|---|---:|
| `python -m tests.test_sql_ast` | 200 passed, 0 failed |
| `python -m tests.test_query_contract` | 51 registered tests passed |
| `python -m tests.test_compose` | 33 passed, 0 failed |
| `python -m tests.test_enrichment` | 37 passed, 0 failed |
| `python -m tests.test_deterministic_emitters` | 64 passed, 0 failed |
| `python -m tests.test_llm` | 10 passed, 0 failed |
| Compilation of engine/db/training/tests/orchestrator/mcp_server | Passed |

Additional contrasting checks use the production planner/coverage owners with the repository's encoder-free fixture. They are **not** a second implementation of serving or a substitute for the full model-backed Spider run. The [reproduction script](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/review_reproductions.py) and [executed findings](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/review-reproductions.json) are retained as disposable review evidence.

A repository-wide suite, live PostgreSQL/world tests, deployed Chrome demo gate, new hosted generation, training, model promotion, commit, push, and deployment were not performed. This is an accuracy review, not release approval. Latency on this shared desktop is not a controlled engine-versus-hosted or before-versus-after performance comparison.

Hosted regraded summaries: [Sonnet](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-20261011/claude-sonnet-5-5-regraded.json), [Gemini Flash](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-20261011/gemini-3.8-flash-regraded.json). Each includes source prediction path/hash and dataset/scorer hashes. Per-example files are beside them.

| Input | SHA-256 |
|---|---|
| DEV questions | `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4` |
| Shared scorer | `5b35c1fbf12014e4cca3b698edc72499828ea2d732936d593692233a53f6cd66` |
| Runtime bundle | `b11056f8ce8bf16a44da69bf76fea238149df439d7fde52d5e1f4747a5f536c7` |
| Saved Sonnet predictions | `c240d529aa55e4e4171ba83de557f2b9bfd9cc2b15a0487ecac7e66f5c41006b` |
| Saved Gemini Flash predictions | `7bbf9bf1e2489dd3112d369f2402f321290b7735cbd5006e7e7ec1c33f019df4` |

The [fresh engine summary](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/full_eval_review-3817b34-isolated.json), [all per-example predictions](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/full_eval_per_example_review-3817b34-isolated.json), and [final provenance check](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/final-provenance-verification.json) are retained with the review. The check confirms all 1,034 unique indices, external rewriting off, clean reviewed source, and **zero differences across 141 artifact/provenance fields**, including the model bundle and database inputs. The fresh engine latency was median 2.667 s, p90 5.329 s, maximum 9.229 s; shared concurrent CPU work prevents a controlled speed comparison.

Command, run in the isolated checkout with `EXTERNAL_LLM_ENABLED=0` and `PREREASONER_DATA_DIR=C:/work/prereasoner-data/engine/data`:

```powershell
python -u -m spider.probe.full_eval --data C:/work/prereasoner-data/spider/data --dbs C:/work/prereasoner-data/spider/data/dbs --config whole_db --selection served --backend sql --cap 5000 --timeout 12 --out C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011 --tag review-3817b34-isolated --checkpoint-every 25
```

The interrupted first run has an explicit [aborted-run note](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-20261011/ABORTED-ENGINE-RUN.md) and contributes no engine accuracy figure. The 25-question one-thread probe is separately labeled as partial and contributes no headline result.

## Repository state at completion

This task changes only this report and the measured-results entry in `spider/results/RESULTS.md`. The primary checkout's other modifications were made by another session during this review and are preserved. `git diff --check` passes. No production code was edited by this task.

```text
 M DECISIONS.md
 M docs/ARCHITECTURE.md
 M engine/admin.py
 M engine/closed_class.py
 M engine/query_contract.py
 M engine/sql_expansion.py
 M spider/results/RESULTS.md                         # this task
 M tests/test_conversations.py
 M tests/test_sql_ast.py
 M web/public/lib/workbook-conversations.js
 M web/tests/browser/release-flow.spec.js
?? release-review/spider-review-20261011.md         # this task
```

The isolated review checkout remains attached at `C:/Users/manid/.codex/worktrees/spider-review-20261011/prereasoner-data` for replay. The app refused its archive because it is protected by a pinned task or workspace; no checkout files were removed. The benchmark JSON, per-question comparison, and reproduction results remain in the linked review-artifact directories.
