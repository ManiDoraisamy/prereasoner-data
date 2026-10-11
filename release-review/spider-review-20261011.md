# Claude fix review and matched Spider comparison — 2026-10-11

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
