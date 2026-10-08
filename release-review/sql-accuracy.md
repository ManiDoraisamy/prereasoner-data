# SQL accuracy: weaknesses, proposed plan, and implementation guide

**Prepared:** 2026-10-08

**Inspected checkout:** `97ac4ff5178a54e81babbb9e26927e0a0f542c6b`

**Scope:** the existing own-data planner, its recorded Spider failures, and a path to better accuracy through interpretable, deterministic construction. This is the owner's requested assessment and implementation guide, not another agent-rule file or a competing production architecture. [CLAUDE.md](C:/work/prereasoner-data/CLAUDE.md) remains the working-rule authority.

## 1. Conclusion and objective

The current accuracy deficit is evidence of incomplete language interpretation, candidate construction, and selection. It is not evidence that deterministic AST construction has an accuracy ceiling below SQL-generating language models.

The engine already has useful foundations: typed expressions, explicit foreign keys, inspectable candidate features, execution and grounding checks, a shared serving selection, and deterministic SQL/Python emission for the supported subset. Its weaknesses occur largely before execution: assigning an operation to the right operand, identifying the population being counted, preserving scope, constructing the right candidate, retaining it, and recognizing whether it actually fulfills the request.

The implementation objective is:

> Resolve a question into named, scoped, grounded semantic requirements; construct candidate ASTs deterministically from those requirements; verify the candidates against the requirements; select reproducibly; execute the resulting plan with source evidence.

The work must preserve the project's primary objective. In particular:

- No next-token SQL, Python, AST, or whole-plan decoder in serving. Moving a generative decoder's output into JSON or AST actions would still undermine the objective.
- No opaque model arbitration over generated SQL, no second planner, and no replacement of the shared serving evaluator with an easier benchmark path.
- The promoted encoder can provide explicitly identified semantic evidence. Deterministic code owns bindings, scope, AST construction, validity constraints, selection, rendering, and execution.
- The existing optional Gemini rewrite remains explicitly identified. Accuracy claimed for the deterministic engine must be measured with it disabled. A gain supplied by an external rewrite must be reported separately.
- Gold queries, gold answers, and benchmark identities never influence a serving choice. Evaluation can inspect them after prediction.
- A learned similarity is evidence rather than proof. A trace must expose both the signal and the rule that uses it. Named output dimensions provide decision-level accountability; they do not establish that every internal neural calculation is fully interpretable.

Beating a SQL generator is a measurable hypothesis, not a promised consequence of using ASTs. The report proposes how to test and pursue that hypothesis without violating the architecture.

## 2. Evidence, provenance, and limits

### 2.1 Evidence inspected

The review inspected the current owners and their callers, including:

- [TableQuery selection and semantic signals](C:/work/prereasoner-data/engine/tables.py:401).
- [SQLSearcher](C:/work/prereasoner-data/engine/sql_search.py:149), aggregate binding, grouping, pruning, and the expansion pipeline.
- [CandidateRanker and semantic-role phrases](C:/work/prereasoner-data/engine/sql_rank.py:64).
- [Request coverage and constraint checks](C:/work/prereasoner-data/engine/query_contract.py:46).
- [Schema graph and join-tree enumeration](C:/work/prereasoner-data/engine/sql_schema.py:220).
- [Literal/join grounding and duplicate-row detection](C:/work/prereasoner-data/engine/sql_grounding.py:118).
- [Typed AST validation and tie handling](C:/work/prereasoner-data/engine/sql_ast.py:381).
- [The serving-faithful evaluator](C:/work/prereasoner-data/spider/probe/full_eval.py:224) and [the shared scorer](C:/work/prereasoner-data/spider/probe/spider_eval.py:108).
- [Recorded experiments](C:/work/prereasoner-data/spider/results/RESULTS.md) and [the decision removing the SQL-writing model](C:/work/prereasoner-data/DECISIONS.md:1510).

The local October 7 engine run was inspected at the summary and per-question level. Its recorded identity is:

| Item | Recorded value |
|---|---|
| Base commit | `5f8c57c03674e96a4baf55edc7ae5db98775cfb4` |
| Worktree | Dirty; the result also fingerprints individual source files |
| Validated model-bundle hash | `b11056f8ce8bf16a44da69bf76fea238149df439d7fde52d5e1f4747a5f536c7` |
| DEV input hash | `30d64a3fccde493226df79687aed9e4a1c0129525baf44f29c0573d914d758a4` |
| Schema input hash | `61bb20aa401f03164e2d7f3b16509b7b5f79cc9c943ca7bd159046df1159e2ed` |
| Configuration | `whole_db`, `served`, SQL backend, 5,000 rows per table, external fallback disabled |
| Denominator | All 1,034 Spider DEV questions |
| Summary | [Local summary](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-worktree-5f8c57c-20261007/full_eval_worktree-5f8c57c-20261007.json) |
| Per-question records | [Local predictions and grades](C:/Users/manid/AppData/Local/Temp/prereasoner-spider-worktree-5f8c57c-20261007/full_eval_per_example_worktree-5f8c57c-20261007.json) |

At report preparation, 114 of the 116 recorded first-party Python source hashes match the checkout. `engine/master.py` and `engine/sql_search.py` differ. Consequently, the 247-correct run is a frozen observed baseline, not a new accuracy measurement of the entire current checkout. The current `sql_search.py` has no Git diff against the `e55ab00` source named by the October 7 rewrite experiments. A fresh current-checkout baseline is still the first implementation step.

No production code, model weights, rules, or evaluator behavior was changed for this report. No new full benchmark, paid inference, training, promotion, or deployment was performed. Initial unrelated modifications were `README.md`, `deploy/gcp/button.html`, and `tests/test_community_deploy.py`; the review directory already contained the earlier reports.

### 2.2 What the latest comparisons establish

The October 7 recorded comparison used the same 1,034 questions, capped databases, independent gold execution, and shared scorer. The external SQL generators received all table schemas, declared foreign keys, and three example rows per table. The engine inspected the capped tables through its own ingestion pipeline. These are comparable datasets and scoring conditions, with different input presentations and planning implementations.

All counts below have denominator **1,034**:

| System | Strict correct | Lenient correct | Answered |
|---|---:|---:|---:|
| Engine alone | 247 / 23.9% | 315 / 30.5% | 414 / 40.0% |
| Engine + Gemini rewrite | 341 / 33.0% | 446 / 43.1% | 604 / 58.4% |
| Engine + Haiku rewrite, thinking off | 289 / 27.9% | 371 / 35.9% | 493 / 47.7% |
| Engine + Sonnet rewrite | 336 / 32.5% | 432 / 41.8% | 575 / 55.6% |
| Haiku 5.5 generating SQL | 828 / 80.1% | 871 / 84.2% | 1,029 / 99.5% |
| Sonnet 5.5 generating SQL | 840 / 81.2% | 897 / 86.8% | 1,023 / 98.9% |
| Gemini 3.8 Flash generating SQL | 872 / 84.3% | 904 / 87.4% | 1,034 / 100.0% |

Sources: [rewrite experiments](C:/work/prereasoner-data/spider/results/RESULTS.md:7) and [SQL-generation comparison](C:/work/prereasoner-data/spider/results/RESULTS.md:42). External SQL-generation counts are taken from that recorded report, rather than a newly reproduced run. The rewrite summaries were also read locally and agree with its counts. The SQL-generation harness and per-question records are described as session scratchpad artifacts; recovering their complete provenance and predictions is a prerequisite for a reproducible paired victory claim.

The rewrite comparison is especially informative: a stronger language front end can recover answers, but it remains constrained by our downstream construction and verification. Gemini rewriting adds 94 strict-correct answers; it does not bridge the 625-answer gap between the engine alone and direct Gemini SQL generation. Haiku's cheaper rewrite rescues fewer answers than Gemini's measured rewrite. Changing providers is not the central accuracy plan.

The historical 7B score explains the earlier regression but is not an admissible implementation proposal. Restoring that decoder would violate the objective. Its removal also had other intervening code changes, so the historical before/after numbers are not a pure causal ablation.

### 2.3 The actual failure distribution

The inspected engine run partitions all questions into **247 strict-correct, 167 answered but strict-incorrect, and 620 unanswered**.

| Unanswered-record category | Count | What it establishes |
|---|---:|---|
| A recorded eligible pool existed, but nothing was served | 489 | A later completeness/selection refusal occurred; the record reports Gemini unavailable in these cases |
| Candidates existed, but the recorded eligible set was empty | 122 | Execution, grounding, or request constraints prevented selection; finer reasons are not separated in this summary |
| No candidate pool | 9 | Construction produced no retained candidate |
| **Total unanswered** | **620** | 60.0% of the full benchmark |

An eligible candidate is not necessarily a correct candidate. These counts do **not** establish that 489 valid answers could be recovered simply by disabling checks. They establish where to inspect losses.

Among the 414 answered questions, strict correctness is **247/414 = 59.7%** and lenient correctness is **315/414 = 76.1%**. The 167 strict misses include 68 answers accepted by lenient scoring and 99 accepted by neither metric. Output shape, extra rows, and duplicate/tie behavior need to be separated from wrong numeric or entity answers.

The reported direct Haiku comparison gets 338 strict-correct answers on those same 414 answered questions. It therefore has 490 strict-correct answers on questions the engine declined. Its net strict advantage is 581 answers: 490 from the declined subset and 91 from the answered subset. Coverage accounts for approximately 84.3% of that net gap, without implying that every refusal is incorrect.

| Difficulty | Questions | Answered | Strict correct | Strict accuracy |
|---|---:|---:|---:|---:|
| Easy | 248 | 133 | 105 | 42.3% |
| Medium | 446 | 173 | 103 | 23.1% |
| Hard | 174 | 61 | 24 | 13.8% |
| Extra hard | 166 | 47 | 15 | 9.0% |

The gap is present even on easy questions. Nested SQL alone does not explain it. The pool reaches its 25-candidate cap on **335/1,034** records; this suggests retention pressure but does not prove that a correct candidate was pruned.

## 3. Implementation weaknesses

Each finding below distinguishes observed behavior from a hypothesis that needs an ablation. Historical DEV examples are diagnostic evidence, not authorization to hard-code those questions into serving.

### W01 — Meaning is reconstructed repeatedly, without a complete shared requirement record

**Evidence:** `semantic_role_phrases`, `analyze_question`, search's mention/cue extraction, and the request contract each infer aspects of meaning. They cooperate through phrases, token positions, candidate features, and partial typed checks rather than a comprehensive shared record of required operations and scope. [Role extraction](C:/work/prereasoner-data/engine/sql_rank.py:64), [search](C:/work/prereasoner-data/engine/sql_search.py:149), [coverage](C:/work/prereasoner-data/engine/query_contract.py:55).

`semantic_role_phrases` selects the first aggregate/group/filter/order cue and extracts broad windows. `analyze_question` separately forms role assignments. Multiple operations, embedded comparisons, and repeated scopes can therefore be conflated even if individual keywords are recognized.

**Effect:** the search can construct one reading, the ranker reward another lexical interpretation, and coverage recognize the words without establishing their relationships. Additional isolated cue lists risk increasing these disagreements.

**Proposal:** extend the existing request-contract owner with immutable, non-executable semantic requirements. Give requirements stable source spans, scope identifiers, operator/operand roles, grounded alternatives, and named evidence. Pass the same record through existing search, ranking, and candidate coverage. It is interpretation metadata, not a second executable plan or planner.

**Acceptance:** operation/operand and scope decisions agree across the construction trace and candidate proof. Shared parsing does not replace independent expected-result tests: a common parser error could otherwise fool both construction and verification.

### W02 — Aggregate operators and their operands are weakly bound

**Observed:** DEV 17 asks for average and maximum stadium capacities; the engine emits `AVG(stadium.Average)` and `MAX(stadium.Capacity)`. DEV 4 asks for average/minimum/maximum singer age in France; its served query averages the unrelated `stadium.Average` column while taking minimum and maximum singer age, introducing a multi-table join.

**Code:** [aggregate choices](C:/work/prereasoner-data/engine/sql_search.py:687) and [measure targets](C:/work/prereasoner-data/engine/sql_search.py:905) construct operand options from mentions and local positioning. The AST permits the desired expressions. A numeric column named `Average` also supplies a plausible but incorrect option.

**Proposal:** represent an aggregate request as `(function, operand binding, population scope, distinctness, output position)`. Coordinated operations can share a following operand: “average and maximum capacities.” A field explicitly named “Average” must remain selectable as a field. Structural binding should take precedence over similarity to the operation word.

**Tests:** average/max of capacity with an `Average` distractor; direct selection of the `Average` field; average of that field when explicitly requested; multi-table numeric distractors; reordered coordination; singular/plural names. Use fixtures where every incorrect binding produces a different answer.

### W03 — Repeated aggregate functions are collapsed before argument scope is resolved

**Confirmed code limitation:** `_aggregate_choices` deduplicates explicit cues by function, retaining the earliest cue. Its own comment acknowledges that repeated functions collapse until argument-scope parsing becomes more precise. [Deduplication](C:/work/prereasoner-data/engine/sql_search.py:747).

**Effect:** “sum revenue and sum cost” needs two SUM expressions. Function identity alone cannot distinguish that from reinforcing paraphrases of one operation. Similar ambiguity applies to two averages with different populations.

**Proposal:** deduplicate by normalized semantic requirement, including bound operand and scope, rather than by function. Preserve the distinction between two outputs, two populations, and two phrasings of the same output. The predicate scope of conditional aggregates must also be expressible before claiming support for them.

**Acceptance:** the serving result contains both independently correct requested aggregates; negative cases do not create duplicate output columns for one repeated paraphrase. Do not patch only the words “revenue” and “cost.”

### W04 — Completeness mixes lexical recognition with semantic satisfaction

**Code evidence:** `read_question` forms `schema_words` from every schema column, includes a broad set of ordinary operation words, and computes unread wording largely by set membership. Separate checks already prove some distinct, direction, numeric, date, exclusion, and calculation constraints. Those checks are useful, but recognizing a word from the whole schema does not establish that the chosen AST uses its intended field, role, or scope. [Reading](C:/work/prereasoner-data/engine/query_contract.py:505).

**Observed refusal:** DEV 2 asks to list name/country/age ordered oldest to youngest. Its record contains 25 executable candidates, 19 eligible candidates, and no selection. The aggregate “no executable candidate” error therefore conceals a later interpretation refusal. Whether any retained candidate answers the question correctly requires pool inspection.

**Important existing behavior:** `_choose` already searches the eligible ranking for another coverage-complete candidate if the preferred candidate is incomplete. [Alternative-complete selection](C:/work/prereasoner-data/engine/tables.py:608). “Try the next complete candidate” alone is not a missing feature.

**Proposal:** establish completeness through requirement-to-AST witnesses: exactly which node fulfills a requested projection, operation, operand, polarity, comparison, ordering, and scope. Ordinary syntax can be consumed by grammatical rules; an operation word must not disappear merely because it is in a noise list. Maintain distinct reasons for unknown wording, contradictory meaning, missing operation, missing source data, and genuine ambiguity.

**Acceptance:** recover independently validated false refusals while retaining rejection of omitted qualifiers and world facts absent from the upload. Historical experiments that made unread wording advisory improved Spider counts but failed cases such as unfiltered totals for premium customers or restaurants in an absent country. They are evidence of a problem, not a releaseable solution. [Rejected experiments](C:/work/prereasoner-data/spider/results/RESULTS.md:306).

### W05 — Coarse role similarities and additive ranking can reward the wrong relationships

**Code:** `_model_features` averages role-to-column similarities; broad role windows and named structural weights contribute to an additive score. [Model features](C:/work/prereasoner-data/engine/sql_rank.py:270). The features are inspectable, but an average similarity does not prove that two separate requested operations bind to the right two operands. Some feature extraction observes only particular expression shapes and does not represent every nested role relationship.

There is also an important generation boundary: the base `_column_mentions` and `_aggregate_choices` methods take tokens/schema mentions rather than role-specific `SemanticSignals`. The default path derives those base options before the final semantic ranker. Signals also participate in calculation expansion, and optional profile expansion has its own conditions, so they are not exclusively ranking inputs. However, a useful role-to-column signal cannot recover a general base binding that lexical construction never retained. The default `ast_semantic_signals` return does not populate `sketch_profiles`, and `search_ast` does not enable the optional profile configuration. This limits how much better encoder evidence alone can improve candidate recall.

**Proposal:** first enforce semantically established requirements, then rank remaining alternatives using named evidence attached to each binding and scope. Do not allow a positive similarity or a join feature to offset a proven contradiction. Preserve uncertainty where the requirements themselves have multiple credible bindings. Record each alternative and deterministic tie-break, rather than presenting a close score as calibrated certainty.

Make existing named role evidence available when constructing bounded schema-binding alternatives, with the exact signal and rule recorded. Compare this against the current late-ranking use under the same weights. It must remain a finite, inspectable binding step; it must not become a whole-plan decoder. Do not train stronger signals before checking whether the current generation path can use them.

**Acceptance:** distinguish a present-but-misranked correct AST from a missing AST. Recover selection misses without a learned SQL arbiter or an opaque whole-plan score. Validate ranker changes on held-out schemas and contrastive fixtures.

### W06 — Early pruning and sequential expansion can remove correct interpretations

**Code:** drafts are expanded through projections, aggregates, and predicates before grouping and ordering. Completed candidates are deduplicated and clipped before recursive, constraint, extrema, parsimony, and calculation expansions. Defaults include a 64-draft beam, a base pool bounded by `max(beam_size, max_candidates * 4)`, and 25 final retained candidates. [Search pipeline](C:/work/prereasoner-data/engine/sql_search.py:149), [base pool clipping](C:/work/prereasoner-data/engine/sql_search.py:334).

**Hypothesis:** a low-scored partial binding can be necessary for the correct complete scope or join. Sequential clipping may remove it before later evidence exists. Current output records do not identify the pruning stage of a lost correct interpretation.

**Proposal:** instrument survival at each existing stage. Apply typed impossibility constraints early; defer score-only pruning until enough semantic relationships are bound. Retain bounded diversity by semantic structure and unresolved binding, with stable canonical keys. Use a shared per-request work budget and cap each retained alternative set. Stage diversity is internal to one search owner, not a competing production baseline.

**Acceptance:** demonstrate higher correct-candidate retention per unit of work. Do not assume raising every beam or candidate cap improves top-1 accuracy; it may only increase cost and distractors.

### W07 — Join connectivity and legal SQL do not establish the requested population or grain

**Observed:** DEV 26 asks which year has the most concerts. The served AST joins singer participation and groups by both year and concert ID. That changes the population and grouping from concerts per year. DEV 24 similarly introduces singer participation into a concert-frequency question.

**Code:** `join_trees` connects required table sets, with limits of eight trees, six hops, and 5,000 expansions. The grounding owner checks contradictory foreign-key pairings. Duplicate-row evidence principally concerns SUM/AVG, and selection prefers a non-duplicating candidate when available rather than universally rejecting every multiplying join. [Join trees](C:/work/prereasoner-data/engine/sql_schema.py:220), [grounding](C:/work/prereasoner-data/engine/sql_grounding.py:118), [selection preferences](C:/work/prereasoner-data/engine/sql_rank.py:686).

**Proposal:** carry returned entity, counted entity, measure source, group keys, filter roles, and intended aggregate population as semantic requirements. Derive the tables needed for those roles before joining. Use declared key/cardinality evidence when available, and distinguish structural key guarantees from uniqueness merely observed in the current rows. Prefer EXISTS for membership when multiplication is unrequested and the typed representation supports it. Use COUNT DISTINCT only when entity semantics justify it.

**Acceptance:** adding another participation row must not change the answer to “count concerts”; it may legitimately change “count participations.” A blanket “reject every fan-out” or “always use DISTINCT” would fail legitimate questions. Diagnose self-join/role-alias and repeated-table requirements separately before deciding whether a new AST capability is needed.

### W08 — Nested comparisons, Boolean scope, and quantifiers lack a comprehensive shared binding model

**Observed:** DEV 12 and 13, asking for songs by singers older than average, are unanswered despite retained candidates. Their gold queries compare singer age with a scalar average of singer age. This shows a failed interpretation/selection family; it does not establish that `ScalarSubquery` is absent. The current AST has scalar subqueries, EXISTS, IN, Boolean expressions, and set queries.

**Proposal:** bind a threshold's reference population and correlation keys explicitly. Distinguish row filtering from group filtering, “A and B” from “either A or B,” absence from NULL, and “all” from “at least one.” Assign scope identifiers to nested populations and use typed operators to compose them. Extend existing constraint/recursive/extrema expanders for measured missing families.

**Acceptance:** independent fixtures where global average, filtered average, and per-group average differ; where AND and OR differ; where universal and existential membership differ; and where an empty related population matters. Do not treat a fully representable AST as evidence that search can find it.

### W09 — Schema ambiguity is sometimes resolved before enough request meaning is available

**Code:** column mentions, value bindings, and table-copy suppression operate before full query construction. `_unasked_copies` selects representatives of repeated layouts according to explicit names, observed values, and input ordering. [Mentions](C:/work/prereasoner-data/engine/sql_search.py:562), [copy handling](C:/work/prereasoner-data/engine/sql_search.py:402).

**Risk requiring classification:** equivalent layout is not equivalent data or equivalent source meaning. An irrelevant table with a column named after an operation can distort binding. Observed-value membership is useful evidence but should not prohibit a valid zero-result filter when its literal appears nowhere.

**Proposal:** keep bounded alternatives for genuinely different source roles until request evidence resolves them. Preserve the established active-tab/source policy for otherwise ambiguous repeated uploads. Qualified schema references and exact literals outrank loose similarity. An irrelevant distractor should not change an unambiguous answer; intentional source precedence must remain explainable rather than being mistaken for accidental nondeterminism.

**Acceptance:** distractor-table and duplicate-layout fixtures, absent literals, ambiguous currencies, Unicode names, duplicate headers, and operation-like column names. Do not silently equate copies or hard-code benchmark table names.

### W10 — Current diagnostics cannot localize enough of the accuracy loss

**Evidence:** a final `ast_search` failure can mean no candidates, failed execution, wrong grounding, constraint rejection, or lexical completeness refusal. Pool caps and coarse selection counts conceal which stage lost the right plan. Current pool-oracle entries use executable-and-grounded eligibility, whereas serving ranking also applies request constraints and completeness behavior. The oracle documentation also warns that raw selected-member grading differs from served top-1 tie handling. [Pool oracle](C:/work/prereasoner-data/spider/probe/full_eval.py:343), [oracle reduction](C:/work/prereasoner-data/spider/probe/full_eval.py:410).

**Proposal:** extend the same owners' diagnostic records with reason codes and stage counters. Report raw construction recall, final retained-pool recall, constraint-admissible recall, complete-candidate recall, and served accuracy separately. Record a correct candidate's first lost stage in evaluation-only analysis. Evaluate candidate realizations using the same relevant final transformations as serving, and preserve legacy/raw oracle numbers with explicit labels during a diagnostic migration.

**Acceptance:** each error can be assigned to a primary failure category with secondary causes. Gold may annotate an offline result but must never enter search or selection. No second evaluator is required.

### W11 — Benchmark success is not yet a sufficient semantic acceptance gate

**Code facts:** strict scoring compares normalized row multisets, preserving column positions and duplicate multiplicity but ignoring row order. Numeric normalization rounds to three decimals; lenient scoring compares flattened value sets; the scalar metric checks whether the gold scalar occurs anywhere in the predicted values. [Scorer](C:/work/prereasoner-data/spider/probe/spider_eval.py:70).

**Effect:** these metrics can miss a wrong ordering, over-credit a multi-cell response containing the scalar, or conceal decimal differences. A tie-preserving correct product answer can also disagree with Spider's single-row gold answer. “Strict lower bound” and “lenient upper bound” are not mathematical guarantees of actual semantic accuracy under this normalization.

**Proposal:** preserve the existing scorer for historical comparisons and add independently specified semantic acceptance checks in existing regression tests: exact scalar shape, ordered results where requested, explicit tie policy, Decimal money, NULL/empty behavior, duplicates, and invariance under irrelevant schema/data changes. Use adversarial data variations that distinguish plausible wrong queries.

**Acceptance:** a benchmark gain must also satisfy these independent semantics. Do not weaken product tie policy, suppress duplicates, or change the scorer merely to claim victory.

## 4. Proposed interpretation contract inside existing owners

### 4.1 One shared, non-executable meaning record

Extend `engine/query_contract.py` to own the request's semantic requirements and verification record. This is a proposed internal data contract; its exact class names should follow the existing code during implementation. Reuse current span extraction, schema references, date/currency/calculation specifications, and focused expanders rather than creating another grammar beside them.

| Requirement field | Purpose |
|---|---|
| Stable requirement ID and source span | Identify the exact words that justify a decision, including repeated mentions |
| Kind | Projection, aggregate, comparison, membership, grouping, order, limit, arithmetic, source, or output unit |
| Operator and polarity | AVG vs MAX; greater vs less; includes vs excludes; AND vs OR |
| Operand alternatives | Typed schema references, entity identities, literals, or existing expression types |
| Population/scope ID | Outer rows, group, related rows, or a scalar/reference population |
| Correlation and relation roles | Which population depends on which outer entity and through which keys |
| Cardinality/grain obligations | What is counted or summed, and what must remain unique |
| Evidence | Exact name, quoted value, grammatical relation, named encoder signal, key fact, or explicit source policy |
| Resolution state | Bound, bounded alternatives, unresolved, or conflicting; confidence is not assumed calibrated |
| AST witness | The selected typed node and rule that discharge a requirement |

A schematic reading of “average and maximum capacities of all stadiums” is:

```text
population p0: stadium rows
requirement r0: AVG(stadium.Capacity), population p0, output 0
requirement r1: MAX(stadium.Capacity), population p0, output 1
shared operand evidence: both coordinated operations modify "capacities"
source evidence: "stadiums" names the requested population
```

Both operations can be constructed using current AST nodes. The `stadium.Average` distractor should be excluded by operand evidence, not a hard-coded stadium exception.

For “songs by singers older than average,” record an outer singer population, a projected song field, and a reference AVG(age) population. Whether the average is global or scoped to a stated filter must be resolved from the question; an ambiguous question must not receive invented scope.

### 4.2 Flow through the one planner

```text
question + schema + source evidence
    -> existing shared requirement extraction
    -> grounded role/operand/scope alternatives
    -> existing deterministic typed-AST search and expanders
    -> typed validity + requirement witnesses + grain constraints
    -> existing named deterministic ranker over admissible alternatives
    -> shared TableQuery selection
    -> existing final AST transformations
    -> existing SQL / supported AnalysisPlan emitters
    -> execution + independently checked result
```

The requirement record cannot execute queries or act as a new planner. `AnalysisPlan` remains the executable dual-emitter representation derived from the selected AST. No model supplies the record as an autoregressively generated whole plan.

### 4.3 Meaning checks and ambiguity

Use hard constraints only for established obligations: a proven operand mismatch, omitted explicit filter, reversed comparator, incorrect known grain, or incompatible units. Distinguish a proven contradiction from an uncertain binding. Semantic alternatives remain bounded and visible; an arbitrary rank difference should not be dressed up as certainty.

Calculate coverage once per candidate/requirement pair and reuse it during selection. The current alternative-complete behavior should be retained and improved through more faithful coverage, rather than duplicated.

An explanation should identify a decision's origin, rejected alternatives, decisive constraint, and unresolved assumptions. Keep this in the existing trace/developer diagnostics. Do not add implementation controls, processor choices, or privacy text to add-on surfaces.

## 5. Ordered implementation plan and decision gates

The work extends current owners in place. Each stage requires its own short change contract, preserves unrelated work, and completes regression checks before the next stage. The following are proposed milestones, not measured gains or authorization to train/deploy.

### Stage A — Establish the current ceiling and classify losses

**Owners:** `spider/probe/full_eval.py`, `engine/tables.py`, `engine/sql_rank.py`, existing timing and coverage owners.

1. Freeze the current source identity, source hashes, model bundle, database bytes, schemas, inputs, flags, and environment. Run an external-fallback-off `whole_db` baseline.
2. Use the existing `pool_oracle` diagnostic. Explicitly reconcile its raw eligibility and tie treatment with serving before interpreting the ceiling.
3. Add per-stage diagnostic counts and candidate rejection reasons without changing selection. Do not call this an accuracy gain.
4. Separate AST representability, construction, pruning, ranking, false refusal, execution, backend transformation, and evaluation-policy mismatches. The current 489/122/9 partition is a starting point.
5. Manually validate a stratified sample against independent meaning and perturbations. Include easy failures, wide schemas, repeated layouts, nested scopes, and each difficulty.

**Exit gate:** a current measured baseline and a quantified loss ledger. The historical 544/1,034 pool ceiling belongs to an older planner and cannot substitute for this measurement. If the correct AST is absent from most pools, prioritize binding/construction; if it is present but declined or misranked, prioritize witnesses and selection.

### Stage B — Fix operation/operand and coordinated-output interpretation

**Owners:** `query_contract.py`, `sql_expansion.py`, `sql_search.py`, `sql_rank.py`.

1. Extract operator spans and argument bindings through the existing shared parsing helpers.
2. Resolve coordinated operations sharing an operand; preserve distinct operands and output positions.
3. Replace function-only aggregate deduplication with semantic deduplication.
4. Give exact qualified operands and grammatical bindings named evidence.
   Make existing role-specific encoder evidence available to bounded operand enumeration, and measure its effect separately from a weight change.
5. Migrate overlapping aggregate-role interpretation in search, ranker, and verifier to the shared record. Remove superseded parsing logic after every live caller migrates; do not preserve competing implementations.

**Exit gate:** positive, distractor, contrastive, and negative fixtures pass through serving. A fresh full evaluation reports wins/losses and candidate recall. A parser improvement that only rephrases traces is not an accuracy win.

### Stage C — Make completeness a semantic verification step

**Owners:** `query_contract.py`, `tables.py`, `sql_rank.py`.

1. Attach requirement witnesses to candidate AST nodes, including nested scope where needed.
2. Distinguish unread syntax, missing predicates, incorrect bindings, and unsupported requirements.
3. Reuse candidate checks while preserving the current search for a complete alternative.
4. Fix each measured false-refusal family without global suppression of unknown wording.
5. Preserve original-request verification for the optional rewrite path; do not let a rewording redefine required meaning.

**Exit gate:** recover validated refusals and retain tests that reject omitted premium/customer/geographic qualifiers, wrong numeric comparisons, exclusions, and repeated ambiguous headers. Historical advisory experiments must not be promoted wholesale.

### Stage D — Bind entity population, join roles, and grouping grain

**Owners:** `sql_schema.py`, `sql_search.py`, `sql_grounding.py`, `sql_rank.py`, `query_contract.py`.

1. Establish returned/counting/measure populations before choosing a join tree.
2. Exclude joins unsupported by requested roles when the semantic contract is decisive.
3. Use atomic composite-key edges and explicit membership/correlation semantics.
4. Enforce grain against declared structural facts and independently tested data behavior.
5. Add aggregate-before-join or membership formulations in the existing AST search when the intended measure/population requires them. Do not apply universal DISTINCT or universal fan-out rejection.

**Exit gate:** duplicate-related-row and irrelevant-table perturbations preserve the correct entity-level answers while legitimately changing event-level answers. Both supported emitters agree on the same independently correct plan.

### Stage E — Extend measured missing scoped families and improve retention

**Owners:** existing `sql_constraints.py`, `sql_recursive.py`, `sql_extrema.py`, `sql_parsimony.py`, `sql_search.py`, and AST support when a capability audit demonstrates a gap.

1. Choose the largest remaining named failure family from the loss ledger, not one convenient DEV question.
2. Bind comparison populations, group filters, quantifiers, Boolean scopes, and correlation keys explicitly.
3. Measure where correct partial interpretations are lost. Introduce bounded structural diversity or deterministic constraint-guided expansion within `SQLSearcher`.
4. Compare budgets under fixed work limits. Retain minimal sufficient hypotheses rather than multiplying every lexical option.
5. Audit new AST nodes for validation, traversal, SQL rendering, coverage, lowering, and both emitter semantics. Existing AST expressiveness should be used before adding nodes.

**Exit gate:** retained and admissible recall rise, served accuracy rises, and mandatory semantic fixtures remain correct. An expanded oracle ceiling with unchanged served accuracy is headroom, not a completed accuracy improvement.

### Stage F — Improve named model evidence only if the remaining losses justify it

**Owners:** `encoder_overlay.py`, `tables.py`, the existing encoder/readout and `training/props/` pipeline. No new production model or training pipeline.

First run controlled signal ablations with identical code, data, and weights: remove a signal, inspect its binding mistakes, and compare its contribution against exact and grammatical evidence. Raw cosine is not a calibrated probability. Do not activate dormant sketch/profile machinery or change model size merely because it exists.

If an explicitly authorized training experiment is warranted, target named outputs such as operation role, operand relation, counted entity, grouping grain, membership polarity, scope relationship, and comparison population. These are bounded classifications/relations whose deterministic use can be inspected. They are not token sequences for SQL or full plans.

Use independent labels and database-disjoint validation, include hard negatives and operator-like schema names, preserve previously useful intent/property dimensions, and measure calibration plus downstream accuracy. Keep candidate artifacts in the existing experiments path and preserve the promoted bundle. Follow the repository's training contract and separate promotion approval.

**Exit gate:** the named evidence improves independently held-out binding/semantic tests and the integrated engine. No promotion based on loss reduction, property AUC, or Spider DEV accuracy alone.

### Stage G — Demonstrate a fair and durable accuracy advantage

**Owners:** the existing evaluator and regression suites; documentation records measurements in `spider/results/RESULTS.md`.

Recover complete stored reference predictions when possible. Otherwise a separately authorized reference evaluation may obtain them. Score them through the same scorer and input contract, using evaluator-only dependency injection rather than a second harness or production provider path. Freeze reference model/version, prompts, sample rows, effort, retries, and execution budgets.

Keep a frozen public comparison and a separate fresh/private test with unseen schemas and composition combinations. Spider familiarity can affect both language models and our DEV-driven rules; this report does not quantify contamination or use it to dismiss the observed gap.

**Exit gate:** external-fallback-off engine accuracy exceeds the frozen reference on paired questions and independently held-out semantics, with uncertainty, strata, correctness regressions, and resource costs reported. Training/promotion/publishing remain separately controlled actions.

## 6. Implementation recipes

### 6.1 Diagnose before modifying selection

Extend the current selection/evaluator records with stable reason codes and stage facts:

```text
constructed -> retained -> type_valid -> executable -> grounded
            -> request_constraints -> complete -> selected -> served_transform
```

Record which checks apply, not a single overloaded `eligible` flag. Suggested reasons include missing operand binding, missing projection, comparison mismatch, unsupported population scope, literal misbinding, row multiplication at the requested grain, unknown wording, work-budget pruning, execution failure, and genuine ambiguity. Candidate indices/canonical structural IDs link the facts; benchmark question IDs remain evaluation-only.

Production timing/logging must follow `request_timing.py`: record permitted counts, phases, and reason codes without logging questions, prompts, source cells, or replies. Detailed public-benchmark records and existing authorized response traces are different surfaces from runtime logs; diagnostics must preserve that boundary.

For an offline error, ask in order:

1. Can the existing typed representation express the independently expected meaning?
2. Was that semantic family activated by the question interpretation?
3. Were its schema bindings present before pruning?
4. Did a correct candidate survive the relevant search stages?
5. Which production check rejected it, or which alternative outranked it?
6. Did final tie/output/backend behavior change the result?

A correct-looking query on one fixture is insufficient. Validate its meaning on counterexample rows before declaring a guard false positive.

### 6.2 Migrate semantic requirements without introducing a parallel grammar

1. Start with the highest-value aggregate binding family. Store its typed evidence in `query_contract.py`; reuse `sql_expansion.py`'s existing extraction and schema-linking primitives.
2. Let `SQLSearcher` consume it and construct current `Aggregate`/`SelectItem` nodes.
3. Let `CandidateRanker` consume the same binding evidence; keep deterministic feature names and tie-breakers.
4. Let coverage inspect actual AST structure against those requirements.
5. Update every caller in the same coherent family change and remove superseded overlapping inference. Avoid import cycles: request records should depend on AST/schema primitives and focused helpers, not import the entire search/ranker to reconstruct meaning again.
6. Expand to further requirement kinds only after the first family's measured acceptance gates pass.

A requirement's typed alternatives can remain unresolved until existing search binds them. The schema contract must be rich enough to preserve ambiguity, rather than quietly selecting one source in the parser and claiming search proved it.

### 6.3 Use structural identity and justified pruning

Define deterministic structural keys using bound references, scope, operator, polarity, population, grouping, and output order. Do not merge ASTs merely because they happen to produce the same rows on the present fixture. Do not reorder Boolean terms, projections, or expressions unless equivalence under NULL/bag semantics and the product contract is established.

Safe early rejection includes type incompatibility, proven contradictory literals, impossible required relationships, and violation of an established semantic obligation. A weaker similarity alone is not proof that a partial candidate cannot become the correct complete plan. When a budget prunes it, record that fact.

Keep bounded alternatives per semantic structure and share a deterministic expansion budget. Use counts of expansions/retained states rather than elapsed wall-clock time to define ordinary search behavior. Runtime deadlines may still abort work; expose those cases explicitly. The evaluator's `--timeout` is a soft measurement budget, not proof that every over-budget prediction was canceled.

### 6.4 Verify at the intended grain

For every aggregate, identify the population being aggregated and the identity needed to count or preserve each contribution. Derive uniqueness from declared keys when present; validate observed uniqueness separately. Keep composite keys atomic. A SUM over line items can legitimately repeat an order ID; a SUM of order totals after a one-to-many line-item join usually changes the intended measure. A DISTINCT SUM can wrongly merge equal-valued orders and is not a general repair.

Check COUNT(*), COUNT(non-null field), COUNT(DISTINCT entity), WHERE, HAVING, and membership separately. Empty populations, NULLs, ties, and repeated equal values must appear in fixtures. Record uncertainty when a key or population cannot be established.

### 6.5 Preserve interpretation and execution determinism

Pin model artifacts, thresholds, source/schema normalization, numeric handling, and supported runtime configuration. Sort canonical alternative keys before stable selection. Test different Python hash seeds. Validate permutations of unambiguous input tables/rows and preserve intentional active-source precedence where ambiguity exists. Do not promise cross-hardware bitwise equality of neural similarities without measuring it; test decision repeatability on supported deployment environments and expose marginal binding decisions.

Use existing typed AST traversals rather than parsing rendered SQL to recover semantic obligations. SQL formatting and quoting must not determine whether a request is considered satisfied. Preserve the existing source-provenance and calculation trace conventions.

### 6.6 Keep SQL/Python parity as an execution check

The same selected AST must lower to the same existing `AnalysisPlan` and both supported emitters. A shared wrong plan can produce identical SQL and Python results, so parity is necessary for supported backend equivalence and insufficient for natural-language correctness.

Every affected operation needs independent expected-result fixtures in addition to parity. Shapes outside the supported dual subset must follow the documented execution policy; adding another planner to make Python pass is not acceptable.

## 7. Concrete regression and held-out test matrix

Add tests to the existing suites and explicit `TESTS` lists where required. Use the serving entry point, plus focused unit tests for semantic requirements and AST witnesses. Derive expected values independently; use data that makes incorrect alternatives disagree.

| Family | Positive case | Required contrast/negative | Independent check |
|---|---|---|---|
| Operation vs column name | Average/max capacities | List field named Average; average that field explicitly | Correct operands and two scalar outputs |
| Repeated operation | SUM revenue and SUM cost | Repeated paraphrase of one SUM; separate populations | Separate bindings/scopes and requested output count |
| Shared coordinated operand | AVG/MIN/MAX age | AVG age plus MAX salary | Every operation uses its specified operand |
| Projection vs filter | List names of singers from France | List names and countries; country filter absent from data | Projection shape and exact filtered rows |
| Comparator binding | Amount over 100 | Age over 100; under 100; exactly 100 | Correct field, operator, boundary, and scope |
| Sorting | Oldest to youngest | Youngest to oldest; explicit ordering field distinct from projection | Ordered rows, not only row multiset |
| Ranking grain | Year with most concerts | Concert with most participants; newest concert year | Count population and group grain |
| Entity vs event count | Count customers with orders | Count orders; customers with multiple orders | Duplicate-child perturbation |
| Aggregate fan-out | Sum order amounts for qualifying orders | Sum line amounts; equal-valued orders | Each intended contribution counted once |
| Composite relationship | Join through both key components | Same first key, different second key | Exact related-row identity |
| Membership | Customers with qualifying orders | Without qualifying orders; no related rows; NULL keys | EXISTS/absence semantics |
| Universal quantifier | Customers whose orders all qualify | At least one qualifies; mixed orders; no orders | Explicit empty-population policy |
| Reference average | Age above global average | Above filtered/per-group average | Deliberately different reference populations |
| Row vs group filter | Orders above 100 | Customers whose total orders exceed 100 | WHERE versus HAVING meaning |
| Boolean scope | A AND (B OR C) | (A AND B) OR C | Rows distinguishing the two scopes |
| Output currency vs source filter | Total converted to GBP | Count rows whose currency is GBP | Unit and source-population checks |
| Dates | After/before a stated date | Boundary equality; grouping by month across years | Exact boundary and year-month grain |
| Literal containment | Contains literal a_b or a%b | Exact equality; wildcards treated literally | Escape behavior and exact values |
| Ambiguous sources | Qualified measure in named table | Repeated layouts, conflicting sources, active-tab policy | No silent source substitution |
| Distractor invariance | Unambiguous aggregate | Add unrelated table with Average/Count/Total fields | Same AST meaning and answer |
| Numeric/NULL semantics | Decimal aggregate | Empty set, NULLs, malformed numeric text, negative zero | Exact fixture-defined numeric behavior |
| Tie policy | Most frequent entity with tied winners | Explicit single-result request and top-N | Product policy and stable ordering |
| World boundary | Filter using a provided country field | Country/premium qualifier missing from upload | Correct delegation/refusal, no unfiltered answer |

Hold out schema families and some combinations of these roles. A corpus that repeats the same templates with different literal values is not a strong compositional-generalization test. Include natural-language paraphrases, misleading labels, renamed schemas with explicit meaning, and hidden perturbations.

## 8. Evaluation targets and a credible victory claim

### 8.1 Fixed denominators and meaningful targets

The frozen reported references have 828 strict-correct answers for Haiku, 840 for Sonnet, and 872 for Gemini. On the unchanged 1,034-question scoring contract, at least **829** beats Haiku's point count; at least **873** beats the strongest recorded point count. Neither single-answer margin establishes a statistically convincing or broadly general advantage.

A stronger proposed target is at least **934/1,034 strict correct (90.3%)**, with independent semantic gates and held-out replication. This is an engineering target, not a forecast.

Let `R` be the number of questions with at least one candidate that passes the applicable production admission checks and is strict-correct under the frozen scorer after serving's relevant final transformations. Let `C` be the strict-correct served count under that same scorer, with every served candidate subject to those admission checks. Then:

```text
strict accuracy = (R / N) * (C / R), for R > 0
```

This identity separates admissible recall from selection capture. If recall reaches at least 983/1,034 (95.1%) and selection captures at least 95% of those cases, the count must be at least 934. The hard work is establishing that those targets are reachable and improving both factors. A raw pre-guard oracle is not automatically `R`. Independent semantic tests remain necessary: passing the engine's admission checks and matching a reference on one database do not establish true semantic equivalence.

Use intermediate measured milestones to reassess feasibility. Do not claim that a ranking improvement can exceed the candidate recall ceiling, that permissive refusals are precision gains, or that typed validity guarantees language correctness.

### 8.2 Required scorecard for each meaningful change

- Strict, lenient, exact-scalar supplemental checks, and answer coverage with counts/denominators.
- Raw retained-pool recall, request-admissible recall, and served selection capture with precise definitions.
- Strict wins, losses, unchanged correct, unchanged wrong; highlight harmful omitted-constraint regressions separately.
- False refusals verified independently, correct refusals, wrong served answers, and execution/budget failures.
- Difficulty, schema width, source ambiguity, joins, aggregates, nested scopes, and semantic failure family.
- Median/p90/p95 latency, encode/search/execute/grounding timing, candidate counts, expansion work, and memory where relevant.
- Source/model/input hashes, fallback status, machine/runtime conditions, and comparison provenance.

An isolated change should deliver a positive measured strict gain, retain mandatory semantic fixtures, and stay within an agreed resource envelope. For prioritization, a net gain of at least ten strict-correct questions on this frozen set is a useful proposed engineering threshold, not a license to overfit DEV or ignore even one serious correctness regression. Small infrastructure changes can be worthwhile with zero accuracy gain if labeled accordingly.

For a final comparison, use paired wins/losses with a paired significance analysis and confidence intervals. Account for correlation within the 20 DEV databases, for example through database-level resampling in addition to question-level paired analysis. Predefine the acceptance criterion before inspecting the final holdout. Report the sample's uncertainty; 1,034 questions are not 1,034 wholly independent database domains.

### 8.3 Reproducible commands using the existing evaluator

The following commands are for future implementation validation. They were not run for this report. Output goes outside committed results until a measured entry is intentionally documented. Use a fresh unique tag/path per changed contract; `--resume` is valid only when every recorded input and flag matches.

```powershell
$env:EXTERNAL_LLM_ENABLED = '0'
$accuracyRun = Join-Path $env:TEMP 'prereasoner-sql-accuracy-baseline-<unique-tag>'
.venv\Scripts\python.exe spider\probe\full_eval.py `
  --data spider\data --dbs spider\data\dbs --out $accuracyRun `
  --config whole_db --selection served --backend sql `
  --cap 5000 --timeout 12 --tag '<unique-tag>'
```

For a fresh oracle diagnostic, use a different output path/tag and the existing `--selection pool_oracle --backend sql` option. Its current eligibility and tie caveats must remain visible until diagnostics are aligned. Do not publish `gold_tables` oracle accuracy as ordinary whole-database accuracy. Do not change the production search budget only for the headline run.

After planner changes:

```powershell
.venv\Scripts\python.exe -m tests.test_sql_ast
.venv\Scripts\python.exe -m compileall -q engine db training tests orchestrator mcp_server
```

Run calculation, numeric, query-contract, deterministic-emitter, and request-budget suites relevant to the touched owner; run `tests.test_compose` when routing/decomposition interactions change. Before repository-wide completion, run the applicable `tests.run_all` checks and report skipped live suites explicitly. SQL/Python verification uses the existing evaluator's `--backend verify` on the applicable supported cases. A major release additionally requires the documented Chrome demo-dataset gate, including existing conversations.

### 8.4 Fair reference comparison

Use identical questions, database snapshots/caps, declared schema facts, gold execution, scorer, and failure accounting. Document each system's data access and schema serialization. A row-aware engine and a schema-plus-sample-row LLM have different information interfaces; preserve that distinction rather than calling their prompts identical.

For saved external predictions, verify model identity, input fingerprints, generation settings, answer count, and retry policy. Preserve execution errors as failures. Do not select whichever of several generated queries matches gold. A reference query may be executed by the evaluator without becoming an allowed production path.

Supplement fixed Spider results with fresh/private schemas and adversarial data variations. Report correctness, coverage, latency, cost, and the interpretability trace together. The objective is a reproducible semantic advantage under the project's constraints, not a leaderboard number obtained by changing those constraints.

## 9. Performance and operational constraints

More accuracy must not require indiscriminate combinatorial search. Prioritize:

1. Building the schema graph and request requirements once and reusing them.
2. Caching candidate witnesses and immutable request-scoped evidence within the existing owners.
3. Pruning typed impossibilities before expensive join construction/execution.
4. Sharing equivalent bound subexpressions through canonical structural keys, only under proven semantic equivalence.
5. Executing retained candidates through the existing shared connection and budgets.
6. Measuring encoder work separately: the inspected baseline's median prediction time is 1.481 seconds and p95 is 4.194 seconds on its shared-workstation conditions. Other recorded isolated timing figures must not be mixed with these as a before/after improvement.

Budget changes must report recall, served accuracy, resource cost, and newly lost families. A hard latency cutoff can also hurt accuracy; deterministic work caps and explicit timeout accounting keep that tradeoff measurable.

All training, model-size changes, paid reference runs, promotion, commits, and deployments require the repository's appropriate explicit authorization. This report requests none of those actions and changes none of those boundaries.

## 10. Recommended first implementation package

Start with **measurement plus aggregate/operand binding**, followed by semantic coverage. These directly address observed easy failures and establish reusable machinery for harder scopes.

The first package should produce:

- A fresh external-fallback-off baseline and correctly labeled pool-oracle diagnostics.
- A failure ledger with coverage/constraint reasons and current candidate survival data.
- Independent fixtures for operation-like column names, coordinated aggregates, repeated aggregate functions, and distractor tables.
- Shared typed requirement evidence for that family, consumed by the current search, ranker, and verifier.
- Removed overlapping inference for the migrated family, one coherent serving path, and no model decoder.
- A fresh whole-database evaluation with counts, paired wins/losses, latency, and remaining ceiling.

Then choose the next family from measured remaining losses: false refusals if correct candidates are present; binding/construction if they are absent; join/grain or nested scope when those dominate. Do not fund training or widen every beam before that classification.

Success means the deterministic engine itself becomes more accurate while every query decision remains inspectable and reproducible. The project does not need to concede its objective to pursue that result.
