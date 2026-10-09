# Spider Results

Dated entries come newest first. Each records a run at the commit it names; the scripts, options, and models an older
entry names (the 7B SQL proposer, the arbiter, `training/rank/`, `--selection arbiter`) may since have
been removed, and that commit holds the code that ran.

## A named measure that is no number is refused on every route, released as `7d2410a`: 314 strict, unchanged (2026-10-09)

Same contract as `release-0733eef` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), tag
`unreadable-f495890-dirty`: source `f495890` (`0733eef` plus this file) with the change of `DECISIONS.md` "A named
measure that is no number is refused on every route" in the worktree. The rule (`query_contract.unreadable_operand`)
rejects the planner's candidates for a total or average whose most fully named column is text with a cell that is no
number. The run predates only moving the refusal sentence to `engine/answer_presentation.py` and the reply's wording;
selection is the same code.

| | `release-0733eef` | **`unreadable-f495890-dirty`** |
|---|---:|---:|
| Strict | 314 (30.4%) | **314 (30.4%)** |
| Lenient | 380 | **380** |
| Answered | 523 | **523** |
| Strict: easy / medium / hard / extra | 128 / 139 / 29 / 18 | 128 / 139 / 29 / 18 |
| Prediction seconds, median / p90 | 1.13 / 2.37 | 1.03 / 2.31 |

- 0 wins, 0 losses; all 1,034 served SQL and all 511 refusal reasons identical. No DEV question names a text
  column with a cell that is no number, so the rule never fires: it is a correctness hardening, not an accuracy gain.

- The released `7d2410a` (tag `column-7d2410a`, clean worktree) reports only a column holding numbers and a cell that
  is no number, after the release build's dataset gate caught "total transfers signed in August" refused for the
  dates of `signed`. Same contract, same result: 314 / 380 / 523, every served SQL and refusal reason identical,
  median / p90 0.91 / 1.79 s.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_unreadable-f495890-dirty.json` and
`full_eval_column-7d2410a.json`.

## The review's F1-F5 fixed, released as `0733eef`: 314 strict (2026-10-08)

Same contract as `planted-8b8a7ee-dirty` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000),
tag `release-0733eef`; the 113 source files the run hashes are `0733eef`'s (three differ only in this checkout's CRLF
line endings), and the dirty flag is this file. The change (`DECISIONS.md`, "The review of the messy-data fixes"): a
named aggregate operand that is not numeric is refused whatever its share of bad cells (F1); compose's refusal is
`routing.route`'s answer in serving and here (F5); "alphabetical" needs a text order in the direction asked, and the
search builds it for "in alphabetical order" (F2); a spelled number is read in its role (F3); a participle is read
only when its value relates to the rows one way, or the relating column carries its travel direction (F4).

| | `planted-8b8a7ee-dirty` | **`0733eef`** |
|---|---:|---:|
| Strict | 315 (30.5%) | **314 (30.4%)** |
| Lenient | 381 | **380** |
| Answered | 524 | **523** |
| Strict of answered | 60.1% | **60.0%** |
| Strict: easy / medium / hard / extra | 127 / 140 / 30 / 18 | 128 / 139 / 29 / 18 |
| Prediction seconds, median / p90 | 1.35 / 2.96 | 1.13 / 2.37 |

- 2 wins, both F2: DEV 528 ("in reverse alphabetical order") and 585 ("in alphabetical order"), refused before.
- 3 losses, all F4, now refused: DEV 103 and 145 ("cars produced in/after 1980": one car's Weight is also 1980, so
  the value is held by two columns) and 957 ("owners living in Virginia"). A first F4 that refused every value held
  twice, and every number, lost 20; the travel reading, numbers and same-row columns recovered 17 of them.
- F1 and F3 change no Spider answer.
- The intermediate `bfa30ba` (tag `reviewfix2-30a8cb1-dirty`) scored the same 314, but compose refused DEV 945
  ("...costs less than the average? Give me the name") as an average of the text `name` column: the operand
  phrase ran across the sentence boundary. `0733eef` reads an aggregate phrase within its sentence; 945 now reaches
  the typed search, which finds no runnable query, and every served SQL is identical to `bfa30ba`'s.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_release-0733eef.json`.

## Independent review replay on `30a8cb1`: completeness gain confirmed (2026-10-08)

Fresh full DEV run through `spider/probe/full_eval.py`: all 1,034 questions, `whole_db`, `served`, SQL backend,
compose routing enabled, row cap 5,000, 12-second soft budget, external rewriting off, no resumed predictions.
Commit `30a8cb106db074fde1fd7f307eec3da872f7431d`, `worktree_dirty=true` (the existing untracked review directory);
no production code or model changes during the review. All 137 source/input fingerprints were rechecked after
completion with no changes; bundle `b11056f8ce8bf16a44da69bf76fea238149df439d7fde52d5e1f4747a5f536c7`.

| Metric | Fresh result |
|---|---:|
| Strict | **315/1,034 (30.5%)** |
| Lenient | **381/1,034 (36.8%)** |
| Answered | **524/1,034 (50.7%)** |
| Strict / lenient among answered | **60.1% / 72.7%** |
| Legacy scalar-gold metric | **185/408 (45.3%)** |
| Strict: easy / medium / hard / extra | **127 / 140 / 30 / 18** |
| Prediction seconds: median / p90 / p95 / max | **1.408 / 3.138 / 3.955 / 7.852** |

Against `review2-5f8c57c-dirty` / the inspected October 7 frozen engine baseline: 69 strict wins, 1 loss
(DEV 990), 246 unchanged correct, 718 unchanged incorrect; net +68 strict answers (+6.6 percentage points).
There are 120 newly answered questions (68 strict-correct) and 10 newly refused (one previously strict-correct).
Every SQL and grade matches `accuracy3-97ac4ff-dirty`. Three raw averages differ by approximately 10^-14,
without a grade change. Input/source hashes match `planted-8b8a7ee-dirty` except for the source-commit marker.
No gold execution errors or over-budget predictions. All 1,034 route to AST.

Of the 510 refusals, 379 have a recorded executable-and-grounded eligible member, 122 have no eligible member,
and 9 have no pool. This is pre-completeness eligibility, not proof the candidate fulfills the request. 209
served answers fail strict scoring. A fresh pool-oracle run was not performed. The desktop ran focused tests
and review probes during parts of the run; these latency figures do not isolate a code performance change.

The review passed eight focused suites (557 registered checks) and Python compilation, but reproduced remaining
semantic defects: mostly malformed requested amounts can still be replaced with world population; alphabetical
order, spelled ranking cutoffs, and participle relationships have incomplete acceptance checks. Live world
validation could not reach the configured PostgreSQL host. The existing evaluator also omits serving's compose
clarification gate, as its module documentation states: this is the established served-AST benchmark contract,
not a deployed HTTP/world-data correctness measurement. Detailed findings are in
`release-review/claude-fixes-review.md`; production sources and weights were kept unchanged for this replay.

Outputs: `%LOCALAPPDATA%/Temp/prereasoner-spider-review-30a8cb1-20261008/full_eval_review-30a8cb1.json`,
`full_eval_per_example_review-30a8cb1.json`, and `review-analysis.json` (paired ledger and rechecked provenance).

## Messy-data fixes, on `8b8a7ee`: same answers (2026-10-08)

Same contract as `accuracy3-97ac4ff-dirty` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000),
on `8b8a7ee` with the change applied (`worktree_dirty=true`, tag `planted-8b8a7ee-dirty`). The change (`DECISIONS.md`,
"Messy data changes no answer silently"): compose claims the rows a question measures or counts before grouping,
refuses to total a named column whose cells are not all numbers, and discloses rows whose place matched nothing;
the planner names such cells when it refuses. All 1,034 examples have the same SQL and grade as
`accuracy3-97ac4ff-dirty`: 315 strict, 381 lenient, 524 answered, no wins and no losses. These fixes are correctness
hardening, not an accuracy gain.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_planted-8b8a7ee-dirty.json`.

## The completeness check reads the words of the tables the query reads, on `97ac4ff` (2026-10-08)

The change (`DECISIONS.md`, "A question word is read from the tables the query reads"), from the SQL-accuracy review
of 2026-10-08 (W04): `engine/query_contract.py:read_question` counted a word as read when any
column of any table carried it, and read names only as written ("LifeExpectancy" was one word). It now reads:
- the words of the tables the query reads and of the columns it uses, split as the search splits names, plus the
  other columns of those tables as written; a name spelled in two words ("high schoolers");
- a spelled number the query keeps, order words in a query that orders, and a participle whose own noun phrase
  names a compared value ("departing from APG");
- a counted noun across several tables only when it is a field's name.

Two runs, same contract as `review2-5f8c57c-dirty` (`whole_db`, `served` selection, SQL backend, row cap 5,000),
from `97ac4ff` with the change applied (`worktree_dirty=true`; the 113 source files each run hashes match the tree
described here, bundle `b11056f8ce8bf16a…`). The baseline for the engine alone is `profile-removal-97ac4ff-dirty`, the
same tree without this change (identical to `review2-5f8c57c-dirty` in every example's SQL and grade). The baseline
with Gemini on is `rewrite-gemini-3.8-flash-e55ab00`, run through the same wrapper (the same production rewrite path).

| | Engine alone before | **Engine alone** | Gemini on before | **Gemini on** |
|---|---:|---:|---:|---:|
| Strict | 247 (23.9%) | **315 (30.5%)** | 341 (33.0%) | **386 (37.3%)** |
| Lenient | 315 | **381** | 446 | **482** |
| Answered | 414 | **524** | 604 | **671** |
| Strict of answered | 59.7% | **60.1%** | 56.5% | **57.5%** |
| Scalar | 149/408 | **185/408** | 209/408 | **233/408** |
| Strict: easy / medium / hard / extra | 105 / 103 / 24 / 15 | 127 / 140 / 30 / 18 | 141 / 143 / 31 / 26 | 155 / 167 / 37 / 27 |
| Strict wins / losses | | **69 / 1** | | **52 / 7** |
| Rewording calls (failed), cost | | | 620 (3), $0.31 | 510 (3), $0.27 |
| Prediction seconds, median / p90 | 1.42 / 3.23 | 1.35 / 2.96 | 3.55 / 6.75 | 2.78 / 5.68 |

- Engine alone: 120 questions are newly answered, 68 of them right; 10 are newly refused, one of them right (DEV 990,
  "how much does each charge type costs": "costs" named only another table's column).
- Gemini on: one loss is the change's (DEV 199: the engine now serves its own wrong self-join for "the airport name
  for airport 'AKO'" instead of rewording it). The other six are the rewording's run-to-run variance: one failed
  call and five different rewordings. The engine answers 110 more questions itself, so Gemini is called less.
- A loss ledger of every question (evaluation only, in the session's scratchpad) located the gap: before the change
  a strict-correct eligible candidate existed for 550 of 1,034, the completeness gate refused 489 questions with an
  eligible candidate, and 194 of those refused candidates were right. Each reading rule was simulated on it before
  being written; a rule reading graded adjectives ("youngest", "greatest") let through 27 wrong answers for 19 right
  ones and was left out.
- Not changed: 15 served answers differ from gold only in column order, because a grouped answer lists its groups
  first (`sql_search`, a product choice); the aggregate cue that is also a column name ("average" and a column
  Average, DEV 4, 5, 17) was tried and left out: it needs projection and grouping changes for at most 3 questions.
- Prediction seconds were measured with an unrelated test harness running on the same desktop for both new runs.

Outputs: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_accuracy3-97ac4ff-dirty.json` and
`full_eval_accuracy3-gemini-97ac4ff-dirty.json`.

## The selection's rewording answered by three hosted models, on `e55ab00` (2026-10-07)

Production runs with the operator's Gemini switch on: a question the typed search cannot answer is reworded once
(`engine/question_rewrite.py`) and searched again. This measures that served path with three models answering the
rewording. Same contract as `review2-5f8c57c-dirty` otherwise (`whole_db`, `served` selection, SQL backend, row cap
5,000), on `e55ab00` (tags `rewrite-<model>-e55ab00`; the three ran in parallel on one desktop). Only the model
behind `engine.llm.generate_text` changed, through a scratch wrapper around `full_eval.py`. The prompt
(`sql_prompt.REWRITE_SYSTEM`, `rewrite_prompt`), the check that a rewording keeps every stated value, and the
second search are production code. Each model ran at its least thinking: Gemini at LOW (production); Haiku 5.5
with thinking off (by default it thinks about 300 tokens per rewording); Sonnet 5.5 at its default, which did not
think. The rewording fires on the 620 questions the search alone leaves unanswered.

| | Engine alone | **gemini-3.8-flash** | Sonnet 5.5 | Haiku 5.5 |
|---|---:|---:|---:|---:|
| Strict | 247 | **341 (+94)** | 336 (+89) | 289 (+42) |
| Lenient | 315 | **446** | 432 | 371 |
| Answered | 414 | **604** | 575 | 493 |
| Strict wins / losses against the engine alone | | 94 / 0 | 89 / 0 | 42 / 0 |
| Rewording calls (failed) | | 620 (3) | 620 (0) | 620 (0) |
| Rewording seconds, median / p90 / max | | 1.96 / 2.86 / 17.5 | 1.50 / 2.04 / 7.4 | 0.65 / 0.90 / 3.2 |
| Tokens in / out, thinking included | | 320,372 / 19,112 | 475,049 / 38,563 | 474,429 / 20,803 |
| Cost of the 620 rewordings | | $0.31 | $1.34 | $0.06 |

- Prices per million input/output tokens, list prices read 2026-10-07: gemini-3.8-flash $0.75 / $3.75 (thinking
  billed as output), Sonnet 5.5 $2 / $10, Haiku 5.5 $0.10 / $0.50 (prompts up to 100K tokens).
- Gemini answers the most questions correctly, at a quarter of Sonnet's cost; Haiku without thinking rescues fewer
  than half as many. Haiku at its default thinking was not measured.
- Gemini used 64 thinking tokens across all 620 calls at LOW here, against about 565 per call on the keyword
  prompt measured 2026-10-05 (6 to 14 s then, 1.96 s median here).
- The earlier Gemini-on run at `13edb6a` reached 338 strict / 609 answered on an older planner.
- The runs' prediction seconds were measured three to a desktop and are not comparable with the runs below.

Outputs: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_rewrite-<model>-e55ab00.json`. The
per-call logs stayed in the session's scratchpad.

## Hosted models writing the SQL themselves: about 3.4 times the engine's strict count (2026-10-07)

A measurement beside the engine, never served: no model writes SQL in the product (CLAUDE.md). It shows what that
rule costs on Spider. Each model answered the same 1,034 DEV questions, on the same capped databases
(`evalutil.load_capped`, row cap 5,000, `build_mem_db`), with gold run the same way and graded by the same
`spider_eval.compare` and `record_integrated_result` as `review2-5f8c57c-dirty`. One zero-shot request per
question gave every table of the question's database (`whole_db`) as `CREATE TABLE` statements with 3 example rows
each, plus Spider's declared foreign keys, which `full_eval.py` also gives the engine. The instruction was "Write
one SQLite query that answers the question… Reply with the SQL query only". The Claude 5.5 models refuse a
temperature and ran at their defaults, without thinking; Gemini ran at temperature 0 and LOW thinking (about 25
thinking tokens a question). Calls went 8 at a time from a desktop, and latency is each call's round trip. The
engine's seconds are the half of its run that had the desktop alone.

| | Engine | Haiku 5.5 | Sonnet 5.5 | **gemini-3.8-flash** |
|---|---:|---:|---:|---:|
| Strict | 247 (23.9%) | 828 (80.1%) | 840 (81.2%) | **872 (84.3%)** |
| Lenient | 315 | 871 | 897 | **904** |
| Answered | 414 | 1,029 | 1,023 | **1,034** |
| Scalar | 149/408 | 373/408 | 373/408 | **390/408** |
| Strict: easy / medium / hard / extra | 105 / 103 / 24 / 15 | 236 / 346 / 143 / 103 | 223 / 355 / 151 / 111 | 239 / 364 / 149 / 120 |
| Seconds, median / p90 / max | 1.04 / 2.52 / 6.9 | **0.71 / 1.63 / 5.8** | 1.22 / 1.85 / 8.5 | 1.49 / 2.25 / 34.5 |
| Cost of the run | no per-question fee | **$0.18** | $3.06 | $0.83 |

- Strict-correct for the engine and wrong for the model: 13 (Haiku), 10 (Sonnet), 7 (Gemini). The other way: 594,
  603 and 632.
- On the 414 questions the engine answers, the engine is strict-correct on 247; Haiku on 338, Sonnet on 349 and
  Gemini on 356.
- Spider has been public since 2018 and is very likely in all three models' training data, so their scores are
  probably inflated. The engine's search rules were also built against Spider DEV failures.
- Consistent with the removed 7B proposer's 866 strict (2026-10-01).

The harness and its per-question records stayed in the session's scratchpad; neither is committed, since the tree
keeps one evaluator.

## Revision-2 review fixes, on `5f8c57c`: same answers (2026-10-07)

Same contract as `review-106aefc-dirty` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), on
`5f8c57c` with the change applied (`worktree_dirty=true`, tag `review2-5f8c57c-dirty`). The change (`DECISIONS.md`,
"Release review, revision 2"):
- join keys are read a column at a time, as the schema stores the column (`relations.join_keys`);
- a number in a question is one word, "1,000" and "2.5" included (`numeric.NUMBER_WORD`);
- the polarity check reads a column through LOWER, on either side and in IN lists, and refuses a query that leaves
  an excluded value in;
- both backends divide through one rounding (`numeric.decimal_divide`), AVG included;
- an explicit zero foreign-key confidence stays zero.

From this run on, a result's `artifacts` also hash each database the run reads (`dbs/<db_id>.sqlite`, 20 here) and
the whole validated model bundle (`weight_bundle`, `b11056f8ce8bf16a…`). All 116 source files the run hashes match
the tree described here.

All 1,034 examples have the same SQL and grade as `review-106aefc-dirty`: 247 strict, 315 lenient, 414 answered, with
no wins and no losses. No Spider DEV question holds a decimal or grouped number, so the question-number fix cannot
show here. These fixes are correctness hardening, not an accuracy gain. Prediction seconds: median 1.08 → 1.39, p90
2.39 → 2.94, max 5.58 → 6.88. The first ~500 examples shared the desktop with the hermetic suites and ran 1.4–1.7×
slower; over examples 500–1,033 the median is 1.04 against 1.07.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_review2-5f8c57c-dirty.json`.

## A result records every source file it ran (2026-10-07)

From this change on, a result's `artifacts` hash every Python file under `engine/` and `spider/probe/`, keyed by
its repository path (`spider/probe/full_eval.py` replaces the `eval_harness` key). Results and checkpoints written
before it hash only a hand-kept list of 38 engine files and the harness itself. That list left out, among others,
`engine/relations.py`, `engine/numeric.py`, `engine/sql_dates.py` and the harness's `evalutil.py`, so an edit to one
of them changed neither the record nor the `--resume` check. No answers change.

## Release-review fixes, on `106aefc`: same answers (2026-10-07)

Same contract as `labels-bc216ff-dirty` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), on
`106aefc` with the change applied (`worktree_dirty=true`, tag `review-106aefc-dirty`). The baseline's code is the
engine of `576e1ad`, and `106aefc` changes only the chat. The change (`DECISIONS.md`, "A release review"):
- a negation excludes the first value after it and the values joined to it, and the completeness check refuses a
  query that excludes a kept value or keeps an excluded one;
- a comma is part of a number only where it groups digits (`numeric.GROUPED_DIGITS`), and `sql_schema` and
  `sql_search` share `numeric.NUMBER_TEXT` instead of keeping their own copies;
- foreign-key discovery and join grounding read a key with `relations.join_value`: text exactly, numbers by
  exact magnitude.

The nearness gate and the browser importer are not on the evaluator's path. The evaluator passes Spider's declared
foreign keys, so the discovery change can reach these results only through grounding. The evaluator's fingerprint
list leaves out `engine/relations.py` and `engine/numeric.py`; for this run their SHA-256 values begin
`95261bbffe134d32` and `a3f62b10188e2999`.

All 1,034 examples have the same SQL and grade as `labels-bc216ff-dirty`: 247 strict, 315 lenient, 414 answered, with
no wins and no losses. These fixes are correctness hardening, not an accuracy gain. Prediction seconds: median
1.18 → 1.08, p90 2.45 → 2.39, max 5.96 → 5.58. The baseline shared this desktop with the hermetic suites; this run
had it alone except for a brief browser check.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_review-106aefc-dirty.json`.

## Listings of numbers name their rows, on `bc216ff`: same answers (2026-10-06)

Same contract as `substrings-c0b1f31-dirty` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap
5,000), on a worktree of `bc216ff` with the change applied (`worktree_dirty=true`, tag `labels-bc216ff-dirty`; the
JSON records each engine file's hash). The change:
- a listing of numbers alone shows first the text column its filter keeps several values of
  (`sql_search._rows_named`, applied after the ranking);
- the constraint expansion's "X or Y" readings read aggregate words through `sql_expansion.asked_cues`, so "Avg." in
  a column name no longer averages them.

All 1,034 examples have the same SQL and grade as `substrings-c0b1f31-dirty`: 247 strict, 315 lenient, 414
answered. Spider DEV's gold holds 21 listings of numbers alone under a
text filter, and every one compares a single value, which the rule leaves alone. Prediction seconds: median 1.08 →
1.18, p90 2.30 → 2.45, max 5.41 → 5.96; the hermetic suites ran on this desktop during the run.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_labels-bc216ff-dirty.json`.

## Texts values hold, and aggregate operands, on `c0b1f31`: 4 strict wins, no loss (2026-10-06)

Same contract as `d9ea249` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), on a worktree
of `c0b1f31` with the change applied (`worktree_dirty=true`, tag `substrings-c0b1f31-dirty`; the JSON records each
engine file's hash, and `engine/sql_search.py` has changed since in one docstring only). `c0b1f31` changes no planner
code after `d9ea249`. The change:
- reads "contain", "include", "substring" and a whole value after "all" as a text values hold, requires it compared
  with LIKE in the column the question names, and reads the words that asked once it is;
- takes a text column as an aggregate's operand only where its name ends the aggregate's phrase;
- asks no average from a spelled column name.

Strict 243 → 247: 4 wins (301, 302, 531, 701, all substring questions unanswered before because "contain",
"letter" or "substring" was left unread) and no loss. Lenient 310 → 315: those four, and 970 with its columns in
another order. Answered 408 → 414.

Six examples changed SQL, all unanswered before. The sixth, 944 ("the professionals who have done treatment with cost
below average"), is answered and wrong: the right professionals with each treated dog's name added. Before, the
average's operand included "first name", a text column, so every reading was refused. Substring questions 506, 507,
971, 972 and 973 stay unanswered; 362 and 380 keep their whole-value readings.

Prediction seconds: median 1.16 → 1.08, p90 2.53 → 2.30, max 5.76 → 5.41. The hermetic suites ran on this desktop
during part of the run.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_substrings-c0b1f31-dirty.json`.

## Questions run in parallel, at `d9ea249`: same answers (2026-10-06)

Same contract as `1b5c6ae` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), clean
checkout of `d9ea249` (`worktree_dirty=false`, tag `parallel-d9ea249`). `289dbe8` takes out the engine's
process-wide question lock: what belongs to one request is request-local, and the shared tokenizer and encode cache
take short locks. `d9ea249` sets the chat's Gemini thinking, which this path does not call, and `04d339e` changes a
test only. All 1,034 examples have the same SQL and grade as `1b5c6ae`: 243 strict, 310 lenient, 408 answered.
Prediction seconds: median 0.90 → 1.16, p90 1.85 → 2.53, max 4.13 → 5.76; this run shared the desktop with the
repository's live suites.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_parallel-d9ea249.json`.

## A duration compares how long rows lasted, at `1b5c6ae`: same answers (2026-10-05)

Same contract as `c9bf407` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), clean
checkout of `1b5c6ae` (`worktree_dirty=false`, tag `durations-1b5c6ae`). The commit reads "more than 6 months" and
its kin as a comparison of how long each row lasted (`DateSpan`). No dev question holds a duration phrase, and all
1,034 examples have the same SQL and grade as `c9bf407`: 243 strict, 310 lenient, 408 answered. Prediction seconds:
median 0.92 → 0.90, p90 1.89 → 1.85, max 4.33 → 4.13.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_durations-1b5c6ae.json`.

## A pooled reading's step budget follows the tables it reads, at `c9bf407`: same answers (2026-10-05)

Same contract as `090de16` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), clean
checkout of `c9bf407` (`worktree_dirty=false`, tag `pool-c9bf407`). The commit gives a pooled reading 20 SQLite
steps for each cell of the tables it reads (at least 10M, at most 100M) and runs both of `select_query`'s pools on
one copy of the tables. All 1,034 examples have the same SQL and grade as `090de16`: 243 strict, 310 lenient,
408 answered, no strict win or loss. Every pooled reading of the run had run under the old budget too: the
heaviest of the 14,079 recorded before the change took 1.1M steps, under the 10M floor.

Prediction seconds: median 1.15 → 0.92, p90 2.47 → 1.89, max 6.07 → 4.33; the `090de16` run shared this desktop
with the repository suites.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_pool-c9bf407.json`.

## A question the upload reads whole builds no compose plan, at `090de16`: same answers (2026-10-05)

Same contract as `7b05b8b` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), clean
checkout of `090de16` (`worktree_dirty=false`, tag `routing-090de16`). It covers every commit since `7b05b8b`:
`f8a8821` (a word the search cannot read is asked about, and a quantity word reads as a numeric column; it shipped
without its own run), the upload-once commits (no planner change), and `090de16`, which asks
`routing.reads_upload_whole` before building a compose plan, in serving and in this evaluator. All 1,034 examples
have the same SQL and grade as `7b05b8b`: 243 strict, 310 lenient, 408 answered, no strict win or loss. Compose
owns no Spider question either way (`routed: {'ast': 1034}`).

Prediction seconds: median 0.92 → 1.15, p90 1.73 → 2.47, p95 2.06 → 2.96, max 3.61 → 6.07. Two causes. Encoding,
the same work in both runs, took 16% longer: the repository suites ran on this desktop at the same time. And the
time outside the timed spans grew from 168 s to 352 s across the run, because the evaluator now runs the search
probe for each question the depth gate fires on. Serving already ran that probe for the compound-question check,
so the probe costs evaluator time only.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_routing-090de16.json`.

## A total over rows its joins repeat gives way, at `7b05b8b`: same totals (2026-10-05)

Same contract as `bc85ccd` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), clean
checkout of `7b05b8b` (`worktree_dirty=false`, tag `doublecount-7b05b8b`). The commit serves a member whose SUM
or AVG reads only rows its joins repeat (`sql_grounding.double_counted`) only when every eligible member does.
243 strict, 310 lenient, 408 answered, as at `70022e3`: no strict win or loss, and 1,031 of 1,034 examples
have the same SQL. Five served members double counted before; three changed and two stay, each the only
eligible member (#4, #424).

| # | Difficulty | Before | After | Change |
|---|---|---|---|---|
| 357 | easy | lenient | answered | "How many paragraphs in total?": the two best-ranked readings count the paragraphs over Templates, Documents and Paragraphs and sum each template's Version_Number once per paragraph; the served one counts templates. All three eligible readings are wrong (gold counts Paragraphs), and the completeness check accepts one that never reads Paragraphs. |
| 794 | extra | lenient | lenient | Averages the cities' population the question names, not each country's once per city. |
| 795 | extra | answered | lenient | The same. |

Prediction seconds (the evaluator's summary): median 1.090 → 0.923, p90 2.217 → 1.730, p95 2.583 → 2.061, max
4.755 → 3.615. The commit adds a scan of each pool's join columns and removes no work, so the difference is this
desktop's load between runs.

Spider train gold, read by the same importer: 49 of 6,953 queries SUM or AVG rows their joins repeat (dev: 0
of 1,026), mostly on purpose (credits over the classes that offer a course, latitudes averaged over trips), so
the rule is a preference, not an eligibility rule.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_doublecount-7b05b8b.json`.

## A counted noun whose column no key reaches, at `70022e3`: same answers (2026-10-05)

Same contract as `bc85ccd` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), clean
checkout of `70022e3` (`worktree_dirty=false`, tag `counted-70022e3`). The commit stops grouping a projected
column that no key joins to the group's table (unless the question names it after "by"), anchors the search on
what the question asks of the rows, and lets the completeness check read a counted noun as the rows counted
when only unreachable tables hold the field it names. All 1,034 examples have the same SQL, rows and grade as
`bc85ccd`: 243 strict, 310 lenient, 408 answered. Prediction seconds: median 1.09, p90 2.22, p95 2.58, max 4.76.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_counted-70022e3.json`.

## Tabs of one layout and "the total Amount", at `ed8d280`: same answers (2026-10-05)

Same contract as `bc85ccd` (Gemini off, `whole_db`, `served` selection, SQL backend, row cap 5,000), clean
checkout of `ed8d280` (`worktree_dirty=false`, tag `layout-ed8d280`). The commit searches tables that are
copies of one layout as one, keeps a measure named after an aggregate word ("the total Amount") as a
mention, accepts SUM(Amount) where the question spells a Total Amount field, and derives a request's tables
once. All 1,034 examples have the same SQL, rows and grade as `bc85ccd`: 243 strict, 310 lenient, 408
answered. No Spider DEV database has three-column tables of one layout. Prediction seconds: median 1.24 →
1.24, p90 2.63 → 2.34, p95 3.07 → 2.79, max 4.85 → 4.68.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_layout-ed8d280.json`.

## One schema graph per question, at `bc85ccd`: same answers, shorter tail (2026-10-04)

Same contract as `4aa6ca6` (Gemini off), clean checkout of `bc85ccd` (`worktree_dirty=false`, tag
`perf-bc85ccd`). The commit builds the schema graph once per question and caches the cell words per
graph. All 1,034 examples have the same SQL, rows and grade as `4aa6ca6`: 243 strict, 310 lenient, 408
answered. Prediction seconds: median 1.29 → 1.24, p90 3.10 → 2.63, p95 3.65 → 3.07, max 14.24 → 4.85.

## The served path with Gemini on, at `13edb6a` (2026-10-04)

Production runs with the operator's Gemini switch on: a question the search cannot fully read is
reworded once (`engine/question_rewrite.py`, `gemini-3.8-flash`) and searched again. Same contract as
the runs below otherwise (`whole_db`, `served` selection, SQL backend, row cap 5,000), from a clean
checkout of `13edb6a` (`worktree_dirty=false`, tag `gemini-13edb6a`). The planner is the one measured at
`4aa6ca6`.

| Run | Strict | Answered | Strict of answered | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| `4aa6ca6`, Gemini off | 243 | 408 | 60% | 310 | 149/408 |
| **`13edb6a`, Gemini on (production)** | **338 (32.7%)** | **609** | 55% | 442 | 209/408 |
| unread-word experiment, Gemini on | 444 (42.9%) | 911 | 49% | 581 | 272/408 |

- Gemini's rewording served 201 answers. Against Gemini off: 95 strict wins, no losses (lenient 132 / 0).
- Against `60a55a3` (no completeness check, Gemini off, 497): 45 wins, 204 losses.
- Prediction seconds: median 4.37, p90 8.54, max 55.0.
- The experiment (dirty worktree, tag `gemini-unread-advisory-13edb6a`, the change recorded below) wins
  134 and loses 28 against production. It stays unpromoted for the world-routing and qualifier
  failures recorded below.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_gemini-13edb6a.json`.

## Experiment: unread words rank but refuse nothing, on `6a126c4` (2026-10-04)

Same contract, from `6a126c4` with one uncommitted change to `engine/tables.py:select_query`
(`worktree_dirty=true`, tag `unread-advisory-6a126c4`):
- an unread question word no longer cancels the selected reading or calls Gemini;
- a reading that only repeats its filter's values (`SELECT Keyword ... WHERE Keyword = 'x'`) is refused,
  so "keyword volume for x" still goes to Gemini;
- `constraint_violations` stay hard vetoes.

| Run | Strict | Answered | Strict of answered | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| `4aa6ca6` (shipping) | 243 | 408 | 60% | 310 | 149/408 |
| this experiment | **435 (42.1%)** | **894** | 49% | 568 | 268/408 |
| advisory `6530bbc` (vetoes soft too) | 476 | 1,025 | 46% | 614 | 286/408 |
| `60a55a3` (no check) | 497 | 1,025 | 48% | 603 | 286/408 |

Against `4aa6ca6`: 192 strict wins, no losses. Against `60a55a3`: 19 wins, 81 losses. Against the fully
advisory run: 2 wins, 43 losses; that gap is what the hard vetoes cost. Prediction seconds: median 1.21,
p90 3.10, max 14.72.

None of these runs measures the served path with Gemini on. There, a refusal is reworded once and
searched again; under this experiment an unread word would instead be served unread. Not promoted:
the hermetic planner suite fails 10 tests under it, among them a world question
("total amount for restaurants in United States" on a sheet without a country) answered with every
restaurant's total, and "total Amount for premium customers" answered unfiltered. Output:
`%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_unread-advisory-6a126c4.json`.

## The completeness check reads names, compared values and grain, at `4aa6ca6` (2026-10-04)

Same contract as the deployed `6530bbc` run below (`whole_db`, `served` selection, SQL backend, row
cap 5,000, Gemini fallback off), from a clean checkout of `4aa6ca6` (`worktree_dirty=false`, tag
`contract-4aa6ca6`). The check no longer:
- loses a total to a field name that spells an aggregate word ("the total Avg. monthly searches");
- treats a compared value ("No", "Not Started") as an exclusion;
- reads "sorted by" as a requested grain.

| Difficulty | n | Answered | Strict | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 133 | 105 | 114 | 90/173 |
| medium | 446 | 168 | 99 | 124 | 27/101 |
| hard | 174 | 61 | 24 | 42 | 20/77 |
| extra | 166 | 46 | 15 | 30 | 12/57 |
| **all** | **1,034** | **408** | **243 (23.5%)** | **310 (30.0%)** | **149/408 (36.5%)** |

| Against | strict win | strict loss | both correct | both wrong | lenient win / loss |
|---|---:|---:|---:|---:|---:|
| `6530bbc` (deployed, 241) | 2 | 0 | 241 | 791 | 4 / 0 |
| `60a55a3` (497) | 10 | 264 | 233 | 527 | 16 / 309 |

Answered rose from 402 to 408. Prediction seconds: median 1.29, p90 3.10, p95 3.65, max 14.24.

The first version, `d5268c5`, took the grain from the search's group window. It scored 238 strict, with
1 win and 4 losses against `6530bbc`. All four losses were "each X" questions: the window drops "id"
from names, so both tables' `Stadium_ID` were required. `4aa6ca6` keeps the exact-label reading
instead. Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_contract-4aa6ca6.json`.

## Experiment: the completeness checks as preferences, on `6530bbc` (2026-10-04)

Same contract and data as the deployed run below, from a scratch worktree of `6530bbc` with one uncommitted
change to `engine/tables.py` (`worktree_dirty=true`, tag `advisory-6530bbc`):
- `constraint_violations` and `coverage` choose among runnable candidates instead of removing them;
- an unread question word no longer cancels the selected query;
- Gemini's rewording would run only when nothing runs.

| | Strict | Answered | Lenient | Scalar |
|---|---:|---:|---:|---:|
| `60a55a3` (previous release) | 497 | 1,025 | 603 | 286/408 |
| `6530bbc` (deployed) | 241 | 402 | 306 | 148/408 |
| `6530bbc` + preferences | **476 (46.0%)** | 1,025 | 614 | 286/408 |

Against the deployed run: 235 strict wins, no losses (lenient: 308 wins, no losses). Against `60a55a3`:
20 strict wins, 41 losses, 456 both correct, 517 both wrong (lenient 36 wins, 25 losses). Prediction seconds:
median 1.24, p90 3.09, max 10.88. The change is not in production code; promoting it needs the owner's
decision. Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_advisory-6530bbc.json`.

## The deployed engine at `6530bbc`, fresh whole_db DEV run (2026-10-04)

The serving-faithful contract of the `60a55a3` run below (`spider/probe/full_eval.py --config whole_db`,
`served` selection, SQL backend, row cap 5,000, the Gemini fallback off), from a clean checkout of
`6530bbc` (`worktree_dirty=false`, tag `deployed-6530bbc`): the commit production serves since
2026-10-04 03:11 UTC. Selection there includes the completeness check (`engine/query_contract.py`), which
refuses a runnable query when a question word is unread.

| Difficulty | n | Answered | Strict | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 133 | 105 | 114 | 90/173 |
| medium | 446 | 165 | 97 | 122 | 27/101 |
| hard | 174 | 60 | 24 | 41 | 19/77 |
| extra | 166 | 44 | 15 | 29 | 12/57 |
| **all** | **1,034** | **402** | **241 (23.3%)** | **306 (29.6%)** | **148/408 (36.3%)** |

Against `60a55a3` (497):

| | strict | lenient |
|---|---:|---:|
| win | 10 | 15 |
| loss | 266 | 312 |
| both correct | 231 | 291 |
| both wrong | 527 | 416 |

Of the 266 strict losses, 244 are refusals (no query served) and 22 are different answers. Among answered
questions, 241 of 402 are strict-correct (60%), against 497 of 1,025 (48%) at `60a55a3`. Prediction seconds:
median 1.33, p90 3.06, p95 3.79, max 15.28. Output:
`%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_deployed-6530bbc.json`.

## Release review at `4c29cd5`, whole_db DEV (2026-10-03)

Clean source `4c29cd537e1bd5e820d795c2fbb0ce6ce4f04dbe`, served selection,
`--backend auto`, row cap 5,000, external rewriting disabled, 1,034 questions:
**243 strict (23.5%), 308 lenient (29.8%), 404 answered, 630 abstained**.
Among answered questions, **161 fail strict comparison and 96 fail lenient comparison**.
Scalar gold is 148/408. This is a material accuracy limitation; the system is not at
general LLM SQL accuracy, and completeness checks do not certify semantic correctness.

Artifacts are `full_eval_launch4c29cd5.json` and
`full_eval_per_example_launch4c29cd5.json` in the operator's temporary
`prereasoner-spider-4c29cd5` directory. They pin source/model/harness hashes and
record the clean checkout and local Python 3.11 CPU execution environment.
The separate rewrite-assisted run is still pending and must not be merged into this score.
Subsequent sidebar metadata and own-data response-proof propagation changes do not
change this evaluator's typed planner, ranker, model bundle or lowering artifacts.
The measurements remain labelled with the commit actually evaluated.

## Release review at `5856348`, whole_db DEV (2026-10-03)

Clean source `58563489357173a0f84556680d705cba2e2a83df`, served selection,
`--backend auto`, row cap 5,000, external rewriting disabled, 1,034 questions:
**214 strict (20.7%), 274 lenient (26.5%), 363 answered, 671 abstained**.
Among answered questions, 149 fail strict comparison and 89 fail lenient comparison.
These are denotation metrics, not guarantees that an answer is correct.

This run includes the mandatory completeness/constraint gate across every selection
configuration; older runs can accept more incomplete readings. It also uses the
automatic Python/SQL execution policy rather than the older SQL-only policy.
The local Python 3.11 CPU environment is not the pinned release image environment.
Artifacts are `full_eval_launch5856348.json` and
`full_eval_per_example_launch5856348.json` under the operator's temporary
`prereasoner-spider-5856348` evaluation directory, with source and model hashes.

The review reproduced valid distinct queries rejected as unread and found explicit
ordering needed direction/field proof. The subsequent candidate addresses these
through AST evidence, and requires its own fresh evaluation. The earlier 497 result
below is historical; it must not be presented as current release accuracy.

## The engine alone at `60a55a3`, fresh whole_db DEV run (2026-10-02)

The same serving-faithful contract as the runs below (`spider/probe/full_eval.py --config whole_db`,
`served` selection, SQL backend, row cap 5,000, the Gemini fallback off), from a clean checkout of
`60a55a3` (`worktree_dirty=false`, tag `main-60a55a3`), the commit the customer-fix release serves. It adds
the commits from `0624a24` to `60a55a3`; the ones that change own-data selection are the plural rule
(`1c5eac6`), the typed-search fixes `a9d6e1e`, `e736771`, `2574519`, `e321bab`, `82ad6b8`, `89223b2` and
`60a55a3`, and the coverage gate's reading of a measure named in other words (`7604226`).

| Difficulty | n | Answered | Strict | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 248 | 161 | 179 | 130/173 |
| medium | 446 | 440 | 214 | 254 | 72/101 |
| hard | 174 | 172 | 70 | 107 | 55/77 |
| extra | 166 | 165 | 52 | 63 | 29/57 |
| **all** | **1,034** | **1,025** | **497 (48.1%)** | **603 (58.3%)** | **286/408 (70.1%)** |

Against `0624a24` (458):

| | strict | lenient |
|---|---:|---:|
| win | 40 | 36 |
| loss | 1 | 2 |
| both correct | 457 | 567 |
| both wrong | 536 | 429 |

The strict wins by difficulty: easy 10, medium 20, hard 6, extra 4. The one strict loss (idx 461, `wta_1`,
"Find the year that has the most number of matches.") now ranks years by the `match_num` column instead of
counting matches per year. Nine questions have no candidate that runs and grounds (nine on `0624a24`). No
pool-oracle run was made at this commit; the last one (`4679bed`) is below. Prediction seconds: median 1.23,
p90 2.63, p95 3.03, max 6.89. Output:
`%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_main-60a55a3.json`.

## The engine alone at `0624a24`, fresh whole_db DEV run (2026-10-02)

The same serving-faithful contract as the runs below (`spider/probe/full_eval.py --config whole_db`,
`served` selection, SQL backend, row cap 5,000, the Gemini fallback off), now from a clean checkout of
`0624a24` (`worktree_dirty=false`, tag `main-0624a24`). It adds that evening's typed-search fixes on top of
`d10ca77` and the removal: `73c189f` to `7743e60` and `ab5093a` to `72bf61e` (`DECISIONS.md`).

| Difficulty | n | Answered | Strict | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 248 | 151 | 169 | 126/173 |
| medium | 446 | 440 | 194 | 242 | 69/101 |
| hard | 174 | 172 | 65 | 100 | 54/77 |
| extra | 166 | 165 | 48 | 58 | 24/57 |
| **all** | **1,034** | **1,025** | **458 (44.3%)** | **569 (55.0%)** | **273/408 (66.9%)** |

Against `d10ca77` plus the removal (380): **79 strict wins, 1 loss**. Nine questions have no candidate that
runs and grounds (32 on `4679bed`). Prediction seconds: median 1.09, p90 2.39, p95 2.96, max 5.33. Output:
`%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_main-0624a24.json`.

## No SQL-writing model: the engine alone, fresh whole_db DEV runs (2026-10-02)

The 7B SQL proposer and the fitted arbiter are removed (`DECISIONS.md`, 2026-10-02). Selection serves the
deterministic search's best-ranked candidate that executes and is grounded. The labelled Gemini fallback
was off in every run below (`fallback.enabled: false` in each summary), so no model wrote SQL. Each run is
serving-faithful (`spider/probe/full_eval.py --config whole_db`, `served` selection, SQL backend, row cap
5,000) on a shared workstation CPU, Python 3.14, with the change uncommitted
(`worktree_dirty=true`; every recorded source file is the change applied to the named base).

| Run | Base | Strict | Lenient | Scalar-gold | Answered |
|---|---|---:|---:|---:|---:|
| `no-sql-model-4679bed`: the removal alone | `4679bed` | **354/1,034 (34.2%)** | 470/1,034 (45.5%) | 230/408 (56.4%) | 1,002 |
| `no-sql-model-d10ca77`: plus that day's search fixes (`8af41e9`, `0d925e2`, `f4dfa37`, `d10ca77`) | `d10ca77` | **380/1,034 (36.8%)** | 515/1,034 (49.8%) | 254/408 (62.3%) | 1,004 |

| Difficulty (`d10ca77`) | n | Answered | Strict | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 243 | 137 | 159 | 118/173 |
| medium | 446 | 434 | 142 | 207 | 64/101 |
| hard | 174 | 165 | 56 | 93 | 52/77 |
| extra | 166 | 162 | 45 | 56 | 20/57 |
| **all** | **1,034** | **1,004** | **380** | **515** | **254/408** |

**What the removal cost.** Against the 2026-10-01 7B run (`ca60bd6`, 866 strict) the removal-alone run is
not a paired comparison: the code also differs by the commits between `ca60bd6` and `4679bed`.

| | strict | lenient |
|---|---:|---:|
| win | 11 | 10 |
| loss | 523 | 409 |
| both correct | 343 | 460 |
| both wrong | 157 | 155 |

455 of the 523 strict losses had been served from the 7B's SQL. The other 68 came from the search's own
candidates, which the old arbiter ranked differently or which commits since `ca60bd6` changed; these runs
do not separate the two. The search fixes then added 30 strict wins and 4 losses (`4679bed` to `d10ca77`).

**Where the rest is.** The pool-oracle ablation of the removal-alone run (`--selection pool_oracle`, tag
`no-sql-model-4679bed-pool`, never serving) finds a strict-correct candidate in the search's pool for
**544/1,034 (52.6%)**, lenient 626, scalar 286/408; the same run's top-1 is 363 strict (that mode grades
the selected member without its tied rows). So about 180 questions are ranking misses with the right query
already pooled (first strict hit at rank 1: 28, rank 2: 37, rank 3: 19, deeper: 97), and about 490 have no
correct candidate. 30 questions on `d10ca77` (32 on `4679bed`) have no candidate that runs and grounds:
those are the only questions on which the Gemini fallback would run.

**Latency.** Per-question prediction seconds, `d10ca77`: median 1.71, p90 4.60, p95 5.66, max 19.22, on a
machine shared with other evaluations (the removal-alone run: median 1.28, p90 2.73). The 7B run measured a
median of 20.12 and a p90 of 39.23.

Outputs: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002` (`full_eval_no-sql-model-*.json` and their
per-example files).

## Join grounding, fresh whole_db DEV run (2026-10-01)

`engine/sql_grounding.py` now also makes a pool member ineligible when it equates two columns the foreign keys keep
apart (`DECISIONS.md`, 2026-10-01). The fresh serving-faithful run (`spider/probe/full_eval.py --config whole_db`,
tag `join-grounding-ca60bd6`, source `ca60bd6` clean, the same XiYanSQL 7B Q4_K_M GGUF `50840d65…` at 8 threads, row
cap 5,000, `served` selection, SQL backend) scored **866/1,034 strict (83.8%)** and **869/1,034 lenient (84.0%)**,
against 865 and 868 for the frozen 2026-09-29 run. All 1,034 questions routed to the typed-AST planner; 1,031
answered and 3 errored (car_1 #104, #135, #136: no connected AST candidate, as in the frozen run).

| | strict |
|---|---|
| win | 1 (car_1 #151) |
| loss | 0 |
| both correct | 865 |
| both wrong | 168 |

Served SQL changed on 2 questions. The win is car_1 #151 ("Which distinctive models are produced by maker with the full
name General Motors or weighing more than 3500?"): the proposer had joined `model_list.ModelId = car_names.Model`, a
key with a name column that never matches; the search's join through `car_names.Model = model_list.Model` is served.
The other is student_transcripts_tracking #575, whose decode exceeded its CPU budget in the frozen run and now
finishes; it is wrong in both. Before the run, a replay of the frozen run's 1,022 importable served winners through
the rule predicted exactly the #151 change. Prediction seconds: median 20.12, p90 39.23, p95 45.45, max 59.03.
Output: `%LOCALAPPDATA%/Temp/prereasoner-join-grounding-20261001`.

## Serving answered 180 DEV questions from compose (2026-09-30; measured, fixed in `06819d6`)

The evaluator (`spider/probe/full_eval.py`) lets compose own a question only when `compose_owns` accepts it, so every
DEV question is scored on the typed-AST planner. Serving did not. From `0397569` (2026-09-10) to `06819d6`, the compose
host also gave compose any plan with a composition op whenever the request carried an analysis context, and every
served request does. Measured with serving's own gate on all 1,034 DEV questions (`whole_db`, row cap 5,000, the
learned primitive head's depth evidence, then a composition op in `ComposeEngine.run(..., world=None)`'s plan):

| | questions | strict correct |
|---|---|---|
| served by compose | **180/1,034** (17.4%) | compose **3/180** |
| same questions, scored planner (frozen replay above) | 180 | **146/180** |

Composition ops on those 180: having 51, top-N 43, yoy 40, time filter 29, sort 25, share 7, running 3. So production
answered about 722/1,034 of DEV-shaped questions correctly, not the 865 above. The measurement ran on commit `abcfb82` (code
clean; only documentation uncommitted) against the frozen replay's per-example verdicts; the two scripts are
session scratch, not committed. `06819d6` removes the branch, and serving now asks `route()` alone, as evaluation does. The evaluator's own
numbers do not change: it never had the branch.

## 7B hardening-source full CPU replay (2026-09-29; not a deployment)

The current release-hardening source was evaluated end to end on all 1,034 Spider DEV
questions with the frozen served arbiter and XiYanSQL QwenCoder 7B Q4_K_M on CPU (8 threads,
no gold-value substitution). The replay has 1,034 unique ordered indices, 1,030 answered
rows, and **865/1,034 strict (83.72%)** / **868/1,034 lenient (83.95%)**. Difficulty strict
results: easy 234/248, medium 378/446, hard 150/174, extra 103/166. Against the paired
`clean-engine-q4-8t` replay, strict labels changed on seven examples: **4 wins / 3 losses**,
861 both-correct and 166 both-wrong; strict total is +1. The two replay outputs differ on 23
SQL rows. The official test-suite score on those 23 changed outputs is 15 correct for this
source and 14 for the paired control; the other 1,011 SQL outputs are identical.

The separate pinned `taoyds/test-suite-sql-eval` execution metric is **832/1,034 (80.46%)**.
It used evaluator commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`, 695 test-suite SQLite
files, `keep_distinct=true`, and **did not pass `--plug_value`**. All 1,034 rows completed in
73 bounded evaluator segments with no timeout; four invalid/failed predictions were replaced
by a deliberately invalid SQL sentinel. This is a Spider-only benchmark, not product accuracy.
Its paired control (`clean-engine-q4-8t`) is 831/1,034 when derived from the exact 23 changed
rows; this is not the different historical 839/1,034 deployed record below.

Reproduction evidence is currently in
`%LOCALAPPDATA%/Temp/prereasoner-7b-hardening-20260929/`:
summary SHA256 `336dcdf29142b18018d74ebc595cc4cb25434921b71cd945c27666d2ed1592f9`, per-example
JSON SHA256 `d4333943bab9c721b30f8c46a3d205811f8febea5caec25cf76f900b290a70c1`, and official
metric JSON SHA256 `6aa19c39b6450d51849260b3497daa8fbdaec02bb6274edf22055a01c42fec12`. Source contract
commit `40cd5a44947edaa62b788210afdfd1fcf112875b`, proposer SHA256
`50840d65a753074a670d7929ca0a4b5d633b0a4b435f1a68b4a6fba26c4d18bb`, arbiter SHA256
`fc84162a4dc9900f963bbea751ccf13f2d9218842a0f781ee1cb7e964584977d`, and dirty-tree flag
false. The current serving/evaluation source files match release image source `7817f69`.

The accuracy target is crossed, but complete-evaluator latency is **19.335s p50, 37.111s p90,
42.834s p95, 61.705s max**; 893/1,034 requests exceed the 12s soft target. Errors are three
connected-AST search misses (`car_1`, indices 104/135/136) and one XiYan CPU decode-budget
failure (`student_transcripts_tracking`, index 575). Therefore this is an accuracy result, not
yet a demonstrated 12-second CPU-serving result. A paired 32-row, 8-per-difficulty thread pilot
replayed the exact same SQL on all 32 rows; increasing `SQL_PROPOSER_THREADS` from 8 to 16 moved
subset p50/p90/max from 14.781/26.544/35.609s to 12.271/22.146/28.839s, while 18/32 still
exceeded 12s. This pilot is CPU-host-specific, not Cloud Run evidence; do not promote the 16-thread
override without exact 8-vCPU concurrency measurement. Production remains on revision
`prereasoner-api-00122-zc4`, 100% traffic; this replay did not change traffic.

## Model-matched-neutral selector pilot (2026-09-29; rejected)

On a preregistered CPU-generated 240-question sample from Spider TRAIN (24 questions per each of
10 databases), one gold query was invalid SQL and is retained in the denominator without labels;
239 rows were labeled. The fitted selector used 144 questions / six fit databases and was replayed
against the frozen arbiter on the same 96 examples / four selector-held-out databases. The exact
pool file SHA256 is `2e98974a4c8b1308afef40e7b9f105631aba7e1419e6832eb2d7f077a3be46bd` and the
experimental arbiter SHA256 is
`e37d48dd50fe380c2d220cf0fd3b577e94d0ab9810f84e29d7e7f47fad129061`.

| Selector | Held-out strict | Eligible pool oracle |
|---|---:|---:|
| Frozen production arbiter | 82/96 (85.4%) | 85/96 (88.5%) |
| Newly fitted neutral-sentinel arbiter | 82/96 (85.4%) | 85/96 (88.5%) |

Paired outcome: **0 wins, 0 losses, 82 unchanged-correct, 14 unchanged-wrong**. Per held-out DB,
both selectors scored customer_complaints 19/24, program_share 24/24, student_1 21/24, and
wine_1 18/24. The gain gate failed, so the experimental selector was **not promoted**. This pilot
holds out selector-fitting databases only; XiYan pretraining exposure is unknown. It is a small
TRAIN-side selector diagnostic, not the full Spider DEV result and not an unbiased generalization
estimate. The frozen full-DEV replay and official test-suite evaluation are still required for the
current source tree.

## Integrated 7B production release and final replay (2026-09-29)

The single production Cloud Run service now runs the merged 7B implementation at 100% traffic:
revision `prereasoner-api-00122-zc4`, image
`us-central1-docker.pkg.dev/prereasoner-inference/prereasoner/engine@sha256:82f8f154d8c655bb23e05e0c1a98d956175f3ef1ab13a3fbfe6eb3bd9313b79c`.
The source merged via PR #30 (`ad4f4076fc8f7b09c146811e84aa96802794ce2a`); health, release,
live disposable-PostgreSQL product, CPU HTTP, and browser checks passed.

The production-matched full Spider DEV diagnostic is **864/1,034 strict (83.56%)** and
**867/1,034 lenient (83.85%)**, versus the historical 0.5B main baseline **647/1,034 strict
(62.57%)**: +217 examples / +20.99 percentage points. Full row-by-row replay comparison found
zero SQL, strict-label, or identity differences from the initial complete replay. The identical
predictions score **839/1,034 (81.14%)** on the official Spider test-suite evaluator. These are
separate metrics: DEV is a repeatedly consulted engineering/tuning set, while test-suite is a
Spider-only benchmark and does not measure product joins. Neither should be described as an
unbiased generalization estimate or as multi-source product accuracy.

Final full-evaluator prediction latency p50/p90/p95/p99/max was **7.172/16.384/17.840/19.965/
138.576 seconds**, with 220/1,034 beyond the evaluator's soft 12-second threshold. A separate
bounded HTTP smoke on the exact image reached a concurrency-8 maximum of 11.648 seconds; the two
paths/loads differ. Keep the full-replay latency tail visible as follow-up optimization work.

The RunPod replay artifacts were copied and hash-verified before task pod termination; final
itemized pod billing was `$6.81668396841269`. The worker was confirmed gone. No production database
was used for the live suite, and no signed-in user request was submitted.

## Integrated 7B release-candidate diagnostic (2026-09-28; historical pre-deployment record)

Using the pinned XiYanSQL Q4_K_M CPU proposer integrated with the unified engine and its served
neutral-sentinel arbiter, the exact production-matched full DEV replay scored **864/1,034 strict
(83.56%)** and **867/1,034 lenient (83.85%)**. This is +217 strict examples / +20.99 percentage
points over the historical 0.5B baseline below (647/1,034, 62.57%). Spider DEV was consulted
repeatedly during model and selector development, so this is an engineering/tuning result, not an
unbiased generalization estimate. It is not the official Spider TEST score and does not measure
multi-source Knowledgebase joins. This subsection records the pre-deployment release-candidate
measurement; the production release and final replay are recorded above.

| Difficulty | n | Answered | Strict | Lenient | Scalar-gold |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 246 | 233 | 227 | 163/173 |
| medium | 446 | 446 | 378 | 385 | 97/101 |
| hard | 174 | 173 | 149 | 155 | 74/77 |
| extra | 166 | 166 | 104 | 100 | 47/57 |
| **all** | **1,034** | **1,031** | **864** | **867** | **381/408** |

As a separate benchmark, the official `taoyds/test-suite-sql-eval` evaluator at commit
`e97acc546ecbee8fa27fa8dbf025ef61493a876c`, run on the official Spider test-suite databases
without `--plug_value`, scored **839/1,034 (81.14%) test-suite execution accuracy** (easy 233/248,
medium 375/446, hard 148/174, extra 83/166). This metric evaluates Spider databases only; it does
not test production Knowledgebase joins. Test-suite database archive SHA-256:
`9ec24ea8debc6bd04abfe137b5f1a739b5a8836f32c0464e4dfc94eb7f41da96`.

Reproduction artifacts were hash-verified from the CPU3c run: summary SHA-256
`05792b3a3304a40d5e119673813d71f20ef58f5ac461c431b0874d72dc8fe56a`, per-example JSON
`1bcec9318fe3bbb6a430e9a401722a52e530255f45d67a01840ce1efae30ea26`, checkpoint contract
`0d9987aa6d21a64f6bef0982c853b147df866d2f29294e707ae60b2ae9860610`. Contract source commit is
`0c7e98050f7ab9a88066b6197dccd684c700b9a8`, with the model SHA
`50840d65a753074a670d7929ca0a4b5d633b0a4b435f1a68b4a6fba26c4d18bb` and arbiter SHA
`fc84162a4dc9900f963bbea751ccf13f2d9218842a0f781ee1cb7e964584977d`. The contract records
`worktree_dirty=true`; use its individual artifact hashes rather than the commit alone as
provenance.

Latency at this historical checkpoint was a release caveat, not hidden by the accuracy
result: the Spider evaluator observed
p50/p90/p95/p99/max **7.513/17.614/21.686/129.397/231.588 seconds**, with **273/1,034** beyond
its soft 12-second budget (no cancellation). An earlier bounded Cloud Run API smoke on the
candidate image was materially faster (concurrent-8 max 11.648 seconds); these measure different
request paths and are reported separately. A privacy-safe targeted probe of the ten longest rows
finished in 4.3–7.2 seconds on the same eight-thread CPU; proposal spans were 3.7–6.5 seconds,
encode at most 1.56 seconds, and pool execution under 8 ms. The earlier 142–232-second spikes did
not reproduce in the controlled sample. The completed final replay and updated latency telemetry
are summarized above; this paragraph is retained as the earlier checkpoint record.

The 0.5B baseline table below remains historical and is not overwritten by this candidate result.

This is the historical, reproducible 0.5B reference measurement (not the current 7B deployment):
`engine/tables.py:TableQuery.select_query` — the deterministic typed-AST search (25 candidates), the
d2 SQL proposer (4 beams, every line imported, validated and re-rendered), in-memory pool execution, the
literal-grounding eligibility rule (`engine/sql_grounding.py`), and the linear arbiter
(`engine/data/sql_arbiter.json`). It was measured through the production entry point
(`spider/probe/full_eval.py`, `--selection served`, SQL backend) over the Spider dev set: 1,034 examples
and 20 databases. The summary JSON (`full_eval_served_grounding_whole_db.json`) records its exact source
commit, code and model hashes, settings, and `worktree_dirty=false`.

Measured: **2026-09-24** from clean source commit `841f08c` (tag `served_grounding_whole_db`). The
engine built from `900f3b1` serves it; that commit changes nothing the selection reads. The 2026-09-25
release also checks reversed literal comparisons (`'Lyon' = city`). That only removes eligibility, and
none of the 1,034 selected queries contains such a comparison, so every dev answer and this result are
unchanged (DECISIONS.md). The money-noun rule added later that day ("the sales" in a table named `sales`
is its money total) fires only when a money noun names a table. None of the 20 dev databases has such a
table and 0 of the 1,034 dev questions fire it, so it cannot change a dev answer either.

`6d8b7dd` (2026-09-28) replaced eleven surrogate-key tests with one (`engine/sql_schema.is_surrogate_key`),
and the search reads it, so it was measured again. The run used the same settings, dev set, tables,
encoder, arbiter and proposer as the table below. It ran from `3a40c8e` with that change uncommitted
(`worktree_dirty=true`); every recorded source file and the harness are byte-identical to `6d8b7dd`,
except `engine/deterministic/service.py`, which the SQL backend does not load. Result: 647 strict, 696
lenient and 304/408 scalar, with the same counts in every difficulty. Two selected queries changed (wta_1
#451, tvshow #629), and both are wrong before and after. So the strict and lenient transitions are 0 wins
and 0 losses. The run's JSON is not committed, so the table keeps `841f08c` as its evidence.

| Configuration | Evidence commit | Strict | Lenient | Scalar-gold |
|---|---|---:|---:|---:|
| `whole_db` — all tables, gold-blind (standard Spider comparison) | `841f08c` | **647/1,034 (62.6%)** | **696/1,034 (67.3%)** | **304/408 (74.5%)** |

| Difficulty | n | Answered | Strict | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 246 | 198 | 200 | 145/173 |
| medium | 446 | 442 | 278 | 310 | 72/101 |
| hard | 174 | 168 | 96 | 112 | 53/77 |
| extra | 166 | 165 | 75 | 74 | 34/57 |
| **all** | **1,034** | **1,021** | **647** | **696** | **304/408** |

**What the grounding rule changed, against `6c39942` (below).** A pool member is ineligible when it tests a
text column against a literal the column never holds while another column of the database does. The rule
rejected at least one pool member on 97 questions and changed the served query on 45. Strict transition:
**7 wins, 5 losses**, 640 unchanged-correct, 382 unchanged-wrong. Lenient: 8 wins, 5 losses, 688, 333. The
wins are real mis-bindings the arbiter had preferred (world_1 2, orchestra 2, cre_Doc_Template_Mgt 1,
student_transcripts_tracking 1, tvshow 1). All five losses are in flight_2, whose airport codes carry
leading spaces so most gold queries return nothing: the mis-bound filters also returned nothing and scored
as correct, while their grounded replacements match rows or have another shape. Three flight_2 questions
now have no eligible member and raise in the search stage (13 raise in all, up from 10).

**Latency.** Not comparable with the previous run: this one shared the CPU with the full release test suite
for about three hours (median 30.3 s, p90 56.1 s per question). The rule itself scans cell values only when
a pool member compares a column with a text literal.

### Previous served measurement (2026-09-23)

Measured from clean source commit `6c39942` (tag `served_whole_db`), before the grounding rule. This was
the planner that shipped on 2026-09-23 (engine revision built from `e993476`).

| Configuration | Evidence commit | Strict | Lenient | Scalar-gold |
|---|---|---:|---:|---:|
| `whole_db` — all tables, gold-blind (standard Spider comparison) | `6c39942` | **645/1,034 (62.4%)** | **693/1,034 (67.0%)** | **304/408 (74.5%)** |

| Difficulty | n | Answered | Strict | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 246 | 195 | 197 | 142/173 |
| medium | 446 | 443 | 277 | 309 | 73/101 |
| hard | 174 | 170 | 98 | 113 | 55/77 |
| extra | 166 | 165 | 75 | 74 | 34/57 |
| **all** | **1,034** | **1,024** | **645** | **693** | **304/408** |

Ten examples raise in the AST search stage (the pool is empty or nothing executes); all 1,034 route to the
own-data planner, as Spider exercises no world knowledge.

**Parity with the measured candidate.** The served selection reproduces the evaluator-injected candidate
(`arbiter_s2_d2`, 645/1,034, below) on every example: identical SQL on **1,034/1,034**. The strict
transition matrix is therefore 0 wins, 0 losses, 645 unchanged-correct and 389 unchanged-wrong. The
commits after `6c39942` changed serving only under an analysis context (compound questions, the
single-query serving contract, decomposition leaves), the orchestrator, and the currency calculation
check. `select_query` itself is unchanged, and no dev question carries a currency intent, so this
measurement applies to the deployed code.

**Latency (this run).** Per-question `prediction_seconds` on a shared 8-core workstation CPU (fp32, other
jobs running): median 23.1 s, mean 25.4 s, p90 42.0 s, max 96.3 s. Production on Cloud Run
(8 vCPU / 16 GiB) during the release gate, end to end including the conversational model: turns with one
engine call median 13.3 s (p90 35.3 s, n=117); decomposed questions median 60.2 s (p90 140.4 s, n=27).
Beam generation dominates; inference hardware is the lever.

### Previous planner: deterministic search only (2026-09-06)

Measured from clean source commit `93bc1b3` with the serving-faithful selector of its time
(`serving_top1`, max 25 candidates). Kept as the baseline of the tables below.

| Configuration | Evidence commit | Strict | Lenient | Scalar-gold |
|---|---|---:|---:|---:|
| `whole_db` — all tables, gold-blind (standard Spider comparison) | `93bc1b3` | **359/1,034 (34.7%)** | **453/1,034 (43.8%)** | **224/408 (54.9%)** |
| `gold_tables` — oracle table set (planner upper bound) | `93bc1b3` | **434/1,034 (42.0%)** | **551/1,034 (53.3%)** | **247/408 (60.5%)** |

The whole-db row is the standard comparison number: it includes table-set selection. The oracle row feeds
only tables referenced by the gold query and isolates AST reasoning and ranking. Relative to whole-db, the
oracle removes **75 strict misses (7.3 percentage points)**, **98 lenient misses (9.5 points)**, and **23
scalar misses (5.6 points)**. This is the measured table-selection opportunity, not a claim that oracle
tables are available in production.

## Difficulty (previous planner, `93bc1b3`)

### `whole_db`

| Difficulty | n | Answered | Strict | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 243 | 121 | 138 | 107/173 |
| medium | 446 | 432 | 128 | 179 | 54/101 |
| hard | 174 | 163 | 63 | 88 | 46/77 |
| extra | 166 | 162 | 47 | 48 | 17/57 |
| **all** | **1,034** | **1,000** | **359** | **453** | **224/408** |

### `gold_tables`

| Difficulty | n | Answered | Strict | Lenient | Scalar |
|---|---:|---:|---:|---:|---:|
| easy | 248 | 238 | 133 | 157 | 113/173 |
| medium | 446 | 430 | 164 | 239 | 60/101 |
| hard | 174 | 159 | 85 | 97 | 54/77 |
| extra | 166 | 159 | 52 | 58 | 20/57 |
| **all** | **1,034** | **986** | **434** | **551** | **247/408** |

`strict` is exact row-set equality and is a harsh lower bound. `lenient` is value containment and is a
generous upper bound. `scalar-gold` is the clean single-value subset, where denotation comparison is least
ambiguous. The evaluator reports all three because projection and row-shape differences can make one metric
misleading by itself.

Both configurations route all 1,034 examples to `ast`; Spider is self-contained and does not exercise the
world-enrichment path. The whole-db run had 34 AST-search errors; the oracle run had 48. These are
execution/search failures, not refusals.

## Candidate-pool recall (pool_oracle ablation)

Measured 2026-09-17 at source commit `9e3d687` (worktree dirty with unrelated docs/web edits; the
fingerprinted engine-artifact hashes recorded in each JSON are what identify the measured tree).
`--selection pool_oracle` executes every pooled candidate and scores the example by its best member —
an explicitly labeled oracle ablation, like `gold_tables`, never a serving mode. It measures the
ceiling that ANY ranking improvement can reach with today's candidate generation. The same run also
reports serving top-1, so the ranking gap is apples-to-apples. Note the top-1 at `9e3d687`
(365 strict) differs from the `93bc1b3` headline (359) by planner changes landed between the commits.

| Run | Pool strict (ceiling) | Pool lenient | Same-run top-1 strict | Ranking gap |
|---|---:|---:|---:|---:|
| `whole_db` @25 | 478/1,034 (46.2%) | 594 (57.4%) | 365 (35.3%) | 113 (10.9 pts) |
| `whole_db` @100 | 484/1,034 (46.8%) | 609 (58.9%) | 365 (35.3%) | 119 |
| `gold_tables` @25 | 504/1,034 (48.7%) | 652 (63.1%) | 437 (42.3%) | 67 (6.5 pts) |
| `whole_db` @25 + parsimony expander | 533/1,034 (51.5%) | 612 (59.2%) | 365 (35.3%) | 168 (16.2 pts) |
| `whole_db` @25 + A4 variant families | **543/1,034 (52.5%)** | 620 (60.0%) | 365 (35.3%) | 178 (17.2 pts) |

The parsimony row is the same evaluation after `engine/sql_parsimony.py` landed (tag
`pool25_parsimony_a3`): a deterministic expander that adds, per pooled candidate, its
minimal-join reduction, single-binding reductions of duplicate-named projections, and
drop-one-column reductions, all carrying a generation penalty so serving top-1 is unchanged
by construction (365 = 365, measured). It raises the pool ceiling above the `gold_tables`
oracle and moves the value-superset (lenient-only) family's in-pool rescue rate from 14.2%
to 47.8% — headroom deliberately parked in the pool for a structural reranker to convert.

Findings:

- **Pool recall, not the candidate cap, is the wall.** Quadrupling the cap to 100 adds 6 strict
  examples (+0.6 points); the mean pool holds ~11 candidates against a cap of 25. Generation
  exhausts itself below the cap, so "enumerate more" is not an available lever.
- **Perfect ranking over today's pool tops out at 46.2%** (48.7% with oracle tables). That is the
  measured ceiling of the enumerate-and-rank architecture with the current grammar and proposal
  rules.
- **459 of the 478 in-pool strict hits sit within the top 10 ranks** (first-hit rank histogram in
  the summary JSON), so a reranker over the head of the pool captures nearly all of the ranking gap.

## Strict-miss families

`python -m spider.probe.report pool25_whole_db` (heuristic first-divergence attribution over the
`whole_db` @25 pool run at `9e3d687`; spot-checked). "In-pool" counts misses whose strict-correct
candidate already exists in the pool — the subset ranking alone can rescue.

| Family | Misses | % of 669 | In-pool (rescuable by ranking) |
|---|---:|---:|---:|
| table-set superset (over-join) | 215 | 32.1% | 40 (18.6%) |
| projection row-shape (lenient-only) | 113 | 16.9% | 16 (14.2%) |
| grammar-blocked (static, Probe A) | 113 | 16.9% | 27 (23.9%) |
| projection other (wrong columns/operands) | 111 | 16.6% | 14 (12.6%) |
| table-set subset/different (true retrieval) | 38 | 5.7% | 6 (15.8%) |
| operator/filter/group/order | 47 | 7.0% | 10 (21.3%) |
| error (no answer) | 32 | 4.8% | 0 |

Spot-check notes: the over-join family is candidates that include the right tables plus extras
(a parsimony/scoring failure, not table retrieval — with `gold_tables` the family disappears and its
examples resurface as projection misses); true retrieval misses (missing or wrong tables) are only
~6% of strict misses. Projection misses are right-table queries projecting the wrong columns or
aggregating the wrong operand.

## Trained rank head (Phase B experiment, evaluator-injected)

Candidate head `b2` (NOT promoted; serving stays deterministic-only): trained on 6,998
execution-labeled Spider-TRAIN pools built through the production search
(`training/rank/build_pool_labels.py`, labels sha256 `a9cf0096…`), seed 7, top-10 window,
hidden 64, val-selected margin 0.25. Offline: val pairwise AUC 0.803, held-out-db top-1
+3.0 points. Serving-faithful whole_db with the head injected (`--rank-head`, tag
`rankhead_b2`): **strict 395/1,034 (38.2%) vs 365 deterministic (+30)** — transition
46 wins / 16 losses / 349 unchanged-correct / 623 unchanged-wrong; lenient 477 vs 454.
The `gold_tables` sanity run (tag `rankhead_b2_gold`, 422 vs 437) executed against newer
variant-generation code than the head's labels (recorded artifact hashes differ) and is
not clean evidence. Train gold is training data only — no gold-derived signal reaches any
serving decision, and dev is never used for training.

Candidate head `b3` (labels rebuilt against the A4 variant families, sha256 `a5b7aab5…`,
top-15 window, hidden 128, margin 0.75): whole_db strict **394/1,034 (38.1%)** — the same
plateau as `b2` with a cleaner transition (40 wins / 11 losses vs A4 deterministic 365);
clean `gold_tables` sanity 427 vs 437 (−10, still regressing). Read together: label volume
and pool diversity are no longer the constraint — the canonical-feature bottleneck is. The
A4 ceiling gain (533→543) converted to zero top-1 gain because the 50 canonical features
cannot separate a correct deep-rank variant from its sibling distractors.

Candidate head `b5` (sum/max/count canonical aggregates, same labels as `b3`) had the best
offline validation of any head (+4.4 points held-out) and FAILED both Spider gates:
whole_db 388, `gold_tables` 403 (−34), transition 55 wins / 32 losses. Offline validation
and dev accuracy anti-correlated across b3→b5, so richer within-distribution features made
the head less calibrated out of distribution; the vectorizer change was reverted (git is
the archive).

Candidate head `b6` (b3 recipe, labels mixed 50/50 with gold_tables-config pools, sha256
`4c7da516…`): whole_db 385 (below the ≥390 gate) with the `gold_tables` regression reduced
to 432 vs 437 (−5, from −10). Distribution mixing repairs gold robustness directionally but
dilutes the primary configuration. Feature-head program conclusion across b1–b6: `b2` is
the best whole_db candidate (+30 to 395/1,034), no head passes both gates, and further
gains require proposal-side change (Phase D), not ranking change. No head is promoted.

## Learned proposer (Phase D experiment, evaluator-injected)

Import ceiling: `training/proposer/import_gold.py` maps Spider TRAIN gold into the engine's
typed AST with execution-verified round trips at **6,297/7,000 (90.0%)** coverage — the
AST language was never the wall; enumeration was. Candidate proposer `d1` is a budgeted
CPU LoRA SFT of Qwen2.5-0.5B on those targets (1,200 steps, seed 7, db-held-out val,
50% exact-string match on held-out decodes). Injected into the evaluator as ONE greedy,
frozen, penalized proposal per question — re-imported through the same importer and
engine-validated (tag `pool25_proposer_d1`):

| Measurement | Without proposer | With d1 proposer |
|---|---:|---:|
| Pool ceiling, strict (whole_db @25) | 543 (52.5%) | **747 (72.2%)** |
| Pool ceiling, lenient | 620 (60.0%) | 789 (76.3%) |
| Scalar-gold in pool | 283/408 | 342/408 |

553 novel validated proposals, 59.3% strict-precision; 204 examples are strict-covered
ONLY by the proposer. The deterministic policy "a novel validated proposal is
selected, otherwise the deterministic top-1" (`--selection proposer_first`) scores
**568/1,034 (54.9%) strict, serving-faithful** (tag `policy_d1`) — matching the
record-level counterfactual exactly (552 proposals selected, 59.4% strict when selected;
228 wins / 40 losses vs deterministic 380), as a fully deterministic pipeline must.

Candidate proposer `d2` (same targets and seed, pod-scale SFT: 6,000 steps at effective
batch 16 via `training/tools/runpod_api.py lease`): held-out string-exact was flat vs `d1`
(45/100 vs 25/50) but execution-level dev quality improved — policy serving strict
**587/1,034 (56.8%)** (tag `policy_d2`; 567 proposals selected, 61.7% strict when
selected). String-exact undercounts equivalent SQL; execution measures decide. `d2` is
the standing proposer candidate. Its pool ceiling is 751/1,034 (72.6%) vs `d1`'s 747 —
pod-scale training bought precision, not coverage; coverage is the beam lever. The
standing configuration's own regression pair is clean: `policy_d2_gold` (greedy) scores
**656/1,034 (63.4%)** vs the 437 deterministic gold baseline — greedy beats beams on the
oracle distribution too (656 vs 619).

Value-linked experiment `d4` (same targets/seed, prompts carry sampled column values via
the shared serialization rule; pod-trained): the strongest adapter of the program —
held-out string-exact 55/100 (vs `d2`'s 45), precision-when-selected 66.7% (vs 61.7%),
and `gold_tables` policy **667/1,034 (64.5%)** (vs 619 with `d3` beams, 437 deterministic).
whole_db policy is 585 — a wash against the 587 gate — because better calibration
REDUCES novel proposals (508 selected vs 567) and the override-on-novel policy cannot
express precision gains; greedy pool 745 (vs `d2`'s 751). Three independent measurements
(the 825 beam ceiling, the failed agreement counterfactuals, and `d4`'s
precision-without-yield) locate the one remaining bottleneck at SELECTION over
proposer-inclusive pools; standing serving config remains `d2`-greedy at 587 pending a
learned arbiter.

Beam experiment `d3` (same `d2` weights, 4 deterministic beams): pool ceiling
**825/1,034 (79.8%)**, but beam-best selection over-fires (812 selections at 56.9%
precision) and scores 576 — rejected against the 587 gate. Offline counterfactuals over
the recorded denotation hashes (serving-available signals only): first-novel 576 —
matching the serving run exactly — agreement-gated variants 428–569; none clears the
+10 confirmation threshold. Finding: beams add ~23 points of pooled-but-unselectable
headroom (825 vs 587); converting it requires learned arbitration over
proposer-inclusive pools, not a code policy. `gold_tables` sanity with the proposer
policy: **619 (59.9%) vs the 437 deterministic baseline (+182)** — the proposer
transfers across pool distributions because it proposes from question+schema and never
scores pools; the feature heads' failure mode does not apply. Every step stays deterministic and auditable: frozen
greedy decode, the one importer, the one validator, generation-penalized pools. Serving
latency is the open promotion constraint (fp32 CPU decode ~5s/question; the Phase D
step-1 measurement requires a quantized runtime). Nothing is promoted.

## Arbitration pilot (Step 3): FUNDED

Bounded pilot over 20 train databases (15 fit / 5 validation, validation held out from
BOTH proposer and selector fitting; split + provenance in
`training/rank/data/experiments/pilot/`). Pools: deterministic + d2-beams + d4-greedy,
every candidate teacher-force scored; per-source completion verified (2,041/2,041 each);
exact frozen-policy replay from a dedicated d2-greedy pool. Held-out validation (416
questions, denominators fixed):

| Selector | Strict | vs frozen policy |
|---|---:|---:|
| Oracle ceiling | 302 (72.6%) | — |
| Deterministic top | 106 (25.5%) | — |
| Frozen proposer_first (exact replay) | 189 (45.4%) | baseline |
| S1 likelihood, exec-filtered | 219 (52.6%) | +7.2 pts |
| **S2 logistic, exec-filtered (mixed pools)** | **237 (57.0%)** | **+11.5 pts** |
| S2 on d2-beams pools only | 224 (53.8%) | +8.4 pts |

S2 gains on ALL five validation databases (+6…+16): the funding gate (≥ +5, spread, all
sources complete) passes. S2 is a feature-only baseline — the semantic-scorer branch
stays open regardless.

**Serving-faithful confirmation (tag `arbiter_s2_d2`): 645/1,034 (62.4%) strict** on
whole_db through the production entry point — `--selection arbiter` executes the merged
d2-beams pool (op-bounded), scores every candidate under the proposer prompt, and selects
by the pure-linear pilot artifact. Transition vs the 587 standing policy: 125 wins /
67 losses / 520 unchanged-correct. Difficulty slices: easy 78.6%, medium 62.1%,
hard 56.3% (−0.6 vs policy — the one non-gaining slice), extra 45.2%. The pilot's
held-out +8.4-point estimate landed at +5.6 on dev: the tuning-set caveat stands, but
the held-out methodology predicted within range. Selector capture is 645 of the 825
pooled ceiling (78.2%) — both 90/90 requirements (coverage, capture) remain the
distance to 80%.

### Standing arbiter gold-table sanity (completed 2026-09-23)

`full_eval_arbiter_s2_d2_gold.json` records **689/1,034 (66.6%) strict** for the
standing d2-beam arbiter on `gold_tables`, versus deterministic 437 and d2 greedy
proposer policy 656. This is an oracle-schema sanity check, not whole-db accuracy.
The standing whole-db result was 645/1,034 (62.4%); it was promoted later the same day and reproduced through the served path (top of this file).
Per-example records and the summary carry the run's original code/artifact identity.

### Relabel takeover: corrected shard feature namespaces (2026-09-23)

The initial partial-data refit reported 223/416 but passed `shard1_d2beam` and
`shard2_d2beam` as feature namespaces that `vector()` does not recognize. Its flat
result cannot establish a data ceiling. After canonicalizing shards to `d2beam`,
the same available data produces **225/416**, versus the frozen pilot's 224.
This remains diagnostic: 4,672/6,262 fitting questions are present; 1,590 are missing.
Duplicate records now fail instead of silently replacing source features. The
unchanged pilot mixed-pool control was refit separately and reproduced **237/416**.
These are cached validation comparisons, not new serving results. Candidate artifacts
are under `training/rank/data/experiments/relabel/`; original artifacts are preserved.

The enlarged partial-data **mixed** refit gives **234/416**, versus matched control
237/416. Neither partial refit demonstrates a useful accuracy gain; these are not
unbiased generalization estimates and do not establish a definitive data ceiling.

### Rejected planner coverage trial (2026-09-23)

Fresh `whole_db` / `pool_oracle` / 25-candidate run
`full_eval_takeover_coverage_20260923.json` measured entity-aggregate set alternatives
and positive-membership cues, without proposer or learned ranker: **544/1,034 pool
strict (52.6%)**, **364/1,034 same-run top-1 (35.2%)**. Against historical A4's
543/365, paired pool outcomes are 1 win / 0 losses / 543 unchanged-correct /
490 unchanged-wrong; top-1 outcomes are 0 wins / 1 loss / 364 unchanged-correct /
669 unchanged-wrong. All 1,034 records are present; 32 search errors, no over-budget
examples. Historical evaluator/AST fingerprints also differ, so this comparison
is not a fully isolated causal ablation.

The trial did not pass the no-regression gate. Both uncommitted production-planner
edits and their trial-specific tests were removed; existing owners match HEAD again.
The summary and per-example evidence remain as a rejected experiment, not the current
planner result. Independently tested importer/source-identity fixes remain. Standing
proposer/arbiter whole-db score stays **645/1,034**, not remeasured by this trial.

### Semantic scorer pilot: accuracy gate failed (2026-09-23)

The pinned Qwen 0.5B scalar head + rank-8 LoRA completed the preregistered seed-7,
200-step fp32 schedule (800 sampled question pairs, 578.26 optimizer seconds).
Five validation DBs were excluded from fitting; these DBs were previously consulted
and are a development comparison, not a new untouched holdout. Scoring consumed no
correctness labels and covered every executable candidate on all **416 questions**.

| Cached mixed-pool selector | Strict |
|---|---:|
| Matched feature-only control | 237/416 (57.0%) |
| Semantic scorer | **210/416 (50.5%)** |
| d2 mean-likelihood control (cannot choose unscored d4-only candidates) | 219/416 (52.6%) |
| Full-pool oracle | 302/416 (72.6%) |

Against the matched control: **24 wins, 51 losses, 186 unchanged-correct,
155 unchanged-wrong**. Per-DB semantic/control: csu 28/32, manufactory 53/63,
music_1 41/35, music_4 45/54, soccer 43/53. The fixed gate required >=247 correct,
gains on >=3 DBs, and no DB losing >5; all three accuracy conditions failed.
The two selectors' oracle union is 261/416, not an achievable gold-blind policy.
Do not tune a switching threshold on these reporting labels.

Scorer-only RTX 4090 timing passed the provisional runtime gate: median **0.234s**,
p95 **0.575s**, total 111.43s. This excludes proposal generation and SQL execution,
and provides no CPU/quantized or end-to-end feasibility result.

Training checkpoint was recovered after CUDA became unavailable in the initial
scoring process. An inference-only recovery lease used identical checkpoint/code,
dependency versions and hash-verified base bytes; no retraining or checkpoint choice.
Both pods terminated, zero active pods verified. Estimated compute about USD 0.47,
not an invoice. Evidence: `training/rank/data/experiments/semantic_launch/` contains
the approved contracts, complete comparison, decision, downloaded checkpoint and scores.
Checkpoint manifest SHA256: `91ef6f0ed11f3460cad42d11f20801e0f5e927ab98637a4924e16d289455a1fe`.
The candidate is **not promoted or integrated**. Standing whole-db remains **645/1,034**.
This rejects the bounded standalone configuration, not all semantic scoring hypotheses.

## Evaluation-protocol caveats (read before quoting numbers)

- **Spider dev has served as the program's tuning set.** No training ever saw dev, but
  roughly fifteen branch decisions (adapters, beam counts, selection policies, gates) were
  made on dev aggregates, so dev numbers are engineering scores with selection bias, not
  unbiased generalization estimates. Selector/arbiter development happens on train-side
  held-out databases only.
- **Final holdout reservation:** the official Spider TEST split is designated as the one
  final evaluation set. It is deliberately NOT downloaded into this repository until a
  frozen complete configuration is ready for a single final run; nothing can be tuned on
  data that is not present.
- Pool sizes with an injected proposer are `max_candidates + K novel proposals`
  (proposals append after the deterministic cap); "@25" labels refer to the deterministic
  pool budget.

## Reproduce

```bash
python -m spider.probe.fetch_data
python -m engine.fetch_weights
python -m spider.probe.full_eval --dbs spider/data/dbs --config whole_db --tag <tag>
python -m spider.probe.full_eval --dbs spider/data/dbs --config gold_tables --tag <tag>_gold
```

The evaluator runs the served selection with the runtime bundle; it takes no model or selection-mode
flags (`--selection pool_oracle` adds the labeled pool-ceiling ablation). Commands recorded with
earlier results used the evaluator flags of their time. Run from a clean commit for release evidence. The evaluator intentionally records a dirty-worktree flag
and invalidates mismatched checkpoints so predictions cannot silently be mixed across code or model trees.
Use [`../README.md`](../README.md) for the probe methodology and [`../../docs/SQL_AST.md`](../../docs/SQL_AST.md)
for the planner contract.

## Interpretation

The pool_oracle and miss-family measurements above replace the earlier guess that table-set
retrieval was the major next lever. What is now measured:

- **Ranking is worth up to 16.2 points after the parsimony expander** (168 misses whose correct
  candidate is already pooled; before the expander: 113/10.9 pts). This is the ceiling for any
  reranker that does not change candidate generation further.
- **Candidate generation is the dominant constraint.** 83% of strict misses have no correct
  candidate anywhere in the pool, the cap is not binding (@100 adds 0.6 points), and the two
  biggest families — over-join (215) and projection identity (224 combined) — are mostly
  unrescuable by ranking because the parsimonious or correctly-projected variant is never
  proposed. Accuracy work must therefore widen *proposal* coverage (which table sets, projections,
  and shapes get enumerated), not the pool cap and not table retrieval (true retrieval misses are
  ~6%).
- **Grammar coverage bounds the rest**: 113 statically grammar-blocked misses plus the residual
  gap between the 48.7% oracle-table pool ceiling and 100%.

Older benchmark rows remain available in git history. They are not repeated here because they used retired
planner or routing implementations and are not comparable to this report.
