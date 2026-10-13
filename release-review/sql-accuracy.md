# SQL accuracy: measured weaknesses and implementation plan

## October 13 independent measurement and next implementation order

The frozen package-R snapshot plus review repairs measures **497/1,034 (48.1%) strict / 546/1,034 (52.8%) lenient / 704/1,034 (68.1%) answered**, external rewriting off. The earlier clean `e709851` fresh run verifies 493/543/701. Package R's 113 recorded serving files matched before repair, and final source/input/model fingerprints match after evaluation. Refer to the current [review](C:/work/prereasoner-data/release-review/spider-review-20261011.md) for source scope, hosted comparison and evidence. Later edits must receive their own run.

The disjoint remaining cohorts are 330 unanswered, 132 incorrect under both scorers, and 75 lenient-only. The latter includes semantic failures, not just cosmetic output mismatches. Fixing every refusal alone would yield only 827/1,034; perfect correctness at today's answer coverage would yield 704/1,034. Both coverage and precision must improve to exceed the saved Sonnet 840 or Gemini 872 references. The engineering target for beating both references on this exact contract is at least 873/1,034, with a new untouched acceptance set and comparable coverage. That is a target, not a forecast.

### 1. Make population and output requirements a shared constructor input

Extend the existing `sql_expansion`, `sql_search`, `sql_constraints` and `query_contract` owners. Do not add another planner or a model-written AST grammar. Begin with row counts, stored-count quantities, grouped counts and frequency rankings. Preserve an occurrence for each requirement, rather than collapsing by column name or numeric bound.

| Requirement | Necessary fields | Example obligation |
|---|---|---|
| Output | occurrence/span, entity, expression kind, operand, grain, output ordinal | Show the student ID, first/middle/last names and enrollment count; an operand used only to order is not an output |
| Count/quantity | counted entity or stored-count field, identity key, NULL/distinct policy, grouping grain | Count manufacturers, not the joined cars; transfers 8+5 are 13, not two hospital rows |
| Predicate | occurrence/span, bound field or aggregate, operator, literal, scope, polarity, Boolean parent | Both related counts must satisfy the stated AND/OR; the top-3 occurrence cannot also mean field = 3 |
| Population | subject identity, relation path/direction, qualifying filters, reference population, scope | The mean used as a threshold belongs to the requested population and keeps its filters |

Use the same bindings to build candidates and to discharge their obligations. The bindings are nonexecutable named evidence: they do not contain SQL strings or free-form AST tokens. Encoder heads may assign bounded entity/field/operation/scope labels; fixed typed constructors consume those readings. Every selected AST must expose how each required output and predicate is realized. Missing or contradictory evidence remains unresolved rather than silently ignored.

First positive/negative fixtures: zero/one/many manufacturers with repeated downstream car rows; one row per channel versus many cartoons per channel; repeated output requests; a count used only in HAVING versus a displayed count; COUNT versus SUM of stored counts; SUM of the wrong numeric field; nullable children and repeated labels; identical numbers in cutoff and predicate roles; self-relation direction and scoped averages. Use counterfactual rows so wrong plans cannot pass merely because this particular answer is empty or tied.

### 2. Certify recoverable refusals before relaxing admission

Produce a per-question ledger through the existing production selector. For each refusal, distinguish: correct plan exists and is rejected; correct plan exists only before pool pruning/finalization; correct plan is absent; or the question/data is genuinely ambiguous. Run the actual served realization, including `keep_ties`, on each proposed recovery and independently check its meaning. A table name, stem match or executable query is insufficient proof of an entity, predicate or scope binding.

Prioritize recurring false refusals with a correct, fully witnessed plan. Add same-profile contrastive and negative fixtures before broadening a lexical witness. The current 120-case raw-pool diagnostic is limited to the first 30 questions per difficulty; historical 672 raw hits and the old 227 blocked cases are not today's certified recovery count. Recompute a full current ledger before using those numbers as an accuracy ceiling or a prioritization budget.

### 3. Repair returned semantic errors alongside the refusal work

Work on wrong counted populations, multiplying joins, predicate attachment, output grain, omitted outputs and reference-average scope first. Lenient-only cases with extra rows or incorrect row associations belong in this work as well. Make a join prove whether it preserves or multiplies the requested identity; COUNT(*) is correct only at the proven counted grain. Prefer deterministic semijoins/preaggregation/distinct identity counts only when they realize that obligation. Removing a join or applying DISTINCT merely to match gold is not a valid repair.

Then address true presentation-only mismatches: requested output order, projection shape and explicitly requested bag/set semantics. Preserve the order of requested output occurrences and do not reorder by gold. Keep a separately adjudicated list of reference/tie defects; use one unchanged scorer for every system.

### 4. Measure correct bindings before training

Independently annotate a bounded set of failure families with entity, operation, operand, value, population and scope bindings. Inject those named bindings into the current constructor/checker interface as an offline-only experiment, leaving the production encoder, AST grammar, ranker and evaluator unchanged. Measure served top-1, retained and pre-pruning candidate recall, checker admissions, wins/losses and latency. The previous broad gold-column-role score substitution did not run this experiment.

Measure the encoder's actual coverage of needed spans too: `TableQuery._encode_batch` currently tokenizes at `MAX_LEN = 48`, so audit token truncation on longer global and role phrases before assuming all meaning reaches the heads. Compare a token-budget-only ablation under the same code/data/model/scorer, including latency and unaffected short-question contrasts. Do not blindly change context length or train a larger encoder together with grammar changes.

If correct bindings still cannot build the correct AST, extend typed construction/proofs before training. If correct bindings recover plans but the real encoder's readings do not, train the existing named-property pipeline on those binding labels only after a separate explicit training request and frozen experiment contract. Candidate weights remain outside `engine/data`; promotion is separate. No next-token SQL/AST generator is part of this plan.

### 5. Optimize measured costs after binding correctness

Use the existing per-request timing spans to separate encoding, schema preparation, construction, admission and execution. Existing per-instance vector caching already exists; preserve its invalidation rather than adding another cache. Compute question semantics once and share the named bindings between construction and checking. Cache schema-only indexes by schema/upload version, prune impossible type/scope assignments early, and retain semantic diversity under the existing bounded pool. Compare warm and cold requests separately on an otherwise idle machine. Benchmark-query finalization must remain identical to serving.

### Acceptance gates for each implementation slice

Freeze source, promoted bundle, question/database identities and evaluator contract; keep external rewriting off for the deterministic headline. Require registered positive, contrastive and negative production-owner regressions, applicable emitter parity or explicit unsupported coverage, fresh full `whole_db`/`served` DEV results and paired wins/losses. Report strict, lenient, answered, false-admission counts and marginal precision of newly answered questions, not aggregate wins alone. Diagnose every loss and any precision degradation. Run one new schema/composition holdout that has not informed the change; the two existing 1,000-question samples are development evidence now. Report latency and raw-versus-certified pool recall honestly. Keep one planner, selector, ranker, evaluator and promoted bundle.

This order follows semantics: fix returned wrong meanings and witnessed false refusals together; presentation-only corrections follow. The cohort labels by themselves do not identify root causes, and there is no evidence that next-token entropy makes a generator inherently more accurate than a complete, properly bound deterministic compiler.

**Updated:** 2026-10-13. This updates the existing guide after the independent fix reviews, Spider artifact audit, and review of Claude's implementation of the plan. It supersedes the October 8 baseline and earlier first-batch ordering.

**Historical independent run, October 12:** local `f6fbf4d` plus the uncommitted Boolean-count repair verifies **386/1,034 strict (37.3%), 444/1,034 lenient (42.9%), 590/1,034 answered (57.1%)**, with 361 passing focused checks and all 113 tracked source fingerprints matching after evaluation. The selected SQL and all grades/statuses exactly match Claude's r127. The earlier S1–S6 examples are repaired; the new AND/OR contrast is repaired locally. This stage adds no DEV accuracy gain. See [the current review](C:/work/prereasoner-data/release-review/spider-review-20261011.md) and the assessment below. Detailed `3817b34` and `6263502` records remain historical evidence.

**Historical measured sources:** `3817b34378d0133e272d75619d53bfe66a2e8e2c` and `6263502f7cdd3c0639208f13e49daa4563bfaae0`, in isolated clean checkouts. The October 12 independent source was `f6fbf4d5d9b914129ec1ccab2c3d5ad996fe9b0b` with the documented local repair; immutable file fingerprints, not the commit alone, identify this dirty run.

**Scope:** improve the existing deterministic, interpretable own-data engine. The October 11 plan update changed documentation only. The October 12 independent review includes a narrow Boolean count-composition repair described below; the broader binding implementation and training remain proposed. No training, promotion, paid model generation, or deployment was performed in this review. Repository rules remain in [CLAUDE.md](C:/work/prereasoner-data/CLAUDE.md); architecture facts and decisions retain their existing owners.

**Plan expansion, 2026-10-11:** sections 0, 5, 6, and 10 now explain the semantic-binding bottleneck and specify the internal records, first implementation slice, diagnostic experiments, conditional training objective, and acceptance gates. These are proposed changes, not shipped capabilities or new benchmark results. The current follow-up and section 10 supersede the historical first-batch ordering; repaired F/R examples remain regression controls, not defects to fix again.

## October 12 package: output requirements and witnessed refusals (implemented)

Implemented against the revised next work below, measured in `spider/results/RESULTS.md` (2026-10-12, "Answers shown as
asked"): DEV 386 → 461 strict, Gemini on 471 → 529, the repeatedly used sample 255 → 278, and a fresh 1,000-question
holdout drawn for this purpose 286 → 306. Item 2 (output requirements): grouped answers in the question's order, fields
named only as filters kept out of the output, DISTINCT where a join repeats shown rows. Item 5 (witnessed false
refusals): words a query realizes without spelling them, each family simulated on the full pool census first. Item 3,
in part: a "number" that begins a field's name, and one aggregate word over every field it coordinates. Not done: the
constructor-consumed binding interface (item 4) and the ledger certification (item 1). The new answers are less precise
than the old ones on the samples (held out: 20 of 58 newly answered are right), so the next work is the gate's checks
of what a query shows and groups, as `DECISIONS.md` records with the variants measured and not adopted.

A second package (construction of counts over exclusions and average comparisons, numbers beside fields; output kept
to the fields the question asks) raised DEV 461 → 471 and the held-out sample 278 → 304 with no losses on either, and
lowered the held-out wrong answers 261 → 257 (`spider/results/RESULTS.md`, "Counts over exclusions and averages").
A third (full names, fields asked by their owner, counting words, conditions kept out of answers, plain-English Gemini
rewordings) raised DEV 471 → 493, the held-out sample 304 → 324 with no losses on either, Gemini on 538 → 576, and the
fresh holdout 306 → 329 against what production served; on the fresh holdout 9 of 29 newly answered questions are
right, so the precision of new answers remains the measure to watch (`spider/results/RESULTS.md`, "Full names").
A fourth (a number of a numeric field, all the information, "equal to" and "record") added DEV 493 → 497, held out
324 → 335, the fresh holdout 329 → 334 and Gemini on 576 → 592, with no loss on any of them.

## October 12 assessment: what Claude implemented and what remains

**Assessment:** Claude followed the correctness-hardening and measurement stages, and implemented a useful narrow count composer. He did not implement the proposed constructor-consumed entity/operand/population/scope binding interface. There is no substantial accuracy gain attributable to this latest stage. The retained encoder-only/deterministic architecture remains intact; its broader semantic-binding proposal has not yet received the intended experiment.

### Measured change against the appropriate baseline

The independently reviewed starting point was `6263502`, not the older deployed `8164601`. Recomputed paired comparisons of Claude's complete saved per-question records give:

| Configuration | `6263502` / r123 | `3563863` / r126 | Count composer / r127 | Latest-stage effect |
|---|---:|---:|---:|---|
| DEV, external rewriting off | 386/1,034 (37.3%) | 386/1,034 (37.3%) | 386/1,034 (37.3%) | 0 wins, 0 losses in each stage |
| DEV, cached Gemini rewriting on | 471/1,034 (45.6%) | 471/1,034 (45.6%) | Not separately rerun in r127 | 0 wins, 0 losses for r123 → r126 |
| Repeatedly used 1,000-question sample | 253/1,000 (25.3%) | 255/1,000 (25.5%) | 255/1,000 (25.5%) | +2, 0 losses; then 0 changes in correctness |

The 382 → 386 DEV and 249 → 255 sample release comparison includes earlier R fixes. It must not be credited entirely to the latest S fixes or to the count composer. The two new sample wins are 47 (remove the unrequested count from name/phone output) and 370 (recover the transaction-frequency regression). Sample 102 moves from refusal to a wrong answer, even though no previously strict-correct answer is lost.

All 113 recorded tracked code fingerprints in r126 match `3563863`; all 113 in r127 match `f6fbf4d`. The promoted bundle remains `b11056f8…`. Those historical runs were independently audited; the separate fresh full DEV run after the October 12 repair exactly matches r127's 386 strict / 444 lenient / 590 answered, with no SQL or grade changes and all 113 tracked source fingerprints matching after evaluation. Production deployment and Chrome gates were not independently rechecked.

The Gemini row is our deterministic engine aided by cached wording rewrites. It is not Gemini generating SQL, and it is not the 872/1,034 hosted SQL reference.

### Which parts of the approach were followed

| Proposed part | Actual implementation | Assessment |
|---|---|---|
| Repair reproduced count/membership/performance/numeric-role defects | S1–S6 controls repaired, with additional field-number and lookup-count tests | Useful hardening; passing these fixtures does not certify entire families |
| Share count occurrences between building and checking | `counted_thresholds` retains separate endpoints and is consumed by both; separate child membership plans added | Narrow implementation, not the full entity/filter/grain/scope record |
| Measure candidate availability and blocked choices | All 1,034 raw pools and a served-run ledger recorded | Useful diagnostic evidence, with finalization and retention qualifications below |
| Supply independently correct semantic bindings to construction | Gold's broad column-role score sets were substituted instead | Different experiment; operation, entity, value, Boolean scope and population bindings were not supplied |
| Consume richer encoder readings during enumeration | Base construction still uses lexical/schema evidence; learned column roles remain mainly ranking inputs | The essential interface work remains |
| Demonstrate compositional generalization | Independent fixtures and repeated sample checks | The sample has informed fixes and is now development data, not an untouched final holdout |
| Avoid unmotivated retraining or SQL generation | One unchanged bundle; no new SQL generator | Correct decision; train only after a useful constructor-consumed reading deficit is established |

### What the census and oracle actually establish

The saved census and oracle each contain 1,034 unique indices and no recorded tracebacks. The served ledger reproduces 386 correct; 59 wrong with a raw flag-admitted correct alternative; 27 wrong with only blocked raw-correct alternatives; 118 wrong with no raw strict hit; 200 refused with a blocked raw-correct alternative; and 244 refused with no raw strict hit.

There are **672 questions with a strict hit in the retained raw pool**, including **445 with a raw candidate passing the census's admission flags**. Neither number is certified served recall: the scripts execute raw member SQL, omit `keep_ties` finalization, and do not measure interpretations discarded before the final 25-member pool. Validate realizations and independent meaning before treating the 227 blocked cases as recoverable or the 59 selection cases as safe wins.

The reported structure strata initially omitted 20 questions. A disjoint classification from parsed gold SQL, with set operations before nesting before grouping/ordering, gives **139 flat, 145 grouping/ordering, 40 nested, 38 set = 362**. Flat here allows scalar aggregates. Gold structures are offline diagnostic labels, never serving inputs.

Claude's 535 → 544 result is a **gold-column-role reweighting ablation**, with 20 raw pre-completeness wins and 11 losses. It flattens all nested scopes into role sets and does not supply operation occurrences, COUNT-versus-SUM, counted entity, predicate values, reference populations, output order, polarity, or scope edges. `_choose`'s raw choice is graded before completeness and serving finalization. The result is not a measured 9-answer serving gain, an upper bound on a richer encoder, or completion of the correct-binding experiment.

The defensible conclusion is that the existing role-score interface has limited leverage and base enumeration does not consume it. This is an implementation interface limitation. It does not establish that the encoder understands all required meaning already or that purely lexical construction is the only remaining problem.

Another useful existing ablation points at that interface: r126 with cached Gemini wording rewrites gains **85 strict answers and loses 0** versus rewrite-off (386 → 471). Of those wins, 54 already had a raw-correct candidate under the original question and 31 had no raw strict hit. Rewording can therefore alter both admission and available plans without adding SQL operators. This is not a fully deterministic headline or an automatic source of trustworthy labels: audit meaning preservation and use it only as offline diagnostic evidence. The deterministic alternative is to bind those meanings directly, rather than rely on a text rewrite in serving.

The reported 39 order-only, 53 extra-row, 28 extra-column, 7 duplicate-only and 12 miscellaneous cohorts need saved per-question definitions and semantic validation. Extra rows can arise from wrong filters, joins, populations, grain or legitimate ties; they do not prove a missing filter. No lenient hit does not mean nothing was partially correct. The raw saved census has 222/362 without any lenient hit under its stored flags, so the reported 223 needs its filtering/finalization definition reconciled.

### Additional ordinary-serving bug reproduced and repaired

At `f6fbf4d`, `Which countries have more than 1 car maker OR more than 1 airport?` serves Chile alone on the registered USA/Japan/Chile/Peru fixture. Expected: USA, Japan, Chile. Its scalar version returns 1 instead of 3. `_requirement_candidates` hard-codes AND, and independent count witnesses pass without checking their Boolean connection.

The October 12 repair reads explicit homogeneous AND/OR coordination once through `sql_expansion.count_requirement_operator`, uses it in the typed constructor, and checks the joint Boolean relationship in `query_contract.realizes_numeric_comparisons`. Mixed/unbound coordination remains unresolved. Serving OR/list/scalar/zero-admitting cases and wrong-AND/wrong-OR completeness contrasts fail before and pass after this repair. This is a necessary semantic fix, not a forecast of headline gains; r127's new multi-count constructor had no DEV SQL effects with its existing reader.

Fresh full DEV confirms no new benchmark gains or losses. The 361 focused checks, repository CI Ruff F/E9/high-severity Bandit commands, compilation and whitespace checks pass. These compound membership shapes are SQL-supported; the current Python lowerer rejects them as unsupported, so no execution-parity claim is made for the three new controls. The existing emitter suite passes. Saved outputs and provenance are in `%TEMP%/prereasoner-claude-approach-review-20261012/`; full metrics remain in the canonical results record.

### Revised next work, ordered by evidence

1. **Certify the ledger's serving realizations and preserve joint scope.** Share finalization through the existing selection owner, retain per-candidate rejection witnesses, and distinguish never enumerated from pruned/retained-pool misses. Keep the OR contrasts mandatory. Do not increase base score constants as a substitute for complete semantic requirements.
2. **Implement occurrence-bound output requirements.** Begin with the reported order/extra-column cohorts, independently auditing intent. DEV 49 explicitly says `List the maximum weight and pet type`, while the engine returns pet type first. Honor explicit requested field order through typed SelectItems; keep fields mentioned only as filters out of output. Reuse the requirement in construction, ranking and checking. Preserve scalar/group output grain and update supported SQL/Python parity. A global permutation selected by gold is prohibited; an explicit source-order requirement needs no new product permission.
3. **Bind numeric roles and quantity-versus-entity aggregation before extending many templates.** Sample 102 filters Degrees against 1998–2002 and uses COUNT(Degrees); the reference uses Year and SUM(Degrees), grouped by campus. The year/quantity errors are clear; campus grouping is not explicit in the printed question and must be adjudicated separately. Add fixtures distinguishing quantity totals from record counts and year ranges from amount/ID/cutoff literals. Supply independent bindings through the existing constructor, not just a final cosine adjustment.
4. **Wire the first richer reading into enumeration, then run the actual 60-case diagnostic.** Extend the existing named evidence/requirement interface for output slots, operation operands, and predicate attachment. Preserve source occurrences and scope IDs. Exact/schema/grammatical evidence and current encoder scores propose bounded typed alternatives; deterministic constraints select coherent assignments. On the same audited cases, substitute independently correct bindings at this constructor interface and record which new plans appear, are pruned, are rejected or lose. This answers the question the column-role ablation did not test.
5. **Extend the largest validated missing family and witness false refusals.** Validate the 227 raw-correct blocked cases and the remaining 362 retained-pool misses. Choose a family by correctly recoverable answers per work/resource cost, including compound/group/nested behavior once the output/predicate interface is sound. Empty-result coincidence and gold mistakes must not become evidence for ignoring qualifiers.
6. **Train only for a demonstrated consumed-reading deficit.** If correct bindings materially recover plans under the integrated constructor, use package F's frozen-representation pilot before shared adaptation. Otherwise repair construction/retention. Keep a fresh, untouched schema/composition set, since both DEV and the 1,000-question sample now influenced implementation. Claim a substantive gain only with actual net correct answers and independent generalization, not recovered refusals alone.

The latest fixes are a prerequisite for broader accuracy, not completion of the proposed semantic-binding program. To exceed the frozen Gemini point count, even selecting all 672 observed raw hits would require at least **201 additional question successes** to reach 873/1,034. This is scale arithmetic, not a proven serving ceiling or forecast. The next investment must make meaning available to construction and retain correct plans; more isolated test-case patches will not close that gap.

## October 11 follow-up actions at `6263502` (historical scope)

Continue this implementation plan through the same production owners. The latest report's S1–S6 are counterexamples and repair requirements, not a second planner or a separate implementation program.

1. **Make count requirements entity-bound and their proof exact and bounded (S1/S2/S3/S5).** Extend the existing `sql_expansion.CountThreshold` reading/shared helpers and their existing construction/checking callers together. Preserve each occurrence's counted entity/relation, filters, scope, grouping grain, distinctness and bounds; repeated numbers alone do not identify a requirement. In `query_contract`, prove the aggregate measures that population and validate the complete typed Boolean count formula, including complemented ranges and zero-related entities. Replace the literal-sized loop with exact integer-boundary/interval reasoning. Keep parent population/display identity separate from the child grouping key. A typed, interpretable witness must show which requirement each count satisfies.
   Acceptance: the zero-inclusive range lists four expected country names and its scalar version returns four; parent-ID counts and missing airport counts fail; correct child counts and separate equal-bound requirements pass. Add zero/one/many, filtered children, missing values, multiplying joins, conjunction/disjunction, equal bounds/different populations, and large/fractional-bound contrasts. Bound proof work independently of literal magnitude rather than hiding the issue behind a maximum allowed threshold.
2. **Prove membership population/cardinality, not only projected value kind (S4).** Extend the existing `_binds`/scope evidence. Plain identity projection preserves qualifying people; MAX/MIN without appropriate grouping can collapse them. Require a justified requested extremum or an identity-preserving grouping proof. Keep legitimate extrema and identity-grouped aggregates supported. Test the ordinary age population versus MAX/MIN collapse, aliases, same-name unrelated keys, computed membership, set branches, and requested-extremum controls.
3. **Finish ranking number-role binding (S6).** Extend the existing shared `ranking_cutoff` reader. A year-like integer in a cardinality phrase remains a cardinality; a numeric field/filter remains that field/filter. Keep output admission separate from interpretation. Test 999/1000/1200/2999/3000, explicit year fields, room 111, line 2, ID 101, and actual output-budget refusal through ordinary serving.
4. **Then prioritize safe recovery of the 444 unanswered questions.** Refresh candidate diagnostics using the existing production search/selection and evaluator. Separate missing construction, rejection of an independently correct plan, ranking, execution, and model-reading errors. Measure eligible candidate-pool recall with the existing `pool_oracle` ablation, and distinguish it from pre-contract construction recall. Recover valid refused plans before broadening admission. Track the 204 strict-wrong returned answers separately: 77 lenient-only and 127 incorrect under both metrics.

For each coherent repair, freeze the baseline, keep the bundle unchanged, register positive/contrastive/negative tests against the production owner, run relevant focused suites and fresh full `whole_db` evaluation, and report wins/losses, coverage, conditional correctness, candidate recall and controlled latency. Validate SQL/Python parity for affected executable operations. Use independent fixtures/held-out questions to check generalization; gold labels remain evaluation-only. Do not relax checks to manufacture coverage or train/promote merely because the score is low.

The current strict gaps to Sonnet/Gemini Flash are 454/486 answers. Approximately three quarters occur on questions the engine refuses, but even on the same 590 answered questions its strict correctness is 65.4% versus 84.1%/85.6%. The goal therefore remains safe coverage recovery plus sound interpretation and selection, preserving deterministic typed ASTs throughout.

## 0. Why determinism has not produced higher accuracy

### 0.1 The unresolved problem is interpretation, not SQL spelling

An AST specifies a program precisely. It does not establish which program a natural-language request intended. The engine must first bind words to entities, fields, relations, populations, operators, and scopes. A perfectly deterministic renderer faithfully emits an incorrectly bound AST, just as a compiler faithfully compiles the wrong algorithm.

The measured `3ef82c4` → `6263502` gain is four strict answers, representing two easy-question pairs. Medium, hard, and extra-hard strict counts did not improve. Subsequent S fixes and the count composer add no DEV strict wins in Claude's saved runs. Those repairs corrected specific construction/verification behavior; they did not demonstrate a broad improvement in compositional interpretation. This is why the headline moves slowly despite useful correctness work.

For this plan, distinguish three properties:

| Property | What it establishes | What it does not establish |
|---|---|---|
| Deterministic decisions | Repeatable choices for fixed inputs, state, configuration, weights, and numerical conditions | That the selected interpretation is correct |
| Typed construction and validation | Supported program structure, operand compatibility, scope and relationship invariants | That a human intended that particular interpretation |
| Named evidence and witnesses | Inspectable evidence, bindings, and reasons for accepting/rejecting a candidate | Full transparency of the encoder's hidden computation or proof of human intent |

Natural language can also be genuinely underspecified. A checker can establish that a program implements a chosen interpretation; it cannot manufacture missing business definitions or prove that an ambiguous question has exactly one meaning. Explicit unresolved bindings are part of a trustworthy engine.

### 0.2 Probability and entropy do not imply random program execution

A next-token model estimates conditional scores for the next token given the input and the preceding program. Selecting the highest-scoring token is a deterministic decision rule under fixed computation; sampling is a separate choice. Entropy describes the spread of a predicted distribution, not whether its highest-scoring answer is correct. A sharply concentrated distribution can confidently favor the wrong answer.

Our encoder is learned too. Named dimensions and fixed thresholds do not eliminate uncertainty, classification errors, or missing semantic distinctions. Repeating a wrong binding deterministically is reproducibility, not accuracy.

Program-sequence supervision gives a next-token model an opportunity to learn dependencies across complete examples: a projection depends on entity identity, an aggregate depends on its population, and a nested condition depends on its outer scope. Our approach can learn these dependencies through named classification/relation evidence and deterministic structured assembly instead. There is no need to generate SQL tokens to obtain richer supervision.

The current system comparison does not hold training data, objectives, model capacity, or inference presentation constant. It cannot identify a pure encoder-versus-decoder architecture effect. We should not infer undisclosed Sonnet/Gemini training details or attribute the gap to entropy.

### 0.3 Code evidence for the present information bottleneck

The relevant current owners are linked in [CLAUDE.md](C:/work/prereasoner-data/CLAUDE.md) and [SQL_AST.md](C:/work/prereasoner-data/docs/SQL_AST.md). These observations explain what to measure; they do not prove that each unanswered example has the same cause.

- `TableQuery.ast_semantic_signals` builds phrase-to-column similarities, table similarities, and calculation intent/operand evidence. `semantic_role_phrases` extracts broad role phrases using the first relevant cue, rather than representing every scoped operation occurrence.
- `CandidateRanker._model_features` averages column similarities by role. Together with handwritten structural features, this can favor relevant fields without proving that each field has the correct entity, population, or scope.
- `count_thresholds` deduplicates by predicate bounds; `counted_thresholds` also keys by those bounds. Distinct relationship requirements with equal bounds can collapse. This is a concrete loss of meaning, not simply a low similarity score.
- `SQLSearcher._expand` prunes partial drafts by accumulated scores. The default search beam is 64 and the served pool limit is 25. These are bounded searches, not completeness guarantees. Instrumentation must determine whether correct partial interpretations are discarded before the decisive scope is available.
- The checked-in property trainer optimizes named property/intent coordinates and calculation contrasts. It does not currently expose an explicit full multi-scope SQL binding objective. This observation is about the inspected training code, not a complete reconstruction of the promoted checkpoint's historical training lineage.

The six current S findings expose count-population, occurrence, membership, bounded-proof, and numeric-role gaps. More cosine features or a few score-weight changes cannot supply a plan whose meaning was never represented or constructed.

### 0.4 The architectural direction and its limits

Preserve encoder-only program inference: the model supplies fixed-vocabulary named scores over bounded, enumerated question/schema objects; deterministic code selects coherent bindings and builds every typed AST. No autoregressive SQL/AST token head, free-form plan proposal, or model-written executable code enters this path.

The proposed improvement is **structured semantic binding with deterministic composition**. It extends the existing records, relational readout, search, ranker, and contract rather than replacing them with a second engine.

There is relevant research precedent, but not a ready-made accuracy guarantee. [RAT-SQL](https://aclanthology.org/2020.acl-main.677/) studies relational schema encoding/linking. [Span-based Semantic Parsing](https://aclanthology.org/2021.acl-long.74/) uses encoder-based span scores, typed composition, and constrained chart search, and reports benefits on compositional splits of GeoQuery, SCAN, and CLOSURE. RAT-SQL uses an autoregressive program decoder; the span parser has bounded search and evaluates different tasks. Borrow their representation and composition lessons, not their benchmark numbers as predictions for our engine.

SQL is a favorable application because its operators and schema constraints are explicit. General-purpose code has a broader algorithm and API space, so the same approach needs a clearly stated supported language. Typed structure is an advantage for enforcing semantics that we can specify, not evidence that arbitrary code synthesis will be easier than language-model generation.

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

### 5.3 Concrete internal binding contract

This is a proposed internal data contract, not a new public API or a model-generated plan format. Extend the current `CountThreshold`, `QuestionRoles`, `SemanticSignals`, `Reading`, and `Coverage` responsibilities. Add a shared record only where construction and checking both need it, and migrate those callers in the same change.

First implement count/membership scope. Later apply the same occurrence and scope principles to other operators when the failure ledger justifies them.

| Component | Proposed information | Existing owner / consumers |
|---|---|---|
| Occurrence | Stable ID, original source span, kind, overlapping interpretations, lexical evidence | `sql_expansion.py` readers; `query_contract.py` obligation collection; all relevant construction/checking callers |
| Schema binding alternative | Table/column reference, entity identity, type, relationship path, evidence source, named model score | `sql_schema.py` facts; `SemanticSignals` in `sql_rank.py`; `tables.py` integration |
| Count requirement | Returned subject, counted entity or value, relationship, child filters, group grain, distinct policy, exact predicate formula | Extend `CountThreshold` and count readers in `sql_expansion.py`; consume in `sql_constraints.py` and `query_contract.py` |
| Scope | Stable scope ID, parent scope, row/group/scalar purpose, entity population, filter dependencies, correlation keys | Shared obligations in `query_contract.py`; existing recursive/constraint expanders |
| Boolean requirement | AND/OR/NOT structure over occurrence IDs, including complete range formulas | Existing typed Boolean expressions and expansion/checking owners |
| Witness | Requirement ID, stable typed-AST path, bound population/operand, correlation and multiplicity facts, rule, outcome/reason | Existing `Reading`/`Coverage`, consumed by selection diagnostics and offline evaluator |

Use original source spans, with an explicit alignment to each owner's existing normalized tokens. `question.split()` units, regex phrase tokens, and canonical schema words do not necessarily have identical indices. Do not derive occurrence identity from a lowercased word, a numeric value, or a shared `(operator, bound)` pair.

Separate the following states: recognized but unresolved; supported binding alternatives; bound and witnessed; contradictory; unsupported by the supplied data. A missing learned channel is unavailable evidence, not a fabricated zero-confidence fact. Existing exact names, grammatical evidence, and similarities remain inputs to the one binding/selection flow, not separate alternative planners.

Do not introduce one giant schema for all hypothetical SQL behavior. The first slice needs only count occurrences, entity/relationship bindings, Boolean thresholds, scope, and membership population. Subsequent operators extend the same responsibilities after their required semantics are specified.

### 5.4 One fixture that distinguishes several meanings

This is an illustrative acceptance fixture to implement and execute later, not a newly run test. Give every country, maker, and airport its own non-null key; makers/airports reference the country key. Names are display fields, not identity.

| Country | Related makers | Related airports |
|---|---:|---:|
| A | 3 | 1 |
| B | 1 | 3 |
| C | 2 | 2 |
| D | 0 | 0 |
| E | 0 | 2 |

For `Which countries have more than one maker and more than one airport?`, the binding requirements are conceptually:

```text
output: country display name
outer population: countries, identified by country key
count occurrence 1: makers related to each outer country; count > 1
count occurrence 2: airports related to each outer country; count > 1
Boolean formula: occurrence 1 AND occurrence 2
```

The correct result is C. Construction can use the existing typed membership/existence or grouped derived-table capabilities after auditing their correlation and lowering support. It must not join both child tables and count their Cartesian fan-out as independent entity populations. For C, that join produces four child pairs, although there are only two makers and two airports.

The same fixture must distinguish these contrasts:

- Replace AND with OR: expected A, B, C, E.
- Ask makers between zero and two: expected B, C, D, E, including zero-maker countries.
- Ask how many such countries: expected the one-cell scalar 4, not one maker-count row per country.
- Ask makers greater than one only: expected A, C; the airport condition cannot be invented.
- Ask airports greater than one only: expected B, C, E; the maker condition cannot be invented.
- Duplicate a country's display name while retaining distinct keys: output bag/entity semantics must follow the request, not collapse distinct countries by label.
- Add a child filter with mixed qualifying/nonqualifying makers: count only that filtered child population; preserve zero qualifying children.
- Add NULL/unrelated child relationship values and an unrelated lookup table: neither can invent or remove a valid country membership.

Register tests against actual production serving plus independently constructed wrong-AST rejection tests. The manually supplied correct AST passing is only a verifier check; the ordinary search must also construct and select it.

### 5.5 Named encoder evidence without program generation

The intended model interface is a bounded score table over already enumerated question spans and schema objects. Illustrative channels are `mentions_entity`, `mentions_field`, `operation_operand`, `counted_entity`, `relationship_direction`, `filter_attaches_to_scope`, `scope_parent`, and `output_entity`. These names are proposals, not existing trained runtime dimensions.

The encoder may score that one span refers to a maker entity or that a filter attaches to the maker population. It cannot emit SQL, AST tokens, a complete plan, an arbitrary entity name, or an invented database relationship. SchemaGraph and deterministic rules own the available objects, valid keys, permitted relationships, and executable operators.

Extend the existing request encoder/readout and `SemanticSignals` integration. Reuse context and representations within a request; do not encode every candidate SQL string or load a second model. Audit the current schema/question graph before adding another encoding pass. The existing relational attention already provides a place to incorporate question/schema relationships; a new backbone is not required for the first experiment.

A deterministic structured search chooses a coherent assignment from the scored alternatives:

```text
evidence score = named span/schema factor contributions
              + named relation/scope factor contributions
              + explicit structural preferences

feasible assignment = compatible types and keys
                   AND consistent occurrence/scope bindings
                   AND all required semantic obligations witnessed

winner = best retained feasible assignment, with explicit stable tie-breaks
```

This is not an exhaustive-search or global-optimality claim. The current bounded search remains approximate; record losses from pruning and unresolved bindings. Higher similarity cannot compensate for a proven missing qualifier or a wrong entity population. A neutral feature cannot silently turn an unsupported requirement into a satisfied one.

Interpretability means exposing which named readings contributed, which bindings were chosen, which rule composed them, and which typed node witnesses each requirement. It does not mean claiming all neural internals are understandable. Detailed benchmark traces stay offline; runtime instrumentation uses the current allowed phase/count/reason mechanism.

### 5.6 Deterministic composition, bounded proof, and semantic witnesses

Apply the first slice through the current production owners:

1. Collect distinct count occurrences and their bound subject/child populations once per request. Preserve the entire Boolean formula, not a flattened list of comparisons from any scope.
2. Construct a qualifying entity relation using existing `ConstraintQueryExpander`/recursive capabilities. Keep outer entity filters outside and child filters inside the appropriate scope. Prefer a membership/existence proof or an aggregate at the contribution grain when a join would multiply counted entities.
3. For zero-inclusive counts, enumerate the parent population and use a justified zero-preserving construction. An inner child-only GROUP BY cannot witness countries with no qualifying children. Reuse existing `without_failing_groups` where its identity/NULL guarantees hold; audit `ExistsPredicate` alternatives where appropriate rather than inserting raw SQL.
4. For an outer `how many`, reuse `qualifying_count` to count the qualifying entity relation once. For `which`, project the requested display fields with entity identity preserved internally.
5. Check the count operand, contribution grain, filters, relationship, scope, and Boolean formula against the requirement. A COUNT of the parent key grouped by that same parent is not a count of children. A count of non-null values is not automatically a count of entities.
6. Replace the count-sized proof loop with exact integer interval reasoning over nonnegative counts. For example, `0 <= count <= 2` has complement `count < 0 OR count > 2`; on nonnegative counts that reduces to `count > 2`. Preserve OR, conjunction, and polarity through the typed formula. Intervals belong to a specific counted-population variable: makers and airports remain distinct variables with their own filters, grain, and scope. Do not collapse a multi-population Boolean formula into one count interval. Handle fractional bounds exactly: `count < 1.5` admits 0 and 1. Bound proof work by formula size and numeric representation length rather than enumerating up to a literal; never truncate a bound without proving equivalence.
7. Verify membership's correlated key mapping and population preservation. Plain identity projection preserves qualifying entities; unrequested MAX/MIN can collapse them. Accept legitimate requested extrema and identity-preserving grouped cases only when their witnesses establish the requested population.
8. Carry requirement-to-AST witnesses into the existing selection record. Verify the actual serving realization, including tie transformation, instead of treating the raw draft as the final program. Constraint and completeness decisions must agree about that realization.

Use canonical typed keys including scope, entity/contribution identity, filters, Boolean formula, and output semantics for memoization. Two requirements with equal numbers remain separate. Two plans returning equal rows on one database are not thereby equivalent.

Early pruning is sound only for established incompatibilities. For unresolved semantic alternatives, retain diversity by counted entity, scope, and relational structure within a fixed work budget. Measure a budget change through the same search owner and never choose a budget by whether a candidate happens to match gold at runtime.

## 6. Ordered implementation packages and gates

Each package extends the current owners, removes superseded logic in the same change, registers regression tests, and records measured wins/losses before proceeding. The ordering is a dependency plan, not an estimate of guaranteed score gains or calendar duration. Package F becomes eligible when the first binding slice and diagnostics demonstrate a model-signal deficit; it need not wait for every construction family in E to be completed. Section 10 preserves the original concrete queue; the October 12 assessment records completed narrow repairs and the current next work.

### Package A — Reconcile the failure ledger with actual serving

**Owners:** `TableQuery.select_query`, `PoolSelection` in `sql_rank.py`, `query_contract.py`, and `spider/probe/full_eval.py`.

1. Preserve the verified baseline; freeze a new baseline if current dirty edits are included. Capture source/model/schema/input fingerprints and flags before prediction and verify them afterward. Invalidate prior cached predictions after any relevant change.
2. Extend existing selection diagnostics to distinguish constructed, retained, typed-valid, executable, grounded, constraint-satisfied, coverage-complete, selected, and finalized states. "Executable and grounded" must not be called a complete semantic admission proof.
3. Evaluate raw pool members and their serving realizations separately. `PoolSelection.candidate` currently applies `keep_ties` only to the selected member; use that same owned transformation for diagnostic realizations, rather than copying it into an oracle-only path.
4. Keep gold comparisons exclusively in evaluator-side reduction. Gold may identify a correct candidate offline; it must never select, construct, or admit a production candidate.
5. Assign one primary lost stage with secondary reasons. Distinguish no candidate built from a correct partial interpretation pruned before completion. Grade the actually served realization and link it to the original member.
6. Produce per-family, difficulty, database, and schema-width tables. Manually adjudicate a stratified sample of refusals, lenient-only answers, and both-incorrect answers against independent semantics.

Start the manual audit with 60 existing DEV cases: 20 unanswered, 20 strict-wrong returned answers (include both lenient-only and both-wrong), and 20 strict-correct controls. Spread the cases across databases and difficulty strata, and include matching reproduced families where present. Run the independently reproduced S1–S6 fixtures separately; they are not all observed DEV serving failures. Report the sample as diagnostic, not an unbiased estimate of full-DEV performance. Expand the audit when a family lacks enough evidence to choose an intervention.

Define these diagnostic counts after the same owned serving transformations:

- `raw_correct`: a constructed/retained raw candidate matches the frozen comparator offline.
- `realized_correct`: an actually realizable candidate matches after the owned finalization.
- `admissible_correct`: a realized correct candidate satisfies the complete applicable production obligations and execution limits.
- `served_correct`: the selected, finalized, executed answer matches.

Report plans not constructed separately from plans observed and discarded. Logging a finite set of discarded drafts cannot prove that an unobserved correct plan was impossible. Keep evaluator error and gold-label conflicts as separate adjudication fields.

After the shared binding record exists, run the following counterfactuals on the labeled diagnostic sample through the existing planner/evaluator only. Human/offline program annotations never enter serving or training from DEV.

| Experiment | Fixed components | Changed diagnostic input | What it resolves |
|---|---|---|---|
| Current readings | Production code, bundle, data, budgets | None | Establish sample outcomes |
| Correct semantic bindings | Same code, data, budgets, construction and selection rules | Independently annotated entity/operand/scope bindings | Which failures can be repaired by better readings under this search |
| Binding ablations | Same code/data/weights/budgets | Remove one named signal channel at a time | Whether the current owner consumes useful evidence |
| Search retention | Same readings and obligations | Predeclared bounded beam/retention settings | Which correct interpretations are lost through pruning versus missing grammar |
| Candidate oracle | Same constructed/realized/admitted pool | Evaluator-only best-candidate scoring | Ranking headroom, not serving accuracy |

Supplying correct bindings can also change enumeration/pruning, so its gain is not a pure estimate of encoder quality. Report which hypotheses it made constructible and where they still failed. If correct bindings do not permit a correct candidate, inspect construction/AST expressivity before training. If a correct candidate is rejected, repair the witness. If it is admissible but loses, repair deterministic selection.

**Exit gate:** a complete per-question ledger whose counts sum to 1,034, with raw recall, finalized recall, complete-admissible recall, and top-1 separately defined. Diagnostic correctness only; do not call this an accuracy improvement. Avoid another full baseline run when the exact frozen inputs are already valid and no behavior changed.

### Package B — Repair false acceptances before widening coverage

**Owners:** `sql_expansion.py`, `sql_constraints.py`, `sql_extrema.py`, `query_contract.py`, and their existing consumers; schema identity remains in `sql_schema.py`.

At `6263502`, preserve repaired F1–F5 and R1/R2/R3 examples as regression controls. Implement the remaining S1/S2/S3/S5 count family through sections 5.3–5.6, the S4 membership-population proof, and the S6 numeric-role fix. Do not repeat the already completed historical repairs merely because their old descriptions remain in section 4.

Add independently derived expected-answer and wrong-candidate tests before the fixes. Exercise ordinary serving for count/range/ranking cases and contract rejection plus positive serving for membership cases. Migrate all count-reading callers together, including the numeric-literal classifier in `query_contract.py`; a split migration would still merge equal-bound requirements or misclassify their numbers.

Review every affected scope traversal branch and identity consumer. If construction needs an AST capability not present, audit validation, traversal, renderer, and lowering before extending the existing typed representation. The current AST already has `SubquerySource`, `ScalarSubquery`, `ExistsPredicate`, and typed Boolean/membership nodes; missing construction should not be confused with a missing node.

**Exit gate:** all independent positive/contrastive/negative semantics pass, with fresh whole-database transitions for each coherent behavior batch. Report newly refused wrong answers as safety improvements, not accuracy gains. Explain any frozen-gold loss from the country-count label defects explicitly. A new semantic refusal is incomplete as an accuracy fix if the correct expressible plan still needs to be constructed.

### Package C — Recover valid refused plans through semantic completeness

**Owners:** `query_contract.py`, `tables.py`, and existing expansion/role helpers.

Work through the validated portion of the refreshed ledger. The recorded 199 refused-with-correct-plan and 26 wrong-with-correct-plan-blocked counts are historical hypotheses, not certified current opportunities. For each family, identify the exact unsatisfied obligation:

- Ordinary connective or noun wording: consume it through a structural reading that proves what it modifies; do not add it to a global ignored-word list.
- Field appearing in the right nested relation: prove entity mapping and scope, then accept the witness. Mere presence anywhere in SQL remains insufficient.
- Numeric/comparative wording: bind field, comparator, literal role, and population together; preserve conjunction and negation.
- Required data absent from the upload: retain the existing explicit world/delegation or clarification behavior. Do not replace "premium customers" with all customers or infer an unavailable country filter.

Preserve the existing `_choose` behavior that can select a later complete candidate; "try another candidate" is already implemented. Fix what makes the correct candidate appear incomplete. When external rewriting is enabled separately, verify original-question obligations so a rewording cannot erase meaning; deterministic headline runs keep it disabled.

**Exit gate:** independently correct refusals become correct served answers, omitted qualifiers and wrong-scope negatives still fail, and harmful false acceptances do not increase in the mandatory semantic suite. Report newly answered strict-correct and newly answered wrong separately; a higher answered count alone does not pass.

### Package D — Fix selection where the right admissible plan exists

**Owner:** `CandidateRanker`/`PoolSelection` in `sql_rank.py`, consumed by the same `TableQuery.select_query`.

Validate selection opportunities after finalization in the refreshed ledger; the earlier count of 62 is historical. Attach named binding/identity/population evidence to each alternative. Enforce established requirements before score-based selection; then prefer the best justified remaining interpretation with explicit stable tie-breakers. Inspect existing date, calculation, money-total, and duplication preferences together so one preference does not defeat another obligation.

Add contrasts where the wrong alternative executes successfully and has high lexical similarity. Do not learn an opaque selector over model-written SQL, adjust arbitrary weights to fit DEV IDs, or select whichever candidate matches gold. Test repeatability under different hash seeds and benign input permutations while respecting intentional active-source precedence.

**Exit gate:** top-1 capture of genuinely admissible correct candidates improves; finalized candidate recall is reported alongside it. A better raw pool ceiling with unchanged top-1 is headroom, not a selection gain.

### Package E — Increase construction recall and retain useful alternatives

**Owners:** `SQLSearcher`, `sql_expansion.py`, `sql_constraints.py`, `sql_recursive.py`, `sql_extrema.py`, and existing AST/grounding owners.

Start with the current count/membership/ranking slice and remaining construction failures established by the refreshed ledger. Keep historical repeated-aggregate and ranking examples as controls where already repaired. The earlier 363 no-correct-plan cases indicate scale, not a certified current classification. Select the largest validated family rather than assuming all hard questions need one feature. Audit existing representability before adding nodes.

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

**Initial training hypothesis, if the diagnostic evidence supports it:** named occurrence-to-counted-entity and scope-binding scores, consumed by the same deterministic constructor and verifier, improve count-family correctness beyond the current broad cosine evidence. Other deficits require their own measured hypotheses; do not simultaneously change model size, every objective, and the search algorithm.

Use the following experiment sequence after explicit training authorization and the repository's frozen experiment contract:

1. **Label the missing information.** Begin with an annotation pilot of 200 independently specified non-DEV question/schema items covering counted entity, qualifying parent population, child-filter scope, membership population, and Boolean composition. This is a proposed pilot size, not an existing corpus or a claim of sufficient training data. Label occurrence spans, candidate schema objects, correct bindings, scope relationships, and distinguishing negatives. Independently audit answers and annotations; scale up only after auditing label quality and learning behavior.
2. **Separate groups before generating paraphrases.** Split by source fixture/schema and composition/template provenance, provisionally 60% training, 20% calibration, 20% held-out evaluation at the group level. Record actual group/item counts and exposure to earlier training. Paraphrases, renamed schemas, and row perturbations of one fixture stay in its group. Reserve additional untouched schemas/compositions for the final generalization gate. A small or exposed holdout cannot justify a general accuracy claim.
3. **Start with frozen representations.** Freeze the current Qwen/LoRA and existing relational encoder/readout parameters. Train only the new bounded named binding readout needed for the hypothesis. This tests whether the current representation already contains useful information; it does not expose a SQL-token vocabulary or score arbitrary SQL strings.
4. **Use binding supervision.** Classify/rank occurrence-to-schema links and named scope relations, with matched hard negatives such as counting parent IDs instead of children, filtering the outer instead of inner population, and using the same bound over the wrong relationship. Joint binding correctness is the main offline metric, alongside per-channel metrics; AUC on individual coordinates alone is insufficient.
5. **Test integration before increasing capacity.** Under the same code, inputs, budgets, and scorer, replace only the candidate evidence through the existing loader's explicit offline candidate directory. Compare encoder readings, construction recall, admission, and served correctness. If better readings are ignored or pruned, repair that consumer; do not train a larger model to conceal it.
6. **Only then test representation adaptation if necessary.** If frozen-feature evidence remains weak despite sound labels and demonstrated oracle-binding headroom, preregister one separate experiment unfreezing specified LoRA/readout parameters. Retain replay and the existing property/intent/calculation gates so SQL binding cannot regress world typing, routing, or calculations. Changing backbone size is a further separate hypothesis, not part of this pilot.

The proposed loss components are:

```text
frozen-readout pilot: named binding/link loss + named scope/relation loss

later shared adaptation, if justified:
    existing anchored property/intent/calculation losses
  + weighted named binding/link loss
  + weighted named scope/relation loss
```

Freeze loss weights, seeds, split, calibration policy, changed parameters, candidate directory, and stop conditions before a run. Choose coefficients using training/calibration data, not Spider DEV or the untouched test split. A structured contrast may penalize an incompatible combination of named bindings; it must not become an opaque learned score over complete SQL or a generator of whole ASTs.

**Integration files to inspect before implementing a new channel:** `engine/encoder_model.py`, `engine/encoder_overlay.py`, `engine/sql_rank.py:SemanticSignals`, `engine/tables.py:ast_semantic_signals`, and `training/props/train_props_gpu.py`. The trainer imports `training/lib/relblock.py` and the existing packing/collation helpers under `training/train/`; audit their tensor/metadata contract too. Reuse one definition for newly shared output behavior rather than adding duplicate neural classes, loaders, or parallel training scripts. Preserve the existing promotion owner and bundle manifest.

No SQL/AST token decoding or whole-plan generation. Candidate weights remain outside `engine/data/`; training and promotion each require their repository contracts and authorization. A larger model or new rewrite provider is not the first intervention.

**Exit gate:** better joint bindings on the independently held-out groups, better integrated serving/candidate recall, all mandatory semantic controls and existing property/intent/calculation gates passing, and measured latency/memory within the preregistered envelope. Report absolute counts and uncertainty; loss reduction, coordinate AUC, or DEV score alone does not justify promotion. If the pilot fails, record the diagnosis and compact metrics, expire its unpromoted candidate, and choose the next measured bottleneck. This package is conditional, not part of the currently executed task.

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

The current `6263502` difficulty baseline is easy 137/248 strict, medium 170/446, hard 53/174, extra-hard 26/166. Coverage is 162, 259, 101, and 68 respectively. Improvements should report these strata so easy lexical rescues do not conceal stalled nested/entity semantics. Detailed earlier results in section 2 are explicitly historical.

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

### 7.4 Proposed acceptance targets for the substantive accuracy work

These targets are to make the engineering decision reviewable; they are not measurements, guaranteed gains, or authorization to train/promote. Freeze them with the actual change contract before an experiment. Do not withhold a sound correctness repair merely because it does not meet a larger accuracy target.

| Gate | Proposed criterion | Interpretation |
|---|---|---|
| Measurement correctness | All 1,034 records reconcile; instrumentation leaves every served prediction unchanged | Diagnostics are usable, not an accuracy improvement |
| Current semantic slice | All current S1–S6 reproductions and independent positive/contrastive/negative fixtures pass; search serves the expressible correct controls | Establish sounder reusable bindings, not just injected-AST acceptance |
| Substantive first accuracy package | Target at least 20 net additional strict-correct answers out of 1,034, with every meaningful loss classified and independent family improvement | Approximately 1.9 percentage points; a proposed step beyond another four-answer patch, not a forecast |
| Broadening coverage | Newly answered correct and wrong counts reported separately; qualifiers/population negatives remain rejected; reported precision has a denominator and uncertainty | No credit for coverage alone or merely passing lenient containment |
| Construction | Higher finalized/admissible recall on the targeted missing-plan family at the same work budget, or a documented recall/resource tradeoff | Requires plans that were previously unavailable, not just reordered scores |
| Selection | Higher capture of correct admissible plans with stable tie-breaks | Cannot repair absent plans |
| Performance | Establish an isolated warm-request baseline first; proposed code-only envelope is p95 and peak request memory no more than 10% above it, with unchanged input/search contracts unless separately declared | A proposed budget, not a comparison against concurrent historical timing |
| Determinism | Identical selected typed plans for identical inputs/configuration/weights under controlled numerical conditions; hash-seed and meaning-preserving perturbation controls pass | Never claim bitwise cross-hardware neural reproducibility without measuring it |
| Durable advantage | Surpass the frozen point count and demonstrate an advantage on untouched schema/composition tests with paired uncertainty and resource reporting | DEV-driven tuning alone cannot establish superiority |

Hardening that replaces a wrong answer with a refusal is valuable but contributes zero newly correct answers until construction recovers a correct plan. If the first package falls short of the substantive target, retain justified repairs and use the ledger to determine whether more construction or stronger readings are needed; do not relax obligations or tune against gold until the count rises.

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

### 10.1 Original concrete work queue at `6263502`

This queue expands packages A–G rather than adding a competing plan. Each row is a coherent implementation/change contract; adjacent rows can be combined only when their shared caller migration requires it. No dates or effort estimates are justified before the diagnostic sample and implementation audit.

| Work item | Production files/responsibility | Concrete deliverable | Completion evidence |
|---|---|---|---|
| 1. Freeze baseline and register failures | Existing provenance/evaluation owner; `tests/test_sql_ast.py`, `tests/test_query_contract.py` and applicable budget tests | Pin `6263502`, input/model/scorer/config hashes; turn S1–S6 into independently expected positive/contrastive/negative tests | Existing baseline artifacts verified; negatives fail for the documented reasons; no training or source behavior change |
| 2. Fix candidate realization diagnostics | `sql_rank.py:PoolSelection`, `tables.py:select_query`, `query_contract.py`, `spider/probe/full_eval.py` | Expose the actual owned realization and complete per-candidate admission/witness states; refresh the full ledger; annotate the 60-case sample | Served outputs identical to baseline; raw/realized/admissible/served counts separated; every record reconciles |
| 3. Preserve count occurrences and entities | `sql_expansion.py:CountThreshold/count_thresholds/counted_thresholds`; all `sql_constraints.py` and `query_contract.py` callers | Replace predicate-bound keys with occurrence/entity/scope-aware requirements; bind output subject separately from counted child | Equal-bound maker/airport requirements remain distinct; scalar/listing variants, distinct/value counts and omitted-condition negatives pass |
| 4. Repair count construction and proof together | `sql_constraints.py`, `query_contract.py`, `sql_schema.py` facts; existing AST as needed | Parent-preserving zero-inclusive plans; exact bounded Boolean count proof; count-operand/contribution witness | S1/S2/S3/S5 pass; correct ranges constructed and served; wrong parent-key counts fail; proof work independent of threshold magnitude |
| 5. Preserve membership populations | `query_contract.py:_binds` and recursive/constraint consumers | Correlation/key witness plus row-population preservation evidence; no unrequested aggregate collapse | S4 fails for wrong MAX/MIN collapse; identity membership, justified grouping, requested extrema and aliases remain supported |
| 6. Bind numeric roles explicitly | `sql_extrema.py:ranking_cutoff`, base search and shared checking readers | Interpret cutoff from occurrence/field/scope evidence, including year-like values | S6 and 999/1000/1200/2999/3000 cutoff controls pass; actual year/ID/filter literals remain their proper roles |
| 7. Run the first integrated evaluation | Existing suites and `spider/probe/full_eval.py`, same promoted bundle | Fresh full served evaluation, affected SQL/Python parity, paired transitions, recall and controlled resource measurements | No claim based on stale predictions; semantic suite passes; correct gains, hardening and performance tradeoffs separated |
| 8. Diagnose correct-binding headroom | Existing search/readout integration and evaluator-only injection | Current-reading versus independently correct-binding runs on the annotated sample; inspect remaining no-plan cases | Classify recoverable reading deficits separately from construction, pruning, checking and ranking; do not call oracle success engine accuracy |
| 9. Improve retention/one broader family | `SQLSearcher`, `CandidateRanker`, relevant existing expander and witnesses | Scope-diverse constrained expansion, or the highest-yield missing semantic family identified above | New realized/admissible correct candidates and served wins on both independent fixtures and full DEV; work-budget impact recorded |
| 10. Run one named-reading experiment, only if justified and authorized | Existing encoder/readout, shared training pipeline and promotion gates in package F | Frozen-feature binding pilot; later adaptation only under a separate supported hypothesis | Holdout and integrated gates pass; candidate remains outside runtime until separately approved promotion |
| 11. Demonstrate generalization | Existing evaluator/semantic suites and frozen reference comparison | Untouched schema/composition results, paired uncertainty, final accuracy/coverage/latency/memory report | Evidence beyond repaired DEV phrasings; no gold-informed serving or reference cherry-picking |

For work item 4, benchmark count-proof inputs with the same formula shape and increasing literal magnitude, including 100, 1,000,000, and 1,000,000,000. Exact integer interval construction may have cost in the number of formula terms, but must not enumerate all possible counts up to the bound. A large literal must not monopolize Python before execution budgeting even starts.

For work item 7, use the tests named in section 7.3 plus `tests.test_query_contract`, and run deterministic-emitter parity for affected supported operations. Run full repository/live gates when the change's scope requires them; the earlier 403 focused passes are not evidence for this future implementation. Documentation-only work does not rerun the expensive benchmark or claim those suites passed again.

### 10.2 How the diagnostics choose the next investment

```text
No correct realized candidate:
    independently correct bindings make one appear -> improve named readings/consumption
    correct partial binding was observed then pruned -> repair retention within the existing search
    correct bindings still produce no plan          -> extend deterministic construction/expressivity

Correct realized candidate exists:
    rejected for a false obligation failure         -> repair semantic witness/admission
    fully admissible but loses                      -> repair deterministic ranking/selection
    selected but changed incorrectly at finalization -> repair the owned transformation
    correct plan executes incorrectly               -> repair execution/lowering and parity
```

Several causes can coexist. Label a primary loss stage and preserve secondary reasons; do not force every example into the final `completeness`/`constraints`/`ast_search` label or infer causality from that label alone. The purpose is to avoid another cycle of training an unused signal or changing weights when the right plan is missing.

### 10.3 What would count as a major improvement

The near-term deliverable is a reusable count/entity/scope binding and proof mechanism, followed by measured recovery and construction beyond the easy-question pairs. The medium-term deliverable is higher complete-admissible recall and selection capture across nested and multi-relation families. A model upgrade is only one possible intervention after those measurements.

Track two decompositions, each with its actual denominator:

```text
strict accuracy = answered coverage × strict correctness among answered
                = complete-admissible correct-plan recall × selection capture
```

The second equality requires the actual realized/admitted pool definition in section 9; raw pool recall is not interchangeable with it. As illustrative targets, 95% coverage at 90% answer correctness gives 85.5%, and 95% complete-admissible recall at 95% capture gives 90.25%. Neither is a predicted result. A coverage increase obtained by admitting wrong plans is not an accuracy gain; complete-admissible correct-plan recall counts only independently graded correct candidates offline. Selection cannot recover meanings the search never represents.

Report progress as counts of newly correct, newly wrong, newly refused, and preserved answers, with family and difficulty strata. A major gain should include independent improvements on previously unsupported compositions, not just another collection of easy lexical rescues. The frozen reference point count of 872 requires at least 873 strict-correct answers to exceed it, followed by held-out evidence and uncertainty before claiming a broadly better system.

The intended outcome is **more independently correct answers from the same interpretable deterministic engine**. Model-generated SQL, blanket refusal suppression, changed gold labels, and provider swapping would not satisfy this implementation plan.
