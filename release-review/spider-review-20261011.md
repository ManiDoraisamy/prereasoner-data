# Claude fix review and matched Spider comparison

## October 14 independent review: `3e46a74`, output occurrences and scalar count grain

**Final fresh deterministic result: 546/1,034 (52.8%) strict, 590/1,034 (57.1%) lenient, 728/1,034 (70.4%) answered.** Claude's frozen build independently reproduces **544 strict / 588 lenient / 728 answered**. Against verified 529 it has 16 strict wins / 1 loss, a net +15 / +1.5 percentage points. Claude's changes add no newly answered questions and turn one previously incorrect answer (922) into a refusal; the gain is in already answered questions. The field-occurrence repair alone has 0 wins / 0 losses. The final combined review repair has **2 wins / 0 losses** versus Claude's build. Fixture correctness and measured DEV gains are separate evidence.

### Frozen source and reviewed changes

Reviewed commits `56af915`, `50e3b2c`, `3e46a74` after verified `4edf791`: shared field abbreviations and count-head tables; explicit second-sentence output requests/field alternatives/ordering; pruning unused multiplying count joins; opening commands versus table names; count-only output items; self-relation vocabulary; and top-1 ties when all displayed values are ranking values. Nine registered planner regressions were added. The production diff is in `query_contract`, `sql_ast`, `sql_expansion`, `sql_rank`, `sql_recursive`, and `sql_search`.

Baseline: `3e46a74d47f1c053a02738645458eb331a053128` plus the nine already-dirty files captured at review start, including the three prior code-kind/bound-field repair files. The attached `spider-output-review-20261014` checkout retains it. A second frozen `spider-occurrence-review-20261014` snapshot measures only field-occurrence repair; the final `spider-grain-review-20261014` snapshot measures both repairs and paired emitter support. The separately retained `spider-count-review-20261014` snapshot is a rejected intermediate candidate, not a headline run. All 304 tracked Python files were matched when each snapshot was frozen. Every headline full run uses `whole_db`, production `served`, SQL, cap 5,000/table, 12s soft budget, seed 0, external rewriting off, unchanged promoted bundle `b11056f8…`; no predictions were resumed.

All recorded source/input/model fingerprints match after execution: 141 for each full run and 125 for each 120-question diagnostic. All four frozen 304-file source sets, including the rejected intermediate, and common reference inputs are independently checked after execution. Fresh baseline versus Claude's saved package 15b: 0 SQL changes / 0 grade/status changes.

### Same-question accuracy comparison

| System, all 1,034 DEV questions | Strict | Lenient | Answered |
|---|---|---|---|
| Claude build before review repair | 544/1,034 (52.6%) | 588/1,034 (56.9%) | 728/1,034 (70.4%) |
| Engine after review repairs | 546/1,034 (52.8%) | 590/1,034 (57.1%) | 728/1,034 (70.4%) |
| Sonnet 5.5, saved SQL | 840/1,034 (81.2%) | 897/1,034 (86.8%) | 1,023/1,034 (98.9%) |
| Gemini 3.8 **Flash**, saved SQL | 872/1,034 (84.3%) | 904/1,034 (87.4%) | 1,034/1,034 (100.0%) |

Sonnet and Gemini are **October 7 saved SQL predictions freshly reexecuted and rescored**, not new API generation. All 1,034 question/database/gold identities match the engine set, and provider grades are unchanged. Gemini is **3.8 Flash**. These are local execution scores under this scorer, not official Spider leaderboard scores or freshly generated predictions from today's aliases.

The capped source rows, question set and scorer match. Inference inputs/budgets differ: references receive DDL/FKs and three sample rows/table; the engine receives tables/profiles and constructs ASTs. Saved Sonnet uses nonthinking generation with 1,024 output tokens; Gemini uses temperature 0 and LOW thinking. Reference SQL execution has an 8s limit; the engine has its soft prediction and SQL VM budgets. Provider weights/alias revisions are unavailable. This establishes a same-task accuracy comparison, not a current cost/speed comparison.

Strict compares normalized result-row multisets: column positions and duplicates matter; row order does not; text case and numeric values are normalized. Lenient gold-cell containment permits extra rows, columns and wrong associations. 26 strict-correct engine cases receive lenient-false because gold is empty. Execution coincidence does not establish semantic equivalence.

For the **identical 728-question subset answered by the final engine**:

| System | Strict on matched subset | Lenient on matched subset |
|---|---|---|
| Engine | 546/728 (75.0%) | 590/728 (81.0%) |
| Sonnet 5.5 | 612/728 (84.1%) | 644/728 (88.5%) |
| Gemini 3.8 Flash | 626/728 (86.0%) | 645/728 (88.6%) |

Sonnet is strict-correct on 228 engine refusals; Gemini on 246. Headline accuracy includes all 1,034 questions; conditional correctness must not be compared with a provider's overall percentage.

### Review finding R1: repeated field outputs were lost — repaired

“Find all people. List the id, name and id.” was served as two columns with complete coverage. The output reader retained three requests, but `ExpansionSupport.requested_fields` and `SQLSearcher._requested_projection` deduplicated them; `_in_list_order` could order a subset without preserving repetitions. The registered production-selector regression fails on the frozen baseline at that two-column SQL. Owners: [field requests](C:/work/prereasoner-data/engine/sql_expansion.py:317), [projection construction](C:/work/prereasoner-data/engine/sql_search.py:704), [list ordering](C:/work/prereasoner-data/engine/sql_search.py:2010), [admission](C:/work/prereasoner-data/engine/query_contract.py:516).

The existing constructors now preserve field occurrences. List ordering can repeat only an already bound ColumnRef value; anonymous count slots never invent another aggregate. The checker counts repeated singleton field obligations in actual outputs, resolving table aliases. Positive/reversed/single-field repetitions, the same-schema ordinary list, aliased incomplete/complete ASTs, and anonymous-count negatives pass. Actual SQL/Python execution preserves the repeated result positions. This is bounded occurrence hardening; complete relation-role and scope proofs remain pending.

Full DEV field-only ablation: 544 strict / 588 lenient / 728 answered, 0 wins / 0 losses. Only question 541 changes SQL; no grade or answer-status changes. It is not a claimed DEV accuracy gain.

### Review finding R2: retained count joins still multiplied the named population — repaired

Claude's dogs/treatments regression checked that Treatments remained in SQL but did not check the value. On its three-dog fixture, one dog has two treatments and another has one: the selector returns **3**, while the named dog population with any treatment contains **2**. The strengthened and new registered regressions fail before repair for that scalar COUNT(*) join.

The shared [scalar count binding](C:/work/prereasoner-data/engine/sql_expansion.py:1557) binds one named population only for simple top-level scalar COUNT(*). Construction retains qualification joins and uses `SchemaGraph.identifying_column` to obtain a unique, non-NULL column over the actual capped request tables, counting its qualified values distinctly. These are data-scoped identity/cardinality proofs, not a promise about unseen future rows; the graph is rebuilt from request data. Construction and admission share a key-cardinality proof: plain row counting is retained when each joined table matches at most one row and the root is the counted population. Potential multiplication uses the proven identifying column; a different root or unproved multiplying grain is rejected. Grouping, ranking, aliases, derived sources and distinct-request readings are excluded; nullable field counts and threshold populations retain their existing policies. This is a justified identity count, not blanket DISTINCT over arbitrary answers.

Actual serving controls cover zero/one/many children, repeated display names, a NULL child FK, reversed counted population and standalone counts. A distinct identity count over the join returns 2; it preserves qualifications and counts the requested rows once.

The existing lowerer previously rejected DISTINCT aggregates. `AggregateValue.distinct` now represents distinct COUNT once in the shared neutral plan. SQL emits COUNT(DISTINCT operand); Python uses named `DistinctCountState`, `COUNT_DISTINCT` and its finalizer, with fresh per-group state and NULL exclusion. Scalar single-column COUNT(DISTINCT column) AST lowering is supported; other distinct AST shapes still fail explicitly. Joined identity counts and empty/NULL/group-state controls pass actual SQL/Python/verification execution. No second planner, lowering route, model bundle or token generator was introduced.

The two DEV wins are **966**, treated dogs: 15 joined rows becomes **9 dogs**; and **968**, professionals who treated dogs: 15 joined rows becomes **8 professionals**. Both were already answered; coverage is unchanged. The field repair changes question 541's repeated ID but still lacks its displayed enrollment count.

Final combined repair versus baseline: strict wins [966, 968]; losses []; newly answered []; newly refused []; SQL changes [359, 541, 696, 966, 968]. Each transition and SQL/row/error pair is saved in the paired analysis, including wrong-to-refusal changes.

A preliminary count candidate rejected all joined raw scalar counts and lost three early strict matches (203, 205, 212). It was stopped and excluded from headlines. The final candidate adds the missing cardinality proof and contrast; its full run is separately verified. The rejected source patch and partial checkpoint remain diagnostic evidence.

### Question 202: keep the scored loss and explain the gold discrepancy

Claude's only strict loss versus 529 is question 202, “What are the names of airports in Aberdeen?” The capped source contains two airports with City `'Aberdeen '`. Gold compares `'Aberdeen'` and returns empty; the old flight self-join also returns empty. The new direct query compares the actual stored `'Aberdeen '` and returns Phillips AAF and Municipal. Independent execution of gold, old SQL and new SQL confirms this discrepancy on the same capped database. **The headline still counts the loss.** The scorer, gold and denominator were not changed to rescue it. This is a separately audited intent/reference discrepancy, not permission to relabel other losses.

### Assessment and next priorities

Claude has now started the proposed constructor-consumed interface: `OutputRequest` retains source spans/order, and field alternatives are consumed by construction, ranking and checking. This is meaningful progress beyond a rank-score adjustment. The implementation remains partial: explicit lists are read only in a sentence after the first; count slots are anonymous; majority field coverage is not a relation-role/scope binding; scalar count-head policy does not cover grouped/threshold/multiple-operation populations. The independently correct-binding injection experiment remains the next diagnostic before training.

Remaining disjoint cohorts: **546 strict-correct / 70 lenient-only / 112 incorrect under both / 306 unanswered**. Refusal stages: {'ast_search': 15, 'completeness': 219, 'constraints': 72}. Unanswered means no executed served answer; an executed empty result is answered.

Representative failures still present in the final run (indices are zero-based):

| Question | Observed failure | Required repair |
|---|---|---|
| 129: European countries with at least three manufacturers | Threshold counts joined model/car rows | Bind the HAVING count to manufacturer identity at country grain |
| 149: makers with more than three models | Car names multiply each model before counting | Count the requested model population once, retaining only justified qualifications |
| 541: most-enrolled student and listed outputs | The displayed enrollment count is missing; repeated ID recovery alone does not complete the answer | Give the displayed count its own occurrence/population binding; audit tie policy separately |
| 606: languages used by the fewest channels | Counts Cartoon rows at language grain | Bind the frequency/ranking count to channel identity |
| 639: non-English channel pixel ratio and country | Pixel ratio omitted; English exclusion is narrowed through an unasked TV-series relation | First-sentence output binding and predicate scope/attachment |
| 994: professional email, cell, home phone | Right fields, wrong output positions; lenient passes | Read first-sentence list ordinals |
| 995: each professional's contact fields | Home phone comes from Owners through unrelated joins | Bind every field to the explicitly requested professional role |

These are different error families. Question 994 is a presentation failure; 129/149 can be lenient-only while returning the wrong qualifying population. The cohort name does not decide the fix.

Continue on semantic errors and witnessed false refusals together. Audit the current per-question ledger after actual finalization; raw pool hits alone are not certified recoveries. Next extend the existing named count requirement to operation occurrence, counted entity/key, qualifier scope, predicate attachment and output occurrence. Start with manufacturer/model thresholds and channel-frequency populations; bind every count slot to its own population and NULL/distinct policy. Broaden output binding to first-sentence lists and explicit relation owners so professional contact fields cannot come from Owners. Preserve repeated output ordinals and distinguish displayed counts from counts used only for ranking/HAVING. Add counterfactual zero/one/many, NULL, duplicate-name and wrong-owner controls.

Then inject independently correct named bindings into the existing constructors on a frozen diagnostic set. Measure retained/pre-pruning recall, admissions and served results to separate reading deficits from construction deficits. Train existing named heads only after that evidence and a separately authorized experiment. Keep the single encoder/AST path and bundle, audit the 48-token encoding limit independently, and reserve a new untouched schema/composition acceptance set. The existing [accuracy guide](C:/work/prereasoner-data/release-review/sql-accuracy.md) remains the one plan.

Perfect refusal recovery alone reaches 852/1,034. Beating saved Gemini needs at least 873 strict; even ideal refusal recovery still needs 21 existing wrong answers repaired. This is arithmetic, not a forecast.

### Diagnostic recall, timing and verification

Final same-120-case `pool_oracle` diagnostic, first 30/difficulty (not random): 98 raw strict-hit pools / 98 under executable/grounded flags / 70 unfinalized selected-member hits; actual finalized serving on those identical questions is 69 strict / 86 answered. Prior raw hits: 98. Raw SQL omits tie finalization and flags do not certify all semantic obligations; this is not a full certified ceiling.

Final prediction timing: `{'max': 15.006, 'median': 1.902, 'n': 1034, 'p90': 4.629, 'p95': 5.903}`; 2 soft-budget exceedances. Parallel review evaluations shared CPU; this is not an isolated latency comparison. **410 focused checks pass**: SQL AST 253, query contract 57, compose 33, emitters 67. Ruff F/E9, high-severity Bandit, compilation and whitespace pass. Live Postgres, Chrome and full release gates were not run. No training, promotion, hosted API generation, commit, push or deployment by this review.

Evidence: [final run](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3e46a74-20261014/full_eval_review-qualified-count.json), [Claude baseline](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3e46a74-20261014/full_eval_review-3e46a74.json), [per-question results](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3e46a74-20261014/full_eval_per_example_review-qualified-count.json), [paired analysis](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3e46a74-20261014/review-analysis.json), [final verification](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3e46a74-20261014/verified-review-qualified-count.json), [case 202 audit](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-3e46a74-20261014/case202-audit.json). Frozen patches, before/after regression logs and reference grades are stored beside them.

Primary HEAD `3e46a74d47f1c053a02738645458eb331a053128`; source equality with final tested snapshot: True (304 tracked Python files). Nine pre-existing dirty files were preserved: `DECISIONS.md`, `docs/ARCHITECTURE.md`, `engine/query_contract.py`, `engine/sql_schema.py`, `engine/sql_search.py`, both review/guide files, `spider/results/RESULTS.md`, and `tests/test_sql_ast.py`. `sql_schema.py` is unchanged by this review. The seven newly dirty files are `sql_expansion.py`, the two deterministic emitters, `lower.py`, `operators.py`, `plan.py`, and `test_deterministic_emitters.py`; the review also updates eight of the previously dirty files. This review adds bounded runtime/neutral-plan/emitter repairs, registered tests and factual/report updates. Final status:

```text
 M DECISIONS.md
 M docs/ARCHITECTURE.md
 M engine/deterministic/emitter/py/__init__.py
 M engine/deterministic/emitter/sql/__init__.py
 M engine/deterministic/lower.py
 M engine/deterministic/operators.py
 M engine/deterministic/plan.py
 M engine/query_contract.py
 M engine/sql_expansion.py
 M engine/sql_schema.py
 M engine/sql_search.py
 M release-review/spider-review-20261011.md
 M release-review/sql-accuracy.md
 M spider/results/RESULTS.md
 M tests/test_deterministic_emitters.py
 M tests/test_sql_ast.py
```

Earlier sections below are historical.

## Latest verification: `4edf791`, field names without their kind

**Fresh deterministic result: 529/1,034 (51.2%) strict, 576/1,034 (55.7%) lenient, 729/1,034 (70.5%) answered.** Against the independently verified 527 stage: 2 strict wins / 0 losses; 527 unchanged correct / 505 unchanged wrong. The latest change is a small, useful correction, not a substantial leap in accuracy. The cumulative gain since the independently verified 497 stage is +32 answers / +3.1 percentage points; since 386 it is +143 / +13.8 points. These cumulative gains include earlier packages and must not all be credited to the latest fix.

### What was reviewed and rerun

The source is frozen `4edf7912cba6e382e2401834a411e1dce9ab8161` plus the nine files already dirty at the start. The three existing runtime edits are the previous review's code-kind/bound-field hardening in `query_contract`, `sql_schema`, and `sql_search`; this review makes no new production-code or model changes. The new Claude commit changes `_select_violations` in `engine/query_contract.py` and registers `test_a_field_asked_by_its_name_without_its_kind_is_requested`.

Its generic field-name reader now accepts the same `naming_words` form as construction: “role” can name `role_code`, and “email” can name `email_address`. It uses the existing `action_words` guard so a command such as “Return” does not request `return_date`. The shortened-name positive, ID-only negative, and command-versus-return-date contrasts pass. No question text or benchmark index is used as a serving condition.

The attached `spider-field-review-20261013` worktree retains the tested snapshot. All 304 tracked Python sources matched the primary checkout under universal-newline handling before evaluation. Fresh `whole_db`, production `served`, SQL, cap 5,000 rows/table, 12s soft budget, seed 0, external rewriting **off**, unchanged promoted bundle `b11056f8…`; no predictions were resumed. All 141 recorded source/input/model fingerprints match after the full run; the diagnostic matches all 125. The separate all-source check also passes.

Claude's saved package-12b artifacts differ from this snapshot in only the commit identifier and those three existing review repair files. Fresh versus saved package 12b: 0 SQL changes / 0 grade/status changes. The previous hardening does not become a claimed DEV accuracy gain.

Versus 527, changed SQL indices: [928, 929, 994, 995]; strict wins: [928, 929]; newly answered: []; newly refused: []. The two reported wins are paraphrases of the same professional ID/role/email request; they demonstrate one repaired family, not two independent generalization families. The nearby contact-field questions remain useful failures: 994 has the right cells in the wrong output positions, while 995 takes a home phone from Owners rather than Professionals.

### Apples-to-apples accuracy comparison

| System on all 1,034 DEV questions | Strict | Lenient | Answered |
|---|---|---|---|
| Engine, rewriting off | 529/1,034 (51.2%) | 576/1,034 (55.7%) | 729/1,034 (70.5%) |
| Sonnet 5.5, saved SQL | 840/1,034 (81.2%) | 897/1,034 (86.8%) | 1,023/1,034 (98.9%) |
| Gemini 3.8 **Flash**, saved SQL | 872/1,034 (84.3%) | 904/1,034 (87.4%) | 1,034/1,034 (100.0%) |

Both hosted references are **October 7 saved SQL predictions, freshly reexecuted and rescored in this review** with the identical capped rows, existing SQL executor, and shared scorer. All 1,034 question/database/gold identities match; no provider grades changed. No new provider API generation occurred. The available Gemini reference is **3.8 Flash**, not an unspecified Gemini variant. This is a local execution-score comparison, not official Spider leaderboard accuracy or a new generation run of today's aliases.

Questions, capped databases and scoring match. Inference inputs and budgets differ: providers received schema DDL/FKs and three sample rows/table and generated SQL; the engine receives capped tables/profiles and constructs typed ASTs. Saved Sonnet is nonthinking with a 1,024-token output limit; Gemini is temperature 0 with LOW thinking. The engine has a soft prediction/SQL VM budget; references use an 8s SQL execution limit. Provider weights/alias revisions are unavailable. These artifacts cannot establish today's relative latency or cost.

Strict compares normalized row multisets: output positions and duplicates matter, row order does not. The scorer normalizes text case and numeric values to three decimals. Lenient checks gold-cell containment, which permits extra rows/columns and incorrect associations. Empty gold is lenient-false even when strict-correct; 27 engine cases have this exception. A match on one database does not prove semantic equivalence.

On the **identical 729-question subset that the engine answers**, the comparison is:

| System | Strict on that same subset | Lenient on that same subset |
|---|---|---|
| Engine | 529/729 (72.6%) | 576/729 (79.0%) |
| Sonnet 5.5 | 613/729 (84.1%) | 645/729 (88.5%) |
| Gemini 3.8 Flash | 627/729 (86.0%) | 646/729 (88.6%) |

Conditional correctness is useful for measuring returned-answer quality; headline accuracy keeps all 1,034 questions in the denominator. Sonnet is strict-correct on 227 of the engine's refusals; Gemini on 245. This does not show those plans already exist in the engine's pool. Against Gemini, the net strict gap is 343 questions: 245 on engine refusals plus 98 on the identical answered subset. Coverage is the larger measured component, but recovering refusals alone cannot close the gap.

### Did Claude follow the proposed approach?

**Partly.** The architectural objective is preserved, and earlier packages delivered a substantial cumulative DEV recovery. The central binding experiment and constructor interface remain unfinished.

| Part of the approach | Evidence in the current implementation | Assessment |
|---|---|---|
| Deterministic, interpretable AST construction | One production planner/selector/ranker and unchanged bundle; latest fix uses shared field-name evidence | Followed |
| Repair output requirements and witnessed false refusals | Output shape/order work in earlier packages; this commit recognizes shortened field names; registered contrasts and paired runs | Implemented in parts; useful measured gains |
| Share explicit entity, operand, counted population, output occurrence and scope bindings with construction and checking | `QuestionRoles` still largely holds table/column sets; the latest contract loop accepts a named field used anywhere; base enumeration still relies heavily on lexical/schema readings | Not the complete proposed interface |
| Test independently correct bindings at the constructor boundary | Earlier oracle substituted broad gold column-role scores, without full entity/population/operand/scope assignments | The intended experiment is still pending |
| Certify recoverable refusals after actual serving finalization | Historical raw census plus bounded current pool diagnostic | Full current certified ledger is pending |
| Prove generalization on an untouched acceptance set | Claude's two 1,000-question samples have informed the fixes | Development evidence; reserve a new untouched set |

Claude's saved sample strict scores are now **357/1,000 and 354/1,000**, versus 356 and 353 immediately before this commit: one win / no loss on each, as recorded by Claude. These two samples were not independently rerun here. Their earlier results and the latest gains must not be described as performance on an untouched acceptance set. Saved rewrite-assisted DEV 616/1,034 is a different engine configuration, not the deterministic headline and not Gemini's SQL-reference score.

### Why the remaining gap persists

The disjoint current cohorts are **529 strict-correct / 74 lenient-only / 126 incorrect under both / 305 unanswered**. Refusal stages: {'ast_search': 9, 'completeness': 221, 'constraints': 75}. Unanswered means no executed served answer; an executed empty result is answered.

The implementation still confuses “this field or word is present” with “this AST realizes the requested meaning.” `_select_violations` uses `used`/`_base_refs` and sets of field alternatives; `QuestionRoles.projection_columns` is a set. These do not preserve every output occurrence, its ordinal, owning entity, and scope. Encoder role-to-column similarities help ranking, and calculation expansion consumes some signals, but that is not a complete question-to-bound-meaning interface. A high score cannot restore a plan never built or certify the population of a joined aggregate.

Current DEV examples remain: 129 counts downstream car rows for a manufacturer threshold and passes lenient scoring despite extra countries; 606 counts cartoon rows for a channel population; 541 omits a requested enrollment count and repeated ID, with tie policy a separate issue; 639 omits pixel aspect ratio and narrows an exclusion through a TV-series join; 995 projects the wrong entity's home phone. These are semantic/obligation defects, not an inevitable disadvantage of deterministic ASTs.

The data do not establish that decoder entropy is the reason a SQL generator wins. Our deterministic compiler can reliably realize the wrong or incomplete binding. Better accuracy requires richer, correctly grounded readings, sufficient typed constructions, and admission proofs. Keeping construction deterministic is compatible with improving the encoder's bounded named readings; a model must never author SQL or arbitrary AST tokens.

### What to do next, with concrete deliverables

1. **Certify today's failure ledger.** Extend the existing selector/evaluator records with which requirement failed, whether a correctly bound plan was absent, pruned, rejected or outranked, and its finalized served realization. Audit meaning on question/schema/counterfactual rows, including ties. A raw gold hit or executable query is insufficient. Rank families by witnessed recoveries and returned semantic errors, not by refusal count alone.
2. **Implement ordered, entity-bound output requirements first.** Extend the existing `sql_expansion`/`QuestionRoles` evidence with source occurrence, owning entity/relation role, expression/operand, scope and output ordinal. Consume this same record during `sql_search` construction and `query_contract` checking. For “email, cell phone and home phone of each professional,” all three outputs must bind to Professionals in the stated order; FK connectivity to Owners is not identity. Preserve repeated output occurrences and distinguish an operand used only in a filter/order/HAVING from an output. Add same-schema ID-only, wrong-owner, filter-only, repeated-field and command contrasts. Do not enforce output order by gold.
3. **Bind count populations and grain next.** Extend the current count requirements with counted entity/identity, relation path/direction, qualifying scope, NULL/distinct policy and grouping grain. For “countries in Europe with at least three manufacturers,” qualify European countries and count manufacturer identities in that scope; unrelated downstream cars cannot multiply that count. Use existing deterministic semijoin/preaggregation/identity-count constructions when they realize the obligation, rather than blanket DISTINCT. Add zero/one/many children, repeated labels, nullable keys, multiplying joins, COUNT-versus-stored-quantity-SUM and independently scoped-average fixtures.
4. **Run the actual correct-binding experiment.** Freeze and independently annotate a bounded set of failures and controls with only named nonexecutable bindings. Feed those bindings into the current constructors and checker, retaining the same grammar/ranker/model/evaluator. Record plans created, pruned, admitted and selected. If correct bindings cannot build the plan, repair typed construction/proofs. If they recover it but current readings cannot, the evidence supports a separately authorized named-head training experiment. No SQL/AST decoder, second planner, paid training or model promotion is part of this review.
5. **Validate and optimize one slice at a time.** Keep rewriting off; require positive/contrastive/negative registered tests, applicable emitter parity, fresh full DEV paired wins/losses, newly answered precision and false-admission counts, and a new untouched schema/composition acceptance set. Audit the actual 48-token encoding limit separately. Reuse the existing 8,192-entry encoding cache; compute question requirements once, reuse schema indexes, and prune impossible type/scope assignments before pool truncation. Measure warm/cold performance on an otherwise idle machine.

Work on witnessed false refusals and returned semantic errors together. Lenient-only cases require semantic audit before being treated as presentation defects. Perfect refusal recovery alone reaches 834/1,034; perfect precision at current coverage reaches 729/1,034. Both remain below the references. Beating saved Gemini needs at least 873 strict answers: even if every refusal is recovered correctly, 39 existing wrong answers still need repair. These are arithmetic bounds, not forecasts. The existing [SQL accuracy guide](C:/work/prereasoner-data/release-review/sql-accuracy.md) is the one implementation plan.

### Candidate recall, performance and validation

Current diagnostic: first 30 per difficulty, 120 questions, same indices as the previous diagnostic. **98/120 raw strict-hit pools**, 98/120 under executable/grounded flags, 63/120 unfinalized selected-member strict, versus 60/120 actual finalized served strict and 86/120 answered on those identical questions. Previous raw hits: 98/120. Raw member SQL omits tie finalization; flags do not certify all completeness/constraint obligations. This is a bounded diagnostic, not a representative full census or certified recovery ceiling.

Fresh prediction timing summary: `{'max': 8.306, 'median': 1.601, 'n': 1034, 'p90': 3.751, 'p95': 4.481}`; 0 soft-budget exceedances. Concurrent evaluations shared the machine, including this review's diagnostic. These timings do not establish an isolated production speed regression or hosted-model cost/latency comparison. The 12s prediction budget is soft, not an abandonment cutoff.

**396 focused checks passed:** SQL AST 242, query contract 57, compose 33, deterministic emitters 64. CI Ruff F/E9, high-severity Bandit, compilation and whitespace pass. Live Postgres, deployed Chrome and full release gates were not run. No new production-code edits, training, promotion, provider generation, commit, push or deployment in this follow-up.

Evidence: [full result](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-4edf791-20261013/full_eval_review-4edf791.json), [per-question predictions](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-4edf791-20261013/full_eval_per_example_review-4edf791.json), [paired analysis](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-4edf791-20261013/review-analysis.json), [post-run provenance](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-4edf791-20261013/verified-review-4edf791.json), [reference verification](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-4edf791-20261013/reference-final-verification.json). The frozen patch, source hashes, test logs and reference SQL grades are beside them.

Final primary HEAD: `4edf7912cba6e382e2401834a411e1dce9ab8161`. All-source equivalence to the tested snapshot: True (304 tracked Python files compared); differing files: []. All nine initially dirty files were preserved; only the existing review, implementation guide and canonical results document were edited by this follow-up. Final status:

```text
 M DECISIONS.md
 M docs/ARCHITECTURE.md
 M engine/query_contract.py
 M engine/sql_schema.py
 M engine/sql_search.py
 M release-review/spider-review-20261011.md
 M release-review/sql-accuracy.md
 M spider/results/RESULTS.md
 M tests/test_sql_ast.py
```

Earlier sections below are historical measurements.

## October 13 follow-up: latest Claude fixes and code-binding repair

**Fresh result: 527/1,034 (51.0%) strict, 573/1,034 (55.4%) lenient, 729/1,034 (70.5%) answered.** Claude's changes since the independently verified 497 add 30 strict answers: 30 wins / 0 losses. The additional review repair has 0 strict wins / 0 losses and 0 SQL changes on DEV. It fixes two reproduced wrong-answer paths; fixture hardening is not a claimed DEV gain.

### Source scope and independent verification

Snapshot: `b493126bc86f18568f82a8965f6dc273bcdc9458` plus the package-11 uncommitted ranker and registered tests present at approximately 17:40 Europe/Paris. The production diff since `305c99c` covers six production modules: coordinated aggregate operands; inflected/abbreviated field names and opening sort commands; quantity words versus cell values; nation/store readings, code aliases and year participles; distinct entity candidates; count restatements, longer field names; and the dirty projection/group scoring change. The broader constructor-consumed population/output/scope interface in the accuracy guide remains unimplemented.

The frozen baseline is retained in the attached `spider-fix-review-20261011` worktree. All 141 fingerprints match Claude's package-11 artifact except the commit identifier. A separate `spider-code-review-20261013` worktree adds the runtime repair in `sql_schema`, `sql_search`, and `query_contract`, plus its registered regression. Both fresh full runs use `whole_db`, production `served`, SQL, cap 5,000/table, 12s soft prediction budget, seed 0, the unchanged promoted bundle `b11056f8…`, and external rewriting off. No old predictions were resumed. All recorded source/input/model fingerprints match again after execution (141 for each full run, 125 for the diagnostic).

Fresh baseline versus Claude's saved package 11: 0 SQL changes / 0 grade/status changes. Versus the prior independent 497: 30 wins, 0 losses, 497 unchanged correct, 507 unchanged wrong; 26 newly answered and 1 newly refused. User/Claude edits were preserved. Later changes are outside this named snapshot.

### Same-question comparison

| System | Strict | Lenient | Answered |
|---|---|---|---|
| Engine, rewriting off | 527/1,034 (51.0%) | 573/1,034 (55.4%) | 729/1,034 (70.5%) |
| Sonnet 5.5, saved SQL | 840/1,034 (81.2%) | 897/1,034 (86.8%) | 1,023/1,034 (98.9%) |
| Gemini 3.8 **Flash**, saved SQL | 872/1,034 (84.3%) | 904/1,034 (87.4%) | 1,034/1,034 (100.0%) |

The **October 7 saved SQL** was reexecuted today using the existing capped loader, SQL executor and common scorer. Question/database/gold identity was checked for all 1,034 records; neither provider's grades changed. No new model API generation occurred. Gemini here is **3.8 Flash**. These are local execution scores, not official Spider leaderboard results or a new test of today's aliases.

Questions, database rows and scoring match; inference budgets and inputs differ. Providers receive DDL/FKs and three sample rows/table and generate SQL. The engine receives capped tables/profiles and constructs typed ASTs from encoder readings. Saved Sonnet uses nonthinking generation and a 1,024-token output limit; Gemini uses temperature 0 and LOW thinking. The engine's soft prediction/SQL VM budgets differ from the reference executor's 8s limit. Provider weights and alias revisions are unavailable. These artifacts establish an accuracy comparison, not a current speed/cost comparison.

Strict compares normalized result-row multisets: output positions and duplicates matter, row order does not, text case is normalized, and numbers are rounded to three decimals. Lenient permits extra rows/columns and wrong associations when gold cells occur somewhere in the result. Empty gold results receive lenient-false even when strict-correct; 27 engine answers fall into this exception. Execution matches do not prove general semantic equivalence.

For the **identical 729-question subset answered by the engine**:

| System | Strict on matched subset | Lenient on matched subset | Answered on matched subset |
|---|---|---|---|
| Engine | 527/729 (72.3%) | 573/729 (78.6%) | 729/729 (100.0%) |
| Sonnet 5.5 | 613/729 (84.1%) | 645/729 (88.5%) | 726/729 (99.6%) |
| Gemini 3.8 Flash | 627/729 (86.0%) | 646/729 (88.6%) | 729/729 (100.0%) |

Thus correctness among engine answers is 72.3% strict and 78.6% lenient; headline accuracy keeps all 1,034 in the denominator. On the 305 engine refusals, Sonnet is strict-correct on 227 and Gemini on 245. This does not show the engine already has correct plans for those cases.

| Difficulty | Engine strict | Sonnet strict | Gemini Flash strict |
|---|---|---|---|
| easy | 164/248 (66.1%) | 223/248 (89.9%) | 239/248 (96.4%) |
| medium | 229/446 (51.3%) | 355/446 (79.6%) | 364/446 (81.6%) |
| hard | 82/174 (47.1%) | 151/174 (86.8%) | 149/174 (85.6%) |
| extra | 52/166 (31.3%) | 111/166 (66.9%) | 120/166 (72.3%) |

### Reproduced defects repaired

**R1 — P1: sampled codes invent a semantic filter.** Size containing only 'M' answers “How many male players?” as a count of medium-size entries. Stage containing F/M does the same. The existing S/M/L negative test misses a single-code capped profile and an unrelated F/M domain. The new registered regression fails before repair at the Size alias assertion.

`SchemaGraph._build_value_index` and the existing qualified-code constructor now share `code_word_matches_column`: M/F needs a sex/gender field, L/R a hand/side/direction/orientation field. Unknown labels do not acquire these meanings from sampled letters. Explicit raw codes, supported Sex/Gender and handedness, and existing Y/N/boolean readings remain usable. This is a bounded naming policy, not a complete data-semantics system.

**R2 — P1: a code on another field falsely witnesses the requested meaning.** With Sex and Stage present, a typed COUNT WHERE Stage='M' candidate is accepted for “How many male players?” because the old checker finds 'M' anywhere in SQL. The production selector returns 2 where Sex gives 1. The before/after evidence runs the actual selector: after repair it rejects that wrong candidate, and normal search returns Sex='M', count 1.

`query_contract.read_question` now witnesses the alias through existing typed predicate extraction and the bound field. It preserves aliases and LOWER and distinguishes bare literal case. A projection literal or a code on another field is insufficient. The registered tests exercise ambiguous Size/Stage/Grade profiles, raw codes on the same profiles, supported fields including a single-code Gender sample, confounding Sex/Stage fields, aliases and LOWER. Complete population, Boolean and scope proofs remain future work.

Owners: [sql_schema.py](C:/work/prereasoner-data/engine/sql_schema.py), [sql_search.py](C:/work/prereasoner-data/engine/sql_search.py), [query_contract.py](C:/work/prereasoner-data/engine/query_contract.py). Regression: [test_sql_ast.py](C:/work/prereasoner-data/tests/test_sql_ast.py), `test_code_words_need_the_named_kind_and_the_bound_field`. No SQL/AST token generator, second planner or model bundle was introduced.

### Remaining weakness and next steps

Disjoint results: **527 strict-correct / 73 lenient-only / 129 incorrect under both / 305 unanswered**. Refusal stages: {'ast_search': 9, 'completeness': 221, 'constraints': 75}. Unanswered means no executed served answer; an executed empty result is answered.

Current examples still show the missing semantic bindings: DEV 129 counts joined car rows for a manufacturer threshold and passes lenient scoring despite extra countries; 606 counts cartoon rows for a TV-channel population; 541 omits the displayed enrollment count and repeated requested ID and differs from gold's single-row tie policy; 639 omits pixel aspect ratio and narrows the language exclusion through a TV-series join. Word coverage, FK connectivity and execution success do not establish the requested outputs or counted/filtered population.

Work on recoverable refusals **and** semantic errors in returned answers. Perfect refusal recovery alone reaches only 832/1034; perfect precision at current coverage reaches 729/1034. Both are below the references. Beating saved Gemini requires at least 873/1034; even perfect refusal recovery still needs 41 existing wrong answers fixed. These are arithmetic constraints, not recovery forecasts.

1. Extend the existing occurrence-based requirements and question-role owners with constructor-consumed output, entity/count-unit, operand, predicate, relation path and scope bindings. Preserve output ordinals and repeated requests. Use the same bindings to construct ASTs and discharge obligations. Add zero/one/many children, repeated labels, NULL keys, multiplying joins, output-versus-filter and scoped-average contrasts. Start with count grain and omitted outputs.
2. Produce a full current refusal ledger through the production selector: absent, pruned, correctly bound but rejected, or unresolved. Verify recoveries after tie finalization and on counterfactual rows. Do not relax a check because one raw query matches gold.
3. Independently annotate a bounded binding set and inject only named, nonexecutable labels into the existing constructors as an offline experiment. Measure construction recall, selection, admission and paired outcomes. The earlier broad gold-column-role score substitution is not this experiment. Train the existing named-head pipeline only if correct bindings recover plans the current readings cannot supply, with a separately authorized frozen training contract.
4. Classify lenient-only cases semantically first; then repair request-derived output shape/order and explicit bag/set semantics. Adjudicate gold/tie disagreements separately, keeping one scorer and gold-blind serving rules.
5. Profile an isolated workload and audit the actual 48-token production encoding limit. Test token budget alone before changing context or weights. Reuse the existing 8,192-entry encoding cache; profile repeated distinct-value scans, field linking and FK/entity scans before optimizing them. Reserve a new untouched schema/composition acceptance set.

The existing [SQL accuracy guide](C:/work/prereasoner-data/release-review/sql-accuracy.md) remains the implementation plan. Claude's saved samples are 356/1,000 and 353/1,000, versus 335 and 334 at the last independent 497 stage. These sample runs were not rerun here; both sets have informed fixes and cannot be treated as untouched acceptance sets.

### Candidate recall, performance and validation

The diagnostic repeats the same first 30 questions per difficulty (indices 0–182): 98/120 raw strict hits, 98/120 under executable/grounded flags, versus 95/120 previously. Unfinalized selected-member strict is 63/120; actual finalized serving on those identical questions is 60/120 with 86/120 answered. Raw pools omit tie finalization and eligibility flags do not certify all constraints/completeness. These are limited diagnostics, not a representative full census, a certified ceiling or 38 proven recoveries.

Full repaired-run timing: median 3.809s, p90 7.522s, p95 9.007s, max 16.920s, 12 soft-budget exceedances. Other evaluations and this review's parallel runs shared the machine. These figures establish neither a production latency regression nor a fair comparison with older hosted timings. The prediction budget is soft.

Final validation: SQL AST 241, query contract 57, compose 33, deterministic emitters 64 (**395 checks passed**), CI Ruff F/E9, high-severity Bandit, compilation and whitespace. Baseline SQL AST 240 also passed. Live Postgres, deployed Chrome and full release gates were not run. This review performed no training, promotion, commit, push, deployment or new hosted-model generation.

Evidence directory: [paired analysis](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-b493126-20261013/review-analysis.json), [fresh baseline](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-b493126-20261013/full_eval_review-b493126-pkg11.json), [fresh repaired result](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-b493126-20261013/full_eval_review-code-bindings.json), [per-question predictions](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-b493126-20261013/full_eval_per_example_review-code-bindings.json), [post-run verification](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-b493126-20261013/verified-review-code-bindings.json). Before/after fixtures and both frozen patches are stored beside these files. Older sections below are historical.

### Final repository state

The primary HEAD moved to `eeae5610de6d3384b9ae3c27ea73e47a446b9e4f` through concurrent work; this review did not commit. All 287 tracked Python files
still equal the tested repair snapshot under Python newline handling. The five files already dirty at the start were
DECISIONS.md, docs/ARCHITECTURE.md, engine/sql_rank.py, spider/results/RESULTS.md and tests/test_sql_ast.py.
Those existing package-11 changes are preserved; the ranker change is now committed by concurrent work. The review's
uncommitted changes are the three runtime repair files, its registered test, factual decision/architecture updates,
and the two review documents plus canonical results entry. No model bundle or frontend files changed.

Final `git status --short`:

```text
M DECISIONS.md
 M docs/ARCHITECTURE.md
 M engine/query_contract.py
 M engine/sql_schema.py
 M engine/sql_search.py
 M release-review/spider-review-20261011.md
 M release-review/sql-accuracy.md
 M spider/results/RESULTS.md
 M tests/test_sql_ast.py
```

Primary-source equality evidence: [primary-source-equivalence.json](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-b493126-20261013/primary-source-equivalence.json).

## October 13 independent review: completed package R and matched comparison

**Verified result:** 497/1,034 (48.1%) strict, 546/1,034 (52.8%) lenient, and 704/1,034 (68.1%) answered, with external rewriting disabled. Against October 12's independently verified 386, this is **+111 answers / +10.7 percentage points**: 116 wins and 5 losses. This is a substantial DEV gain, though the engine remains well below both SQL-generator references.

### Exact source and review scope

The first fresh full run freezes clean `e709851234f091503fb9213cda1ea57bccae6457` and independently checks the 493-answer committed build. Claude completed package R during this review, then committed it as `94c4d501113ec0f89a79772937256f00ebff8872`. Its 113 recorded serving-source fingerprints were matched byte-for-byte in a separate checkout before adding the review repairs. The final run is that audited package-R snapshot plus the three repairs below, based on `e709851`, with a dirty-worktree flag and immutable source fingerprints. Concurrent changes were preserved. After the full run, the tested repairs were applied to the clean `94c4d50` primary checkout as **uncommitted changes**. All 113 serving-source files and the regression source match the tested checkout under Python's universal-newline handling; the only byte differences outside the repairs are line endings. The attached worktree retains the immutable tested source. This report measures these named snapshots, not subsequent edits or a deployed release.

The final selected SQL/grade/answered comparison against Claude's saved package R is **0 strict wins, 0 strict losses, 0 SQL changes, 0 grade/status changes**. The review repairs must be credited only for that measured effect; correctness regressions passing on independent fixtures are separate evidence. Early interrupted main-checkout and intermediate repair runs are excluded from every reported total.

### Apples-to-apples accuracy

| System | Strict execution match | Lenient containment | Answered |
|---|---:|---:|---:|
| Engine, external rewriting off, frozen package R + review repairs | 497/1,034 (48.1%) | 546/1,034 (52.8%) | 704/1,034 (68.1%) |
| Sonnet 5.5, saved October 7 SQL, reexecuted October 13 | 840/1,034 (81.2%) | 897/1,034 (86.8%) | 1,023/1,034 (98.9%) |
| Gemini 3.8 **Flash**, saved October 7 SQL, reexecuted October 13 | 872/1,034 (84.3%) | 904/1,034 (87.4%) | 1,034/1,034 (100.0%) |

Scalar-gold correctness is 258/408 (63.2%) for the engine, 373/408 (91.4%) for Sonnet and 390/408 (95.6%) for Gemini. This also leaves a sizeable gap on one-cell numeric answers; the problem extends beyond complex output formatting.

All rows use the same 1,034 question indices, database IDs, capped rows, gold executions and repository scorer. The hosted SQL was reexecuted and regraded independently; neither provider has a changed grade relative to its saved run. No new model API calls were made. This matches the task/data/scorer; **prompts and inference budgets differ**. Hosted inputs include DDL, foreign keys and three sample rows per table; the engine profiles the capped tables. Sonnet used non-thinking generation with a 1,024-token limit; Gemini used temperature 0 and LOW thinking. The external weights/alias revision cannot be recovered from these artifacts. These are local execution scores, not official Spider leaderboard results or proof of latest provider-version performance.

| On the identical engine-answered subset | Strict | Lenient |
|---|---:|---:|
| Engine | 497/704 (70.6%) | 546/704 (77.6%) |
| Sonnet 5.5 | 587/704 (83.4%) | 621/704 (88.2%) |
| Gemini 3.8 Flash | 602/704 (85.5%) | 623/704 (88.5%) |

Conditional correctness divides by the same 704 questions for every system. It is not another all-question accuracy. Sonnet is strict-correct on 253 of the engine's 330 unanswered cases, and Gemini on 270. Across all questions, the engine is uniquely strict-correct versus Sonnet on 33 and versus Gemini on 27; Sonnet-only wins are 376, Gemini-only wins 402.

**Scorer definitions:** strict compares normalized row multisets: row order is ignored, column positions and duplicates matter, text is case-normalized and numeric values rounded to three decimals. Lenient checks gold-cell containment and can accept extra rows/columns or wrong row associations. Empty gold receives lenient-false, including 26 strict-correct engine cases; lenient is consequently not a strict superset. Answered means an executed prediction, including an empty result. Unanswered is a refusal/no admissible answer; it is counted wrong in the full denominator.

### Reproduced defects and repairs

1. **High: a ranking cutoff becomes a filter value.** On mountains A/100, B/80, C/60, D/3, E/1, `List the names of the mountains with the top 3 prominence` served D with `WHERE prominence = 3 ... LIMIT 3`, marked valid. Claude's package R adds a nearby-word exclusion. The review uses the same `ranking_cutoff` reader as construction/checking to reserve the particular cutoff occurrence. Top/bottom contrasts, an independent repeated numeric filter, and the real `prominence 3` equality all pass. This is an occurrence-role repair, not a global exclusion of the number 3. Owner: [sql_search.py](C:/Users/manid/.codex/worktrees/spider-accuracy-review-20261013/prereasoner-data/engine/sql_search.py).
2. **Medium: any nested aggregate satisfies a requested count.** `How many people have an age above the average?` was declared complete for `SELECT name ... WHERE age > (SELECT AVG(age)...)`, and for SUM(age), returning Cy or 60 instead of 1. Ordinary selection already chose the correct count on this fixture; this is a reproduced checker defect, not a claimed ordinary-serving failure. The repaired gate checks output COUNTs in the appropriate scope, including projected scalar queries. It rejects unrelated SUM/AVG/MAX and counts only in HAVING/filters. It preserves a legitimate SUM of an explicitly requested stored-count field using the existing shared `counted_measure_columns` reader: transfers 8+5 totals 13, and summing age cannot substitute. Aliases and ordinary listing controls pass. Owner: [query_contract.py](C:/Users/manid/.codex/worktrees/spider-accuracy-review-20261013/prereasoner-data/engine/query_contract.py).
3. **Medium: an unrelated FullName column disables name construction.** An Employee table with FirstName/LastName correctly answered a Calgary question until an unrelated car_makers.FullName column was added; the same question then refused. The suppression is now scoped to the table owning FullName. The requested table's own FullName still wins over its two-part fields. Owner: [sql_search.py](C:/Users/manid/.codex/worktrees/spider-accuracy-review-20261013/prereasoner-data/engine/sql_search.py).

Regressions are registered in [test_sql_ast.py](C:/Users/manid/.codex/worktrees/spider-accuracy-review-20261013/prereasoner-data/tests/test_sql_ast.py). The first two original regressions fail before repair; the original full-name serving failure is saved in `full-name-before.log`. The stored-count contrast prevented an overly restrictive COUNT-only gate from becoming the final repair.

### What remains weak

The disjoint served-result partition is **497 strict-correct / 75 lenient-only / 132 incorrect under both scorers / 330 unanswered**. Refusals comprise 246 completeness, 75 constraint, and 9 AST-search cases. This is a concrete reduction in refusals from 444 at the earlier baseline, not evidence that every new answer is trustworthy.

The constructor-consumed entity/operand/population/scope binding interface from the existing accuracy guide is still missing. Much base enumeration links lexical spans and schema names, while learned column roles act mainly in ranking. Witnessing a word from a used field/table cannot by itself prove the requested relation, counted unit or output scope. Remaining scored examples show these weaknesses: DEV 129 counts over a chain down to car rows for a manufacturer threshold; 606 counts joined cartoons for a TV-channel population; 541 omits requested outputs and mixes frequency ranking with its displayed answer. These are evidence for population/output binding work, not justification to generate SQL tokens or broadly weaken completeness.

The five losses versus the 386 baseline are DEV 37, 233, 234, 237 and 793: duplicate/bag policy, three empty-gold flight cases formerly showing the wrong entity, and output order differing from gold. Keep the common scorer unchanged. Gold/tie ambiguities need separate semantic adjudication rather than benchmark-specific ordering or DISTINCT rules.

Claude's saved sample results are 335/1,000 on the repeatedly used sample and 334/1,000 on the initially fresh sample, versus 255 and 286 earlier. These were audited as saved evidence, not freshly rerun here. The initially fresh sample has now informed diagnosis and fixes (including the top-3 case), so it cannot remain an untouched final acceptance set. DEV gains exceed the latter sample's +4.8 points; generalization remains an open weakness.

### Candidate recall, performance and validation

The production evaluator's explicit pool-oracle diagnostic covers **120 questions: the first 30 of each difficulty**, not a representative whole-DEV census. Raw retained pools have strict hits on 95/120; the evaluator's executable/grounded admission flags retain strict hits on 95/120. Same-run raw top-1 is 57/120. These raw pools omit served tie finalization and their admission flags do not certify completeness; none of these figures is headline accuracy or a fully certified serving ceiling. The full served run remains the authority.

The final serving run has median 1.871s, p90 3.815s, p95 4.775s, max 8.706s and 0 soft-budget exceedances. Other Python work and overlapping evaluations shared the machine. Provider timing is an older remote-generation measure; these timings cannot establish a fair speed advantage or regression. The 12s prediction budget is soft and does not terminate all work.

Focused suites pass: test_sql_ast 227, test_query_contract 57, test_compose 33, test_deterministic_emitters 64 (**381 checks**), plus CI Ruff F/E9, high-severity Bandit, compilation and whitespace checks. No new dual-emitter operation was introduced; SQL was evaluated and the existing emitter parity suite passed. Full live Postgres, deployed Chrome journeys and a production release gate were not run. This review performed no training, model promotion, commit, push or deployment.

All completed run inputs, the 113 tracked serving-source fingerprints and the promoted bundle are verified again after execution. Evidence: [paired analysis](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-20261013/review-analysis.json), [final full result](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-20261013/full_eval_review-pkgR-isolated-repairs.json), [per-question results](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-20261013/full_eval_per_example_review-pkgR-isolated-repairs.json), [provenance verification](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-20261013/verified-review-pkgR-isolated-repairs.json), and [primary-source equivalence](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-review-20261013/primary-source-equivalence.json). The attached [repair checkout](C:/Users/manid/.codex/worktrees/spider-accuracy-review-20261013/prereasoner-data/engine/query_contract.py) retains the actual code, now also applied in the primary owners; historical sections below describe earlier snapshots.

### Next work, preserving deterministic and interpretable SQL

Use the existing [SQL accuracy guide](C:/work/prereasoner-data/release-review/sql-accuracy.md) as the implementation plan. First bind counted populations and output occurrences, then recover witnessed false refusals, and extend missing nested/set constructions when diagnostics prove they are absent. Train only after the proposed independently correct-binding experiment demonstrates that constructor-consumed encoder readings would recover those cases. No SQL/AST token decoder is proposed.

## October 12 follow-up: implementation of the accuracy plan

**Conclusion:** Claude implemented the reproduced correctness hardening, raw candidate census, and a narrow multi-count membership composer. The proposed constructor-consumed semantic binding interface and its independently annotated experiment remain unimplemented. Saved-run comparison against the correct `6263502` baseline is **0 new DEV strict wins**, **2/1,000 sample wins and 0 losses**, and then **0 additional wins from the count composer**. The older release comparison includes the previous four DEV wins and must not be credited entirely to this stage.

Independent artifact audit: all 113 recorded tracked code fingerprints in `r126-dirty` match `3563863`; all 113 in `r127-dirty` match `f6fbf4d`. The two new sample wins are 47 and 370; 102 becomes newly answered wrong. All 1,034 census and 1,034 oracle indices are unique, without recorded tracebacks. The six ledger counts reproduce, but raw SQL was graded without serving's tie finalization, so 672 raw-correct pools and 227 blocked raw-correct cases are diagnostic counts, not certified recoveries. The corrected structure partition is 139 flat / 145 grouping-or-ordering / 40 nested / 38 set, totaling 362.

**Oracle interpretation:** `oracle_bindings.py` substitutes broad 0/1 column-role sets derived from gold, flattens scopes, and calls `_choose`. Its 535 → 544 result is the raw pre-completeness choice, with 20 wins and 11 losses. It provides no operation/count-unit/quantity, predicate-value, source-order, population, or scope binding to base enumeration. It does not execute the proposed constructor-consumed correct-binding experiment and does not establish that the encoder cannot help. The current interface largely leaves learned column evidence until ranking.

**Additional P1 serving defect:** the new `_requirement_candidates` always combines tests with AND. On Claude's own fixture, `Which countries have more than 1 car maker or more than 1 airport?` returns Chile, while USA/Japan/Chile are expected; the scalar answer is 1 rather than 3. Completeness accepts it because each count is independently witnessed. This is a clear wrong answer, independent of Spider gold or model size.

The local October 12 repair shares `count_requirement_operator` between construction and checking, builds the explicitly requested typed AND/OR, and checks the joint Boolean relationship of distinct counted populations. Mixed/unbound coordination remains unresolved. Registered ordinary-serving OR/list/scalar/zero-admitting contrasts and direct wrong-AND/wrong-OR completeness tests fail before the fix and pass after it. This is a bounded family repair, not a complete semantic-scope parser or a promised benchmark gain.

Focused post-repair validation passes: SQL AST 207, query contract 57, compose 33, deterministic emitters 64 (**361 total**), repository CI Ruff F/E9 and high-severity Bandit commands, Python compilation, and whitespace checks. The fresh full DEV run is **386/1,034 strict, 444 lenient, 590 answered**, exactly matching r127 with zero SQL/grade/answered changes and no soft-budget exceedances. All 113 tracked code fingerprints match after the run; the bundle is unchanged. Source is `f6fbf4d` plus the local uncommitted repair. The saved Claude runs above were audited, not rerun at those historical commits. Live deployment, hosted-model generation and Chrome-gate claims in the pasted report were not independently rechecked in this task.

Backend scope: the existing Python lowerer rejects the three OR membership controls as unsupported (`only one non-aggregate comparison is supported`), so this review does not claim SQL/Python parity for those shapes. No new dual-emitter operation or unsupported fallback was introduced; the SQL path is verified and the existing emitter suite passes. Artifacts are in `%TEMP%/prereasoner-claude-approach-review-20261012/`, including the full per-question results and `independent-verification.json`.

**Next implementation priority:** validated output-slot ordering and output-versus-filter roles; quantity-versus-entity aggregation and predicate attachment; then constructor-consumed named bindings on independently annotated cases. DEV 49 explicitly asks maximum weight before pet type, so always emitting the group column first is a request-order defect in that example. Sample 102 compares years against Degrees and uses COUNT rather than SUM; its reference also groups by campus, which the printed question does not explicitly request and needs separate adjudication. Do not ignore qualifiers, permute by gold, or train heads that enumeration cannot consume. The repeatedly used 1,000-question sample is now development evidence, so reserve a new untouched schema/composition holdout.

Detailed current plan and experiment gates remain in the existing [SQL accuracy guide](C:/work/prereasoner-data/release-review/sql-accuracy.md).

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
