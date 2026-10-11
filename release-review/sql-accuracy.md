# SQL accuracy: measured weaknesses and implementation plan

**Updated:** 2026-10-11. This updates the existing guide after the independent fix review and full Spider evaluation. It supersedes the October 8 baseline and ordering of work in this document.

**Later follow-up at `3ef82c4`:** [the latest independent review](C:/work/prereasoner-data/release-review/spider-review-20261011.md) verifies **382/1,034 strict (36.9%), 440/1,034 lenient (42.6%), 591/1,034 answered (57.2%)** and 403 passing focused checks. The original F1–F5 examples below are repaired; broader zero-related-count and membership-proof gaps remain, and top-N above 100 regresses. The two strict losses versus this guide's starting baseline correct country-count gold-label defects. Treat the detailed `3817b34` numbers below as historical baseline evidence, and use the follow-up for current measurements. The implementation objective and single-owner plan remain in force.

**Measured source:** `3817b34378d0133e272d75619d53bfe66a2e8e2c`, in an isolated, clean checkout. Additional edits in the primary checkout are outside this measurement.

**Scope:** improve the existing deterministic, interpretable own-data engine. This update changes documentation only. Implementation, training, promotion, paid model generation, and deployment were not performed for this plan. Repository rules remain in [CLAUDE.md](C:/work/prereasoner-data/CLAUDE.md); architecture facts and decisions retain their existing owners.

## 1. Recommendation

**Make safe recovery of unanswered questions the main accuracy program. First fix the confirmed false acceptances involving entity identity, count grain, and subquery scope. Then recover valid refused candidates, improve selection, and construct the missing semantic families.** Optimize strict correctness across all questions, with independent semantic tests and coverage reported alongside it.

The implementation order is:

1. Correct the known wrong-answer families so that broader coverage has a sounder admission contract.
2. Recover unanswered questions for which an independently correct candidate already exists, by fixing the specific interpretation or verification defect.
3. Correct selection when a complete, admissible answer is available but loses to an incorrect alternative.
4. Expand deterministic construction for questions whose correct plan is absent, including both refused and incorrectly answered questions.
5. Address remaining output shape, duplicate, ordering, and tie issues according to the requested semantics.

The reason to emphasize unanswered is measured: about three quarters of the net strict gap to the matched hosted references occurs on the engine's unanswered questions. The reason to start with correctness hardening is also measured: ordinary serving can currently return wrong populations and wrong count units while declaring the query valid and complete.

Typed AST construction is compatible with higher accuracy than a next-token SQL generator. It gives us explicit structure, constraints, and repeatable search; it does not supply correct language interpretation automatically. The present deficit lies in binding words to operands, entities, populations, scopes, and output cardinality, then constructing and choosing the correct AST. Beating the references remains an engineering hypothesis to demonstrate, not a promised property of using ASTs.

## 2. Verified results and their meaning

### 2.1 Same-question comparison

All rows below use **the same 1,034 Spider DEV questions**, database snapshots capped at 5,000 rows per table, independently executed gold answers, and the repository's unchanged scorer. The deterministic engine uses `whole_db`, `served`, SQL backend, compose enabled, and `EXTERNAL_LLM_ENABLED=0`.

| System | Strict correct / all | Lenient correct / all | Answered / all |
|---|---:|---:|---:|
| Deterministic engine | **384/1,034 (37.1%)** | **442/1,034 (42.7%)** | **593/1,034 (57.4%)** |
| Claude Sonnet 5.5 | 840/1,034 (81.2%) | 897/1,034 (86.8%) | 1,023/1,034 (98.9%) |
| Gemini 3.8 Flash | 872/1,034 (84.3%) | 904/1,034 (87.4%) | 1,034/1,034 (100.0%) |

The engine run is fresh and complete. Hosted SQL predictions were generated October 7 and re-executed/regraded October 11; no new hosted generation occurred. Gemini here is **Flash**, not Pro. The comparison matches questions, database rows, and scoring, but input presentation and inference budgets differ: hosted models received DDL, foreign keys, and three sample rows per table; the engine inspects the capped tables. Sonnet used saved non-thinking settings; Gemini used temperature 0 and LOW thinking.

Among the engine's own 593 answered questions, correctness is **384/593 = 64.8% strict** and **442/593 = 74.5% lenient**. These conditional metrics describe the reliability of returned answers. They do not replace the all-question figures, which also count refusals as failures.

On those **same 593 questions**, Sonnet is strict-correct on 495 (83.5%) and Gemini on 506 (85.3%). On the engine's 441 unanswered questions, Sonnet is strict-correct on 345 and Gemini on 366. Thus:

| Reference | Strict gap | Contribution on engine-unanswered | Net contribution on engine-answered |
|---|---:|---:|---:|
| Sonnet | 840 - 384 = 456 | 345 (75.7% of gap) | 495 - 384 = 111 |
| Gemini Flash | 872 - 384 = 488 | 366 (75.0% of gap) | 506 - 384 = 122 |

The gap therefore combines missing coverage and weaker answers. A coverage-only solution cannot eliminate it.

### 2.2 Disjoint outcome buckets

| Outcome | Questions | Interpretation |
|---|---:|---|
| Strict-correct | 384 | Matches the frozen scorer; independent semantic checks still matter |
| Answered, lenient-correct and strict-incorrect | 77 | Required values appear, but the complete result differs |
| Answered, incorrect under both | 132 | Returned answer fails both checks |
| Unanswered | 441 | No successful served prediction |
| **Total** | **1,034** | Each question appears exactly once |

There are **209 strict-incorrect returned answers = 77 + 132**. The unanswered stages are **365 completeness refusals, 67 constraint refusals, and 9 AST-search failures**. There were no soft prediction-budget exceedances in this run. These are final-stage labels, not proof that the refusal gate itself caused each failure: a completeness refusal can reflect a genuinely missing required operation.

"Lenient-incorrect" is not a separate priority bucket. There are 151 lenient-incorrect returned answers, but **19 are strict-correct empty-gold cases** that the lenient comparator marks false. The remaining 132 fail both metrics. Do not spend engineering effort turning those 19 correct empty answers into nonempty answers to satisfy lenient scoring.

The 77 lenient-only answers are also not necessarily cosmetic. Extra entities, wrong row associations, wrong grain, or extra rows can contain all gold cell values while being semantically incorrect. Classify their actual failure before repairing output shape.

### 2.3 What the scorer establishes

- **Strict:** equality of normalized row multisets, preserving duplicate multiplicity and column positions. It ignores row order, lowercases text, and rounds numbers to three decimal places. This is the repository's denotation metric, not official Spider SQL exact-match or test-suite accuracy.
- **Lenient:** every normalized gold cell value appears somewhere in the prediction. Extra rows, columns, and incorrect associations can pass. Empty gold is lenient-false even when strict-correct.
- **Answered:** a served prediction executes successfully under the evaluator contract, including a successful empty result. Refusals and failed predictions are unanswered.
- The reported scalar-gold value-match is 214/408 (52.5%); it checks flattened value presence, not an exactly one-cell scalar result.

Do not describe strict and lenient as mathematical lower and upper bounds on true semantic accuracy. Their normalization and the quality of gold labels can miss or reward errors.

### 2.4 Tests, history, and provenance

The reviewed source passed **395 focused checks**: SQL AST 200, query contract 51, compose 33, enrichment 37, deterministic emitters 64, and LLM client 10. Python compilation and diff whitespace checks passed. Additional negative probes reproduced the defects in section 4 despite those existing tests passing. Passing the current suite therefore does not establish that the engine is fully correct.

The fresh run exactly matches the saved `own2-dirty` predictions: no selected-SQL or grade changes across all 1,034 questions. Relative to `plan-base-de0cfba`, strict improves 314 → 384: 70 wins, 0 losses, 314 unchanged-correct, and 650 unchanged-wrong. These are benchmark-label changes, not a causal ablation of one fix or proof of semantic correctness for every win.

The full repository/live Postgres/browser release gates were not run in this review. Later uncommitted primary-checkout edits are not covered by this frozen score. No current-working-tree or deployment accuracy claim should reuse 37.1% without a fresh run after those edits.

Canonical measured history: [RESULTS.md](C:/work/prereasoner-data/spider/results/RESULTS.md). Detailed findings and experiment procedure: [October 11 fix review](C:/work/prereasoner-data/release-review/spider-review-20261011.md).

| Provenance | Value / evidence |
|---|---|
| Source commit | `3817b34378d0133e272d75619d53bfe66a2e8e2c` |
| Model bundle SHA-256 | `b11056f8ce8bf16a44da69bf76fea238149df439d7fde52d5e1f4747a5f536c7` |
| DEV SHA-256 | `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4` |
| Engine summary | [Complete summary](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/full_eval_review-3817b34-isolated.json) |
| Predictions and grades | [All per-question records](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/full_eval_per_example_review-3817b34-isolated.json) |
| Matched reference subsets | [Paired comparison](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/paired-comparison.json) |
| Artifact stability | [Verification: all 141 recorded provenance fields unchanged](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/final-provenance-verification.json) |

These scratch artifacts support reproducibility while available; keep their provenance and measured summaries in the existing results record, rather than introducing a competing evaluator or committing generated predictions without authorization.

## 3. Where to spend effort

### 3.1 Candidate availability explains which fix can help

Claude's recorded pool census in [RESULTS.md](C:/work/prereasoner-data/spider/results/RESULTS.md:124) partitions the same 384-correct outcome as follows:

| Recorded condition | Questions | Appropriate intervention |
|---|---:|---|
| Served strict-correct | 384 | Preserve and add distinguishing semantic tests |
| Served wrong, correct candidate recorded eligible | 62 | Selection/binding evidence; check final realization first |
| Served wrong, correct candidate blocked by a check | 26 | Diagnose the rejected obligation; repair its verifier or reading |
| Served wrong, no correct candidate built | 121 | Construction or earlier binding/retention |
| Refused, correct candidate built | 199 | Safe coverage recovery after validating candidate meaning |
| Refused, no correct candidate built | 242 | Construction or earlier binding/retention |

**Evidence qualification:** this census was recorded by Claude and was not independently rerun in the October 11 review. The fresh top-1 score is independently reproduced. The raw pool diagnostic does not apply serving's `keep_ties` realization to every candidate, and its eligibility vocabulary is not equivalent to a complete serving admission proof. Refresh it through the existing owner before treating these counts as certified recoverable answers or a serving ceiling.

Still, it gives a useful work order:

- **225 check-blocked opportunities = 26 + 199**, subject to semantic validation. The recorded blockers are unread wording in 162 cases and constraints in 63, including 32 "requested field is not used" cases. A field used correctly inside a membership scope differs from a field merely present in an unrelated subquery.
- **62 selection opportunities**, subject to finalization and complete-contract checks. More candidate construction is not necessarily required for these cases.
- **363 no-correct-candidate cases = 121 + 242**. Refusal relaxation or better ranking alone cannot fix them. First distinguish interpretation, missing expansion, and pruning losses.
- The raw pool contains a scorer-correct candidate for **671/1,034 (64.9%)**, with 618 recorded violation-free. That is evidence of construction headroom and a construction deficit; it is not a certified achievable served score.

Even selecting all 671 observed raw successes would fall below Gemini's 872. Reaching at least 873 would require at least 202 additional question successes beyond that observed raw set, assuming comparable candidate realization. This is arithmetic to show the scale of the construction work, not a forecast or a proven ceiling.

### 3.2 Priority rule

First remove demonstrably harmful false acceptances. Then rank failure families by independently validated correct answers recoverable per unit of implementation and runtime cost, giving preference to reusable bindings that improve several families. Include both refused and incorrectly answered examples for each family.

I would not allocate a fixed percentage of effort to the three user-facing labels. They overlap and conceal different causes. The operational ordering is **semantic soundness → false refusals with correct plans → selection → missing plans**, reassessed after each measured batch. Construction must continue alongside coverage recovery once shared binding infrastructure is sound; it is necessary to close the full gap.

## 4. Confirmed defects and concrete repairs

These are reproduced on the frozen source. Existing later edits need their own verification. Fix each family in its production owner with positive, contrastive, and negative cases; no benchmark-specific table names or question-ID rules belong in serving.

### 4.1 Entity identity and NULL-safe exclusion — first correctness fix

**Owners:** [sql_schema.py](C:/work/prereasoner-data/engine/sql_schema.py), [sql_extrema.py](C:/work/prereasoner-data/engine/sql_extrema.py), and [sql_recursive.py](C:/work/prereasoner-data/engine/sql_recursive.py).

**Observed:** two students share `(Major, Age)` but only one owns a cat. Adding a legitimate major lookup table makes the planner accept projected-value `EXCEPT`, removing both students' shared values. Repeated NULLs reproduce the loss without the lookup. Ordinary serving returns a valid but wrong result.

**Cause:** `SchemaGraph.identifies` treats a local foreign key as identifying when its referenced target is unique, and discards NULLs when checking uniqueness. A unique major identifies a major, not the student referencing it.

**Implementation:**

1. Make identity checks explicit about the requested entity and source population. Separate local row identity from referenced-entity identity in the existing schema owner.
2. Use declared local keys when ingestion exposes them. If metadata is absent, label observed complete non-null uniqueness as snapshot evidence, not a schema guarantee. Preserve composite keys as tuples; do not accept one unique-looking component as a composite relationship.
3. A foreign-key endpoint can witness the identity of the referenced entity only when that is the question's entity. It cannot establish uniqueness of referring rows.
4. Exclude entities on their identity before projecting display values. Use null-safe, correctly correlated anti-existence where supported, or a proven non-null membership key. `NOT IN` with nullable values is not a universal replacement.
5. Permit projected-value set difference when distinct values are actually requested; preserve the distinction from excluding source entities.

**Acceptance:** correct ordinary serving with/without the lookup table; repeated foreign keys, repeated NULLs, true local keys, duplicate names, composite keys, and explicit value-set exclusion. Adding unrelated metadata must not remove an otherwise eligible student. Audit every caller of `identifies`/`identifying_column`, not just the reproducing expander.

### 4.2 Count units and nested grain — first correctness fix

**Owners:** [sql_constraints.py](C:/work/prereasoner-data/engine/sql_constraints.py), [sql_expansion.py](C:/work/prereasoner-data/engine/sql_expansion.py), and [query_contract.py](C:/work/prereasoner-data/engine/query_contract.py).

**Observed:** with USA having two makers and Japan one, "How many countries have more than 1 car maker?" returns 2 instead of 1. `_count_having_candidates` constructs makers-per-country and the verifier accepts that inner count as the final answer.

**Implementation:**

1. Bind two obligations: count makers within each country to decide qualification; count the qualifying countries for the requested scalar output.
2. Construct a typed relation of qualifying country keys using existing `SelectQuery`/`SubquerySource` support, then count that relation, or use an equivalent justified entity-membership form. Keep SQL rendering in the existing renderer.
3. Distinguish "which countries", "how many countries", "makers per country", and "total makers in qualifying countries". A top-level COUNT cue cannot choose the inner counted population by itself.
4. Group by entity keys before labels where identity matters. Prevent join fan-out from inflating child counts; use the intended child key or distinct child entity when semantics require it. For optional joins, distinguish child-key count from `COUNT(*)`.
5. Verify final output unit and cardinality, not merely the presence of COUNT, GROUP BY, and a threshold. Correct the existing test expectation that currently blesses the wrong unit.

**Acceptance:** independently expected results for zero, one, and several qualifying groups; duplicate country labels; duplicated join rows; NULL child keys; and the contrasting question forms above. Check exact scalar shape as well as value.

**Gold-label caveat:** DEV 161/162 ask for a country count but gold returns maker counts `[4,4,3,5]`. Gemini returns the requested country count `4` and fails the frozen strict scorer. A semantic repair can lose these two benchmark matches. Keep frozen gold/scorer unchanged for all systems; report these cases separately using [independent executed checks](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3817b34-isolated-20261011/gold-count-unit-cases.json). Never implement the wrong unit to preserve a benchmark win.

### 4.3 Membership scope and correlation — first correctness fix

**Owner:** [query_contract.py](C:/work/prereasoner-data/engine/query_contract.py), with existing schema and AST traversal support.

**Observed:** for "people whose age is above 35", the contract accepts uncorrelated EXISTS over qualifying people, which returns everyone, and `outer.Person_ID IN (SELECT Age ...)`, which compares different quantities. The correct ID-to-ID membership is also accepted. This demonstrates a verifier defect; the probes do not establish that ordinary search selects both bad candidates.

**Implementation:** extend the existing `_Scope`/subquery traversal evidence with the binding that makes an inner predicate constrain the returned entity. For IN, verify outer operand ↔ inner projection identity/relationship and compatible population, not only SQL type. For EXISTS, require the correlation or other structural link justified by the request; a globally quantified request may legitimately be uncorrelated. Track aliases and each quantifier boundary. Preserve existing alternative/negated/scalar distinctions and require the correct AND/OR structure.

**Acceptance:** correct correlated membership passes; uncorrelated and ID-to-age counterexamples fail; legitimate global-existence questions pass. Add alias/self-relation cases and same-type different-key negatives. Use rows where each wrong interpretation produces a distinguishable answer. Test both coverage and ordinary serving when a generated candidate is available.

### 4.4 Repeated aggregates — early construction repair

**Owner:** [sql_search.py](C:/work/prereasoner-data/engine/sql_search.py), using shared request evidence from `query_contract.py` and focused extraction in `sql_expansion.py`.

**Observed:** "average distance and price" works on the fixture; "average distance and average price" refuses, incorrectly suggesting that price is nonnumeric. `_aggregate_choices` deduplicates by function before binding the operand; `_coordinated_targets` cannot treat the repeated cue as part of the same coordinated request.

**Implementation:** retain each operation occurrence with its source span. Bind `(function, operand, population, distinctness, output position)` before semantic deduplication. Support coordinated operations sharing one operand and repeated operations with separate operands. Deduplicate redundant paraphrases only after proving that they denote the same requested output. Reuse the same bindings in construction, ranking, and coverage; remove superseded function-only behavior in the coherent family change.

**Acceptance:** two/three requested aggregates; repeated same-operand paraphrase; different functions; separate filtered populations; column named Average; unrelated numeric distractors; genuinely malformed numeric operands. Diagnose missing construction separately from unreadable source values.

### 4.5 Ranked cardinality and numeric roles — early construction repair

**Owners:** `sql_search.py`, `sql_extrema.py`, `sql_expansion.py`, and `query_contract.py`.

**Observed:** "the two people with the largest Age" builds LIMIT 1/max-scalar alternatives and refuses. A supplied correct LIMIT 2 candidate passes, so the verification repair does not solve normal construction. Adding an exclusion such as Person_ID 2 must not turn the filter literal into the ranking cutoff.

**Implementation:** use one shared bound reading of the ranking operation, ranked population, measure, direction, cutoff, and tie policy. Reuse number parsing; assign source spans to cutoff and independent filter literals. Migrate both base ordering and extrema generation plus coverage to that reading, deleting overlapping cutoff inference after all callers migrate. Construct the requested LIMIT instead of defaulting to 1 when a cardinality was explicitly supplied. Preserve tie semantics for "all most" versus explicit top-N.

**Acceptance:** digit/spelled counts; longer noun phrases; cutoff and filter with equal/different values; exclusions and countries; direction reversal; ties; wrong LIMIT rejected. A manually constructed AST passing is insufficient: the normal serving path must construct and serve the right plan.

## 5. Shared interpretation and proof inside current owners

### 5.1 Extend the existing records, one family at a time

`query_contract.py` should own shared non-executable request obligations and their candidate witnesses. Extend its existing `Reading`/`Coverage` behavior and reuse `sql_expansion.py` primitives; exact internal record names should follow the implementation. Do not introduce a second parser/planner or a model-generated requirement document.

| Required information | Why it is needed |
|---|---|
| Stable source span and obligation ID | Repeated words/numbers can denote different operations |
| Operator, polarity, and Boolean relationship | COUNT versus AVG; includes versus excludes; AND versus OR |
| Typed operand alternatives and evidence | Bind the operation to its actual field rather than an operation-like column name |
| Returned entity and identity | Exclude/count the right entity, not a repeated label or foreign key |
| Population and scope relationship | Outer rows, related rows, groups, scalar reference population |
| Group grain, contribution identity, distinctness | Prevent counting makers when countries were requested or summing multiplied rows |
| Correlation/membership mapping | Prove that an inner condition constrains the intended outer entity |
| Output fields, cardinality, order, cutoff, tie policy | Distinguish a scalar from per-group results and top-N from a single maximum |
| Bound, alternative, unresolved, or contradictory state | Uncertainty must remain explicit; a similarity is not a proof |
| AST witness and rule | Explain exactly how the selected node satisfies the obligation |

A model may supply bounded named evidence for these bindings through the existing encoder. Deterministic code decides bindings, scope, admissibility, AST construction, stable ranking, and execution. A named neural head improves decision accountability; it does not make every internal neural operation interpretable.

### 5.2 Preserve the one serving flow

```text
question + normalized source/schema evidence
    -> shared request obligations with bounded alternatives
    -> SQLSearcher + existing typed expanders
    -> type / execution / grounding / semantic witnesses
    -> existing CandidateRanker and TableQuery.select_query
    -> existing serving finalization, including tie policy
    -> SQL or supported deterministic AnalysisPlan execution
```

The executable own-data representation remains the typed AST. `AnalysisPlan` remains the dual-emitter plan derived from its winner. Compose remains the existing composition/view system and shared routing consumer. No generated SQL, generated whole AST, or separate evaluator enters serving.

Hard-reject proven contradictions; retain bounded alternatives when the question binding is genuinely unresolved. Do not let additive similarity compensate for a missing required filter or a wrong counted entity. Conversely, do not reject a correct relation simply because the field occurs inside the right membership scope rather than at the outer level.

Shared extraction can make construction and verification consistently wrong. Keep independently derived expected answers and adversarial fixtures; a construction witness is evidence, not a substitute for checking the question's intended semantics.

## 6. Ordered implementation packages and gates

Each package extends the current owners, removes superseded logic in the same change, registers regression tests, and records measured wins/losses before proceeding. The ordering is a dependency plan, not an estimate of guaranteed score gains or calendar duration.

### Package A — Reconcile the failure ledger with actual serving

**Owners:** `TableQuery.select_query`, `PoolSelection` in `sql_rank.py`, `query_contract.py`, and `spider/probe/full_eval.py`.

1. Preserve the verified baseline; freeze a new baseline if current dirty edits are included. Capture source/model/schema/input fingerprints and flags before prediction and verify them afterward. Invalidate prior cached predictions after any relevant change.
2. Extend existing selection diagnostics to distinguish constructed, retained, typed-valid, executable, grounded, constraint-satisfied, coverage-complete, selected, and finalized states. "Executable and grounded" must not be called a complete semantic admission proof.
3. Evaluate raw pool members and their serving realizations separately. `PoolSelection.candidate` currently applies `keep_ties` only to the selected member; use that same owned transformation for diagnostic realizations, rather than copying it into an oracle-only path.
4. Keep gold comparisons exclusively in evaluator-side reduction. Gold may identify a correct candidate offline; it must never select, construct, or admit a production candidate.
5. Assign one primary lost stage with secondary reasons. Distinguish no candidate built from a correct partial interpretation pruned before completion. Grade the actually served realization and link it to the original member.
6. Produce per-family, difficulty, database, and schema-width tables. Manually adjudicate a stratified sample of refusals, lenient-only answers, and both-incorrect answers against independent semantics.

**Exit gate:** a complete per-question ledger whose counts sum to 1,034, with raw recall, finalized recall, complete-admissible recall, and top-1 separately defined. Diagnostic correctness only; do not call this an accuracy improvement. Avoid another full baseline run when the exact frozen inputs are already valid and no behavior changed.

### Package B — Repair false acceptances before widening coverage

**Owners:** schema identity, constraint expansion, and query contract owners listed in section 4.

Implement 4.1–4.3 as coherent family changes. Add expected-answer and wrong-candidate tests before the fixes; exercise ordinary serving for identity/count cases and contract rejection for the scope counterexamples. Review every identity consumer and scope traversal branch. If correlated construction needs an AST capability not present, audit validation, traversal, renderer, and lowering before extending the existing typed representation.

**Exit gate:** all independent positive/contrastive/negative semantics pass, with fresh whole-database transitions for each coherent behavior batch. Report newly refused wrong answers as safety improvements, not accuracy gains. Explain any frozen-gold loss from the country-count label defects explicitly. A new semantic refusal is incomplete as an accuracy fix if the correct expressible plan still needs to be constructed.

### Package C — Recover valid refused plans through semantic completeness

**Owners:** `query_contract.py`, `tables.py`, and existing expansion/role helpers.

Work through the validated portion of the recorded 199 refused-with-correct-plan cases, and the 26 wrong-with-correct-plan-blocked cases. For each family, identify the exact unsatisfied obligation:

- Ordinary connective or noun wording: consume it through a structural reading that proves what it modifies; do not add it to a global ignored-word list.
- Field appearing in the right nested relation: prove entity mapping and scope, then accept the witness. Mere presence anywhere in SQL remains insufficient.
- Numeric/comparative wording: bind field, comparator, literal role, and population together; preserve conjunction and negation.
- Required data absent from the upload: retain the existing explicit world/delegation or clarification behavior. Do not replace "premium customers" with all customers or infer an unavailable country filter.

Preserve the existing `_choose` behavior that can select a later complete candidate; "try another candidate" is already implemented. Fix what makes the correct candidate appear incomplete. When external rewriting is enabled separately, verify original-question obligations so a rewording cannot erase meaning; deterministic headline runs keep it disabled.

**Exit gate:** independently correct refusals become correct served answers, omitted qualifiers and wrong-scope negatives still fail, and harmful false acceptances do not increase in the mandatory semantic suite. Report newly answered strict-correct and newly answered wrong separately; a higher answered count alone does not pass.

### Package D — Fix selection where the right admissible plan exists

**Owner:** `CandidateRanker`/`PoolSelection` in `sql_rank.py`, consumed by the same `TableQuery.select_query`.

Validate the recorded 62 opportunities after finalization. Attach named binding/identity/population evidence to each alternative. Enforce established requirements before score-based selection; then prefer the best justified remaining interpretation with explicit stable tie-breakers. Inspect existing date, calculation, money-total, and duplication preferences together so one preference does not defeat another obligation.

Add contrasts where the wrong alternative executes successfully and has high lexical similarity. Do not learn an opaque selector over model-written SQL, adjust arbitrary weights to fit DEV IDs, or select whichever candidate matches gold. Test repeatability under different hash seeds and benign input permutations while respecting intentional active-source precedence.

**Exit gate:** top-1 capture of genuinely admissible correct candidates improves; finalized candidate recall is reported alongside it. A better raw pool ceiling with unchanged top-1 is headroom, not a selection gain.

### Package E — Increase construction recall and retain useful alternatives

**Owners:** `SQLSearcher`, `sql_expansion.py`, `sql_constraints.py`, `sql_recursive.py`, `sql_extrema.py`, and existing AST/grounding owners.

Start with confirmed repeated-aggregate and ranked-cardinality repairs (4.4/4.5). Then select the largest remaining family from the 363 recorded no-correct-plan cases, rather than assuming all hard questions need one feature. Audit existing representability before adding nodes.

| Family to classify | Deterministic construction direction | Required distinguishing data |
|---|---|---|
| Operator/operand and coordinated outputs | Bound operation occurrences and shared/separate operands | Different numeric answers; Average/Total distractors |
| Count/group/unit | Qualifying entity relation followed by requested aggregation | Multiple groups, duplicate labels, unequal inner counts |
| Entity membership and exclusion | Correct key mapping and correlated existence/absence | Shared FK values, NULLs, no related rows |
| Aggregate fan-out | Aggregate at contribution grain or use justified membership | Equal-valued distinct entities and multiple children |
| Reference thresholds | Explicit global/filtered/per-group scalar population | Different means in every competing population |
| Boolean/quantifier scope | Typed nesting, correct polarity, all versus any | Mixed related rows and explicit empty-population policy |
| Ranked cardinality | Bound measure/direction/cutoff at requested entity grain | Ties, repeated events, independent numeric filters |
| Source/join roles | Required-role table set and justified key paths | Distractor tables, aliases, composite relationships |

Instrument where correct bindings or partial plans disappear. Use bounded diversity by semantic structure and unresolved role, stable structural keys, and constraint-guided expansion within the current search. Reject typed impossibilities early. Measure late-completing alternatives before changing pruning. Do not merge plans merely because their results happen to coincide on the current rows, and do not increase every beam/cap without a recall-per-work ablation.

**Exit gate:** finalized and complete-admissible candidate recall improve, then served strict correctness improves without semantic failures. Publish remaining missing-plan counts. Reassess whether the same family is limited by interpretation, construction, retention, or selection before the next change.

### Package F — Improve named encoder evidence only if diagnostics justify it

**Owners:** existing encoder/readout, `encoder_overlay.py`, `tables.py`, and `training/props/`.

First run gold-blind signal ablations with identical code/data/weights: exact schema evidence, grammatical binding evidence, and each named model signal. Determine whether correct operands/scopes are missing because a named signal is poor, because search never consumes it during enumeration, or because later code discards it. Reuse useful named evidence during bounded construction when appropriate; changing a weight cannot rescue an option never generated.

If later explicitly authorized, run one controlled training experiment for a demonstrated named classification/relation deficit, such as counted entity, operation-to-operand binding, relation direction, membership polarity, or reference-population scope. Use independently labeled positives/negatives and database-disjoint evaluation; check actual separation from encoder training before calling a sample unseen. Preserve useful existing dimensions and measure downstream correctness, repeatability, latency, and memory.

No SQL/AST token decoding or whole-plan generation. Candidate weights remain outside `engine/data/`; training and promotion each require their repository contracts and authorization. A larger model or new rewrite provider is not the first intervention.

**Exit gate:** improvement on independent binding tests and integrated serving under unchanged comparison conditions. Loss reduction or DEV score alone does not justify promotion. This package is conditional, not part of the currently executed task.

### Package G — Demonstrate a durable advantage

Retain the frozen public comparison, then test unseen schemas and independently specified questions/data variations. These DEV questions have now influenced development repeatedly; they are development evidence, not a final blind test. A train-derived held-out database sample must document any encoder exposure and prior aggregate scans before stronger generalization claims.

Use paired wins/losses, database-level strata, and uncertainty. Freeze reference settings and retry/failure accounting; a new paid reference generation is a separate authorized action. Keep execution failures in the denominator and do not choose the best of multiple reference answers by gold. Report task-equivalent inputs with their different presentations honestly.

**Exit gate:** the external-rewrite-off engine beats the reference point count and independently held-out semantic evaluation, with sufficient margin and resource measurements to support the claim. A one-answer public-set win alone is weak evidence of a general advantage.

## 7. Evaluation gates and acceptance scorecard

### 7.1 Tests needed for every family

Use the existing explicit test lists and production entry points. The minimum semantic matrix is:

| Concern | Positive | Contrast/negative |
|---|---|---|
| Identity | Exclude entities by their real key | Duplicate FK/display values; NULLs; lookup-table addition |
| Count grain | Count qualifying countries | Makers per country; total makers; several qualifying groups |
| Membership scope | ID-to-ID membership or justified correlation | ID-to-age; uncorrelated condition; unrelated same-type keys |
| Aggregate binding | Separate AVG distance and AVG price | Repeated paraphrase; field named Average; separate filters |
| Ranking | Explicit two oldest people | One oldest; ties; exclusion literal 2; reversed direction |
| Join contribution | Sum each requested entity once | Repeated child rows; equal-valued distinct entities |
| Missing qualifiers | Filter with the supplied qualifier | Absent premium/country/world attribute must not vanish |
| Boolean/reference scope | AND/OR and correct average population | Rows distinguishing conjunctions; global versus group mean |
| Shape/order/numeric semantics | Requested columns/scalar/order | Extra columns/rows; bag duplicates; exact Decimal/NULL/empty behavior |
| Determinism | Stable decision on identical inputs | Hash seeds; unrelated tables; row/table permutations |

Expected answers must be derived independently of the engine and cannot be copied from its output. Include perturbations that keep entity-level answers constant while changing related event counts. SQL/Python parity checks execution consistency on supported plans; both emitters can agree on a wrong interpretation, so parity is insufficient on its own.

### 7.2 Required measurement after a coherent behavior change

- All-question strict/lenient/answered counts, plus among-answered reliability and the disjoint bucket partition.
- Raw, finalized, and complete-admissible candidate recall with exact definitions; top-1 selection capture.
- Strict wins, losses, unchanged correct, unchanged wrong, plus semantic adjudication of serious regressions and gold-label conflicts.
- Newly answered correct versus newly answered wrong; wrong answer → correct, wrong → refusal, and correct → refusal transitions.
- Difficulty, database, schema width, identity/grain/scope family, and budget/execution error strata.
- Exact requested scalar shape, ordered results, ties, bag multiplicity, numeric/NULL tests independently of the frozen comparator.
- Search work, pool size, encode/search/check/execute timing, p50/p90/p95, memory, and machine/runtime conditions.
- Commit, dirty state, source/input/schema/model/scorer fingerprints, flags, denominator, and complete per-question artifacts.

Baseline difficulty is easy 133/248 strict, medium 172/446, hard 53/174, extra-hard 26/166. Coverage is 163, 260, 101, and 69 respectively. Improvements should report these strata so easy lexical rescues do not conceal stalled nested/entity semantics.

Mandatory semantic regressions must pass. Semantic hardening may reduce frozen-label accuracy where gold is wrong; record that honestly rather than applying an unconditional no-score-loss gate. For an accuracy package, require demonstrated net improvement on the intended independently checked family and overall transitions, with each meaningful loss investigated. A coverage-only increase or an unexplained correctness loss is not sufficient.

### 7.3 Reproducible validation using existing owners

These are future implementation commands, not additional tests executed for this documentation update. Use fresh outputs after behavior changes; resume only if every fingerprint/flag matches. Set the bundle path explicitly when using an isolated checkout without its own model data.

```powershell
$env:EXTERNAL_LLM_ENABLED = '0'
$env:PREREASONER_DATA_DIR = 'C:/work/prereasoner-data/engine/data'
$accuracyTag = 'replace-with-unique-change-tag'
$accuracyOut = Join-Path $env:TEMP ('prereasoner-sql-accuracy-' + $accuracyTag)
.venv/Scripts/python.exe -m spider.probe.full_eval `
  --data spider/data --dbs spider/data/dbs --out $accuracyOut `
  --config whole_db --selection served --backend sql `
  --cap 5000 --timeout 12 --tag $accuracyTag

.venv/Scripts/python.exe -m tests.test_sql_ast
.venv/Scripts/python.exe -m compileall -q engine db training tests orchestrator mcp_server
```

Run query-contract, numeric/calculation, deterministic-emitter, and request-budget tests when their owners change, and `tests.test_compose` for compose/routing interactions. Use the existing evaluator's `pool_oracle` only as a labeled diagnostic with its eligibility/finalization limits exposed. Use `--backend verify` on affected supported dual-emitter cases. `gold_tables` remains a labeled schema oracle, never the headline.

Before repository-wide completion run the applicable `tests.run_all` checks, reporting unavailable live suites as skipped. Before a major production release run the documented live Postgres and deployed Chrome demo-dataset gates, including ordered follow-ups in both existing and fresh conversations. Planner accuracy tests do not replace those release checks.

## 8. Performance work that supports accuracy

The verified run's prediction time was p50 **2.667 s**, p90 **5.329 s**, p95 **6.224 s**, maximum **9.229 s**. Several CPU jobs ran concurrently. These are observations of this run, not a controlled before/after speed comparison or a provider latency comparison.

Prioritize optimizations that preserve candidate meaning and observable selection:

1. Compute immutable schema identity/cardinality facts once per normalized input, with explicit invalidation when rows/metadata change. Correct the identity semantics before caching them.
2. Extract shared obligation bindings once per request. Reuse candidate witnesses across ranking/completeness and repeated AST traversals within that request.
3. Use canonical typed structural keys to avoid duplicate expansion/execution only where equivalence is established, including bag/NULL/scope/output semantics. Equal rows on one input do not prove equivalent plans.
4. Prune proven type/relationship/obligation contradictions before expensive joins and execution. Keep bounded diverse alternatives when evidence is unresolved.
5. Reuse the current request-scoped execution connection and evidence; inspect existing caching before adding another cache. Avoid cross-tenant or stale-data evidence reuse.
6. Instrument with the one `request_timing.py` owner. Runtime logs carry allowed phase/count/reason metadata, not prompts, cells, questions, or tokens. Detailed benchmark records remain offline.

Measure controlled single-workload cold-start and warm-request performance, using fixed hardware/runtime/model/search budgets and representative wide/narrow schemas. Report candidate recall and top-1 along with latency/memory. Declare any allowed latency envelope before evaluating a budget change. Do not silently trade away coverage by shrinking tables, beams, or deadlines, or call a wider search a free accuracy gain.

## 9. Milestones toward beating next-token SQL generation

The strongest regraded frozen reference has **872 strict-correct answers**. At least **873/1,034 (84.4%)** exceeds that point count; it does not establish a statistically convincing or broadly general advantage. A larger engineering target such as **934/1,034 (90.3%)** is a target, not a forecast.

Separate the two factors. Let `R` be questions with at least one scorer-correct candidate after actual serving realization and all applicable production admission checks, and `C` be strict-correct served answers subject to those checks. Then, for `R > 0`:

```text
all-question strict accuracy = (R / 1,034) × (C / R)
                                 recall       selection capture
```

A raw pre-contract pool count is not `R`. If `R` reaches at least 983 and selection captures at least 95% of it, the count is at least 934. Establishing that recall and capture are attainable is the work; neither typed validity nor the current 671 raw successes demonstrates it.

| Milestone | Deliverable | Decision gate |
|---|---|---|
| Measurement trustworthy | Serving-faithful candidate ledger | Explain every failure and finalization difference |
| Known semantics hardened | Identity, count-unit, membership fixes | Independent positives/negatives pass; losses classified |
| Coverage recovered safely | Valid refused candidates served | Correct rescues measured; omitted qualifiers still rejected |
| Selection improved | Correct complete candidates win | Higher capture with stable decisions and no hidden gold input |
| Construction extended | Missing families produce correct plans | Finalized/admissible recall and top-1 improve within measured work budgets |
| Generalization demonstrated | Unseen-schema and data-perturbation results | Semantics and repeatability hold beyond DEV-driven cases |
| Reference surpassed | Paired frozen and held-out comparison | Margin, uncertainty, costs, and remaining wrong answers reported |

Do not attach invented percentage gains or fixed dates to these milestones before the refreshed ledger and implementation measurements exist. Recovering some of the 199 refusals is useful progress; it is not sufficient to reach 84.4% while correct plans are missing for hundreds of questions.

## 10. First implementation batch

The next coding batch should follow this concrete order:

1. Freeze the chosen source including any intended Claude edits; register the independently reproduced negatives and baseline expectations.
2. Reconcile the existing candidate census with complete checks and serving finalization, then publish the baseline failure ledger. Known reproduced fixes need not wait for exhaustive manual adjudication of every remaining case.
3. Correct entity identity/NULL-safe exclusion and count-unit construction in their owners; correct the misleading count-contract test.
4. Add entity/membership correlation evidence to the existing scope verifier, preserving positive nested-scope behavior.
5. Fix one high-yield witnessed-completeness family from that ledger; demonstrate correct rescues and preserved qualifier negatives.
6. Repair repeated aggregate and top-N numeric-role construction using shared bindings, with ordinary serving regressions.
7. Evaluate the coherent batch on all 1,034 questions, report paired transitions/candidate recall/resources, and choose the next family from measured remaining losses.

The intended outcome is **more independently correct answers from the same interpretable deterministic engine**. Model-generated SQL, blanket refusal suppression, changed gold labels, and provider swapping would not satisfy this implementation plan.
