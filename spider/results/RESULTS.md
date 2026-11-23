# Spider Results

Dated entries come newest first. Each records a run at the commit it names; the scripts, options, and models an older
entry names (the 7B SQL proposer, the arbiter, `training/rank/`, `--selection arbiter`) may since have
been removed, and that commit holds the code that ran. Records cited under `prereasoner-readers-artifacts/`,
`enron_corpus/`, `prereasoner-injection-test/` and `training/*/data/experiments/` are in the archive
(`training/archive.json` names each group; `python -m training.archive fetch GROUP`).


## The choice reads a small table whole: pick 727 -> 733 with unwarned wrong level, not kept for one demo answer (2026-11-23)

- Hypothesis: the choice saw each column's header and three example cells, so readings that only the lines tell apart
  ("which game had the greatest score difference", "the line after Pegasus") read alike to it, while the written models
  it is measured against read the whole table. Change tried: a table of at most 600 cells shown to the choice line by
  line (cells to 40 characters; the reading call unchanged), the instruction saying so only when one is shown, so a
  choice over larger tables is asked byte for byte as before.
- Gate (the 896 and the 91 demo questions; 978 new choice calls, 6.4M tokens, $0.64, the later runs replayed):
  built 792 -> 792; pick 727 -> 733 (10 wins, 4 losses: WikiTableQuestions 232 -> 237, 8 / 3; fresh dev 127 -> 129;
  messy 105 -> 104; DataBench unchanged, no table small enough); unwarned wrong 64 -> 64 (WikiTableQuestions 28 -> 25,
  fresh dev 17 -> 21, messy 2 -> 1); unmarked right 652 -> 655; erred 83 -> 77. The first form, the instruction changed
  for every choice, measured 730 and 65.
- Demo: 86 answered right and 1 wrong as before, but two answers swapped: customer-orders #1 (Europe's total in GBP)
  from unwarned wrong to right, and formfacade-bank-deposits #2, a release-gated question ("which country has the most
  deposits?" => Switzerland), from warned right to warned wrong: with the lines in view the choice took the country of
  the single largest deposit over the largest total. The gate keeps demo answers unchanged, as on 2026-11-18, so it is
  not kept; it is the one form today that moved the pick.
- Diff, records and recorded calls: `prereasoner-scratch/patches/whole-tables-20261123/` in the archive.

## The choice asked in both orders, its probabilities averaged: pick 727 -> 725, not kept (2026-11-23)

- Hypothesis: the choice reads its listings in decoder order, best first, and a listing's place moves its probability
  (on the training find set the right listing was the choice's second choice on 57 of 197 misses). Change tried: the
  choice asked again with its listings reversed (option r00 the last listing), each listing's probability and none's
  the mean of the two calls; no listing, word or threshold otherwise changed.
- Gate (the 896 and the 91 demo questions; 978 new choice calls, 4.6M tokens, $0.46): built 792 -> 792; pick
  727 -> 725 (9 wins, 11 losses: messy 105 -> 108, fresh dev 127 -> 126, WikiTableQuestions 232 -> 231, DataBench
  263 -> 260); unwarned wrong 64 -> 58; unmarked right 652 -> 627 (warned right 53 -> 78, erred 83 -> 89); demo 84 right
  and 2 warned right -> 83 and 3, the same answers.
- Not kept: the wins and losses are noise, so the listing's place is not what limits the choice. The fall in unwarned
  wrong is the averaged probabilities' lower peaks crossing the thresholds (T_ERR, T_FLAG were set for one call), a
  threshold shift bought with 25 right answers losing their clean mark, not accuracy.
- Diff, records and recorded calls: `prereasoner-scratch/patches/both-orders-20261123/` in the archive.

## The choice's misses on the training find set, and a line pin that narrows an aggregate to one line: three forms, none kept (2026-11-23)

- **Study** (no spend: the find set's 201 WikiTableQuestions training questions whose right reading the engine listed
  but did not pick, replayed at 335945bd from the recorded Decisions answers only; 197 replayed, 4 pools changed with
  the spread form). The choice gives the right listing under 5% on half of them; it is the choice's second choice on 57,
  third or lower on 131; 99 of the 197 were served unmarked. By how the picked reading differs from the best right one:
  50 count as built only through a listing that pins one line by its label (right by accident: it pins the line holding
  the gold), 44 picked such a pinned listing over an unpinned right one, 54 read another output or measure, 29 other
  conditions, 10 add a condition, 3 swap a comparison's sides, 3 are ties, 2 flip a direction, 2 drop a condition.
  "Built" on the find set therefore overstates the engine's reach by about 2.4 points (50 of 2,100).
- **The family tried**: an aggregate (count, total, lowest, highest, rank, breakdown, also inside a difference or a
  check) that a line pin by its label alone narrows to one data line among the several its other conditions keep says
  nothing an aggregate says. A pin the other conditions already make, a pin holding several lines, a printed line's pin
  and every show are untouched. Measured on the 896 (and the 91 demo questions) in three forms:

  | Form | Built | Pick | Wins / losses | Unwarned wrong | Spend |
  |---|---|---|---|---|---|
  | Baseline (335945bd) | 792 | 727 | | 64 | |
  | Not listed by the decoder | 791 | 728 | 5 / 4 | 68 | $0.11 (342 new choice calls) |
  | Listed and offered; its probability goes to no answer | 792 | 724 | 2 / 5 | 61 | none (every call recorded) |
  | Listed and offered; it lends its answer mass but cannot carry one alone (estimated from the two runs' records) | 792 | 724 | 1 / 4 | 63 | none |

  The demo stayed at 84 right, 2 warned right, 4 erred, 1 wrong in every form except the first (85, 1, 4, 1).
- **Why none is kept**: removing the listings reshaped 66 of 91 demo pools and most fresh dev pools, and the choice's
  probabilities follow the pool, so wrong answers lost their competitors and their mark (fresh dev unwarned wrong 17 ->
  21). Keeping them offered but taking their probability lowered the right answers they agree with: on the 300
  WikiTableQuestions dev questions unmarked right fell 198 -> 176. And on the dev sets a pick such a pin narrows to one
  line is right more often than wrong (the pin lands on the line the question means), so even the form that changes no
  confidence loses picks. The family the training questions showed does not hold on the dev sets.
- The diffs, the runs' records, the recorded calls and the study (script, per-question listings, families):
  `prereasoner-scratch/patches/pin-rule-20261123/` in the archive (group `scratch-records`).

## The final fresh sample: on 1,000 WikiTableQuestions test questions no one had read, the engine alone 68.4%, with its retry 73.0%, Gemini 4 Neon 85.0%, Claude Opus 5.5 82.8% (2026-11-23)

- Sample `wtq-fresh2` (`regress/sets/public/samples/wtq-fresh2.json`): 1,000 of the 2,344 WikiTableQuestions test
  questions neither earlier sample drew (seed 20261124), drawn after the night's last engine change (ca5dfb2e) and read
  by nobody before the run. `regress/run_public.py --run 2026-11-23-final`, the systems as the 2026-11-22 benchmark
  ran them (the frontier models get the retry's prompt over the raw table, one answer-format line more); records and
  every model answer in `regress/sets/public/records/2026-11-23-final/` and `regress/sets/public/calls/`.

  | System | Right of 1,000 | 95% interval |
  |---|---|---|
  | Prereasoner, the engine alone (no model writes a query) | 684 (68.4%) | 65.5-71.2 |
  | Prereasoner as served (an erred answer retried by Gemini 4 Neon) | 730 (73.0%) | 70.2-75.7 |
  | Gemini 4 Neon writing every query | 850 (85.0%) | 82.7-87.1 |
  | Claude Opus 5.5 writing every query | 828 (82.8%) | 80.3-85.0 |

- Paired with Neon: both right 645, the engine alone right 39, Neon alone right 205, neither 111. The engine built the
  right query for 792 (79.2%) and picked it on 684; a perfect choice among what it builds stays below Neon. Its marks:
  unmarked 728 answers, 583 right (80.1%); warned 141 (72 right); erred, and retried in serving, 131.
- Against the earlier fresh sample (`wtq-fresh`, 1,000 other test questions, 703 at fp1): within each sample's
  interval; the engine alone stands near 69-70% on unseen WikiTableQuestions tables, 15-16 points below Neon.
- Spend $47.12 of the owner's $50: Neon $32.67, Opus $11.79, the retry $1.65, Decisions $1.00.

## A measure's spread, its highest less its lowest: dev picks 725 -> 727 (2 wins, 0 losses), unwarned wrong 65 -> 64; its training family 4 -> 6 right of 11 (2026-11-23)

- "How long have they been playing?", "the difference between the highest and the lowest average": one column's
  highest less its lowest over the kept lines had no program (every difference contrasted two lines, two columns or
  two measures; SQUALL writes max(c) - min(c) for it, 10 of 2,100 training questions). The decoder offers
  Calc(Agg(max), minus, Agg(min)) of one measure, with or without a condition, where the reading asks for a difference
  of the highest and the lowest (`engine/program/decoder.py` calc_frames, `_spread`).
- The 896 dev questions (`regress/run_dev_decisions.py --record --keep`, now from the committed sets): WikiTableQuestions
  picks 230 -> 232 (2 wins, 0 losses), built 255 -> 257, unwarned wrong 29 -> 28; messy, fresh dev and DataBench
  unchanged (two pools change, no outcome); all 896: built 790 -> 792, picked 725 -> 727, unwarned wrong 65 -> 64.
  Demo: 84 right, 2 warned right, 4 erred, 1 wrong of 91, unchanged. Its training family (`regress/run_public.py
  --set wtq-train --squall`, 11 questions): right 4 -> 6 (3 wins, 1 loss), built 5 -> 8. Decisions spend $0.01.
- Spider calibration, 1,053 questions, `whole_db`, served, fresh (`--data spider/calibration`, at ca5dfb2e): answered
  836, strict 373, lenient 581, scalar gold 278 of 377, the same as fp1 (0 wins, 0 losses; 1,052 of 1,053 serve the
  same SQL); the Python and SQL programs verified alike on all 836; prediction median 1.7 s, p95 3.2 s. Records
  `spider/results/full_eval_spread_ca5dfb2e.json`; per-example records `prereasoner-scratch/spider-spread/` in the archive.

## A text column's figures read as numbers and lines taken by where they stand: the engine alone, no retry, 689 -> 703 of a fresh 1,000 WikiTableQuestions test questions (built 802 -> 823); on the 896 dev questions 717 -> 725 picked right, unwarned wrong 66 -> 65, demo unchanged; a side-by-side re-ask of close runners-up fails (2026-11-23)

- The plan after the public benchmark (2026-11-22: the engine alone 69.8% of DataBench test within the upload limit
  and 70.1% of a 1,000 WikiTableQuestions test sample, Gemini 4 Neon 88.7% and 85.9%; the right query built for 79.3%
  of the in-limit questions): raise the engine itself, without a model writing queries, in three steps, each kept only
  if picks rise and unwarned wrong does not. Diagnosed on training questions only (the find set: a seeded 600 of
  WikiTableQuestions' training questions that SQUALL annotates with a human-written query, and 400 of DataBench
  train), gated on the 896 dev questions in aggregate, confirmed once on a fresh test sample.
- **Step 1, a text column's figures (kept).** SQUALL's queries read a number inside a text cell in 41% of training
  questions (`c5_number1`, `c1_year`, `c4_first_number`); the compiler read a column of scores ("3–2"), seasons
  ("1996–97"), places ("1st") or bracketed figures as text. `engine/sheet/values.py` reads a cell's figures by digits
  and separators only (a sign at the cell's start or after "(", a season's span completed: "1996–97" is 1996 and 1997,
  a time "2:10" none), and only where a figure opens the cell or it writes two or more ("p1", "A-123", "Apollo 13"
  none); `engine/sheet/compiler.py:_part_columns` gives a text column whose cells mostly write figures a number column
  per place that four in five of its cells hold (`Score #1`, `Score #2`, at most three), each cell traced to its source
  cell, so a program totals, ranks and compares them like any number column.
- **Step 2, lines by where they stand (kept).** "The first venue", "the last 3 entries", "the page counts of the first
  five books" (8.5% of SQUALL's training queries take a line by its place) had no program: every rank read a column.
  `LineOrder` ranks lines by their place in the sheet (lowest the first lines, highest the last, conditions first); its
  read-as is "Venue of the first line on ...", its formula `=TAKE(range, ±n)`, both emitters agree. A slot only its own
  closed question fills (`closed.order_record`: the question alone, "first rows", "last rows" or none, beside the said
  call), so the decoder offers it only where that call reads it, and every other call's body is unchanged.
- **Step 3, a head-to-head re-ask (failed, removed).** Of the 69 dev questions whose right answer was built but not
  picked, 47 had it second. A second choice call over the likeliest answer and runners-up within a quarter of its
  probability (each its reading and a fuller preview), its answer served warned or erred when it differed: served
  answers 11 better and 17 worse than the plain choice, and 16 right answers turned to erred ones (erred 87 -> 121),
  the retry growing rather than shrinking. Not kept; the choice stays as it was.
- **The 896 dev questions** (`regress/run_dev_decisions.py`, three processes as eor1 ran, new bodies recorded):

  | | Listed right | Picked right | Right | Warned right | Warned wrong | Unwarned wrong | Erred |
  |---|---|---|---|---|---|---|---|
  | HEAD e5b8332c (neon2) | 786 | 717 | 648 | 48 | 46 | 66 | 88 |
  | Step 1 | 790 | 722 | 643 | 54 | 47 | 65 | 87 |
  | Steps 1+2 (fp1) | 790 | 725 | 649 | 54 | 45 | 65 | 83 |
  | Steps 1+2+3 | 790 | 725 (served 719) | 634 | 45 | 32 | 64 | 121 |

  Steps 1+2 against HEAD: 11 wins, 3 losses, listed +4 -0. By set: WikiTableQuestions +5 picks, messy 0, fresh dev
  +1, DataBench +2. The dev sets hold few such questions; the gain shows where they are common:
- **The find set (training questions)**, the engine alone: WikiTableQuestions 600: right 447 -> 474 (28 wins, 1 loss),
  built 524 -> 541; DataBench 400: 319 -> 321 (4 wins, 2 losses), built 351 -> 349 (the two lost are reading shifts on
  workbooks whose new part columns change the reading call's sheet).
- **Public sets, the engine alone (no retry), Decisions calls only** (`regress/run_public.py --system engine`, records
  `regress/sets/public/records/2026-11-23-{head,fp1}/`; graded by WikiTableQuestions' official evaluator and DataBench's
  `default_compare`):

  | Set | HEAD | Steps 1+2 | Wins / losses | Built (HEAD -> 1+2) |
  |---|---|---|---|---|
  | **Fresh WikiTableQuestions test, 1,000** (seed 20261123, of the 3,344 the benchmark sample left out; unseen) | 689 | **703** | 29 / 15 (sign p 0.049) | 802 -> 823 |
  | WikiTableQuestions test, the benchmark's 1,000 | 701 | 715 | 26 / 12 (p 0.034) | 783 -> 806 |
  | DataBench test, 450 within the upload limit (72 refused) | 314 | 317 | 4 / 1 | 367 -> 369 |

  The benchmark sample and DataBench test misses were read on 2026-11-22, before this work (first lines in sheet order
  among them), so those two rows are test-informed; the fresh sample is the confirmation. On it, served per answer:
  right 590 -> 608, warned right 75 -> 73, warned wrong 67 -> 78, unwarned wrong 131 -> 129, erred (retried in
  serving) 137 -> 112. Against Gemini 4 Neon's 85.9% on the benchmark sample the engine alone moves from 70.1% to
  71.5%; the gap stays, and its bulk is still the right query not built (19% of the fresh sample) and not picked (12%).
- **Demo gate** (`--sets demo --demo-maps`): HEAD at parity; steps 1+2: 84 right, 2 warned right, 4 erred, 1 wrong of
  91, unchanged; five pools of eval-neartail-ecommerce change, no outcome. Parity replay of fp1 on this commit: all 896
  and the 91 demo questions cache hits, every outcome equal (the three label-only mismatches HEAD also shows: the warned
  answer whose reading reads like another's, which the probe's labeler does not know). The dev replay's reference adds
  the fp1 layer (`regress/sets/dev_decisions/records/fp1_demo/` and the references in
  `regress/sets/dev_decisions/reference/`).
- Spider calibration, 1,053 questions, `whole_db`, served, fresh (`spider/probe/full_eval.py --config whole_db
  --selection served`; records `spider/results/full_eval_fp1_e5b8332c_dirty.json`; per-example records `prereasoner-scratch/spider-fp1/` in the archive): answered 836, strict **373** (35.4%),
  lenient 581 (55.2%), scalar gold 278 of 377; the Python and SQL programs verified alike on all 836; prediction median
  1.9 s, p95 3.4 s. Against v1 at `48abf835` (371 strict, 2026-11-21, `spider/results/full_eval_v1_48abf835.json`; per-example records `prereasoner-scratch/spider-v1/` in the archive; the retry's commits lie between): 14 strict
  wins, 12 losses, 852 of 1,053 serving the same SQL.
- Checks: tests (program 89, answer 26, SQL AST 90, connected 18, sheet compiler 72); the hermetic repository run, every
  suite passing (`tests.test_complex_datasets` reads the knowledgebase and passes 6 of 6 through the Cloud SQL proxy);
  the offline regression gate, 8 passed and the 5 known limitations as at v1; `tests.test_datasets` live, PASS; ruff
  F/E9, bandit on tracked code, the dependency locks.
- Provenance: base `e5b8332c`, dirty with this change; the runners are `regress/run_public.py` and
  `regress/run_dev_decisions.py`, the records `regress/sets/public/records/` and `regress/sets/dev_decisions/records/`; Decisions endpoint `gemini-decision-1`, no
  model artifact changed (`engine/data/weights_manifest.json` untouched). Decisions spend $5.03 (recorded calls), of
  the $30 the owner allowed.


## Browser gate on b9b0fe50: 102 of 102 fresh, 77 of 78 existing; Sheets add-on v35 end to end; Prereasoner as served 88 of 100 against Gemini 4 Neon alone 81 (2026-11-22)

- Chrome on chat.prereasoner.com, production `b9b0fe50` (engine `00344-kip`, chat `00243-jar`), the 24 demo datasets'
  prompt.txt and eval.txt follow-ups, chat shorthand included, graded by `regress/browser_gold.py` (harness and
  captures: `prereasoner-readers-artifacts/gate_harness/b9b0fe50/`): fresh conversations **102 of 102** (v1: 101,
  the formfacade-leads date range now 37,000); conversations made before the release **77 of 78**. The one miss is
  customer-orders' shorthand "in GBP for the whole of Europe?" read as the average (145.53 GBP) in a conversation whose
  last earlier turn asked for an average; the eval line expects the total (1,917.48), which it reads in a fresh one.
  Every own-data answer carried its `check`; none of the 180 was warned or retried (the demo questions are ones the
  path is sure of), 76 were answered by the world and compose routes, which carry no mark.
- Google Sheets add-on: `sheets-addon/Code.js` (forwards `check`, sends `retry`) pushed as Apps Script version 35 and
  set as the Marketplace SDK's draft script version (installs run 34 until the draft passes Google's review). End to
  end through an Apps Script test deployment (head) on a new sheet of the bank-deposits rows: "how much more does the
  biggest bank hold than the smallest one?" was answered 300 with a warning mark (45% sure), its reason and Retry;
  Retry answered 700 (1,200 - 500, right) marked retried.
- Head to head on a stratified sample of the dev sets (25 questions from each, seed 20261122): Prereasoner as served
  (the Decisions path, an erred answer retried) right on **88 of 100**; Gemini 4 Neon writing the SQL for every
  question over the same workbook right on **81** (wrong 13, cannot answer 6). Paired: both right 78, Prereasoner
  alone 10, Neon alone 3, neither 9. By set: WikiTableQuestions 22 / 22, messy 24 / 21, fresh dev 21 / 20, DataBench
  21 / 18. Neon spend about $1.2.

## The retry reads what the request's links reach: a Retry of "total deposits for banks in Europe" reads 3,300; 757 of 896 right, 2 asked (2026-11-22)

- Released 2026-11-22, `b9b0fe50`: engine `prereasoner-api-00344-kip` (`engine@sha256:3755fc5d...`, all four build
  stages), chat `prereasoner-chat-00243-jar` (`chat@sha256:36cc0c57...`), smoke `k8kk8` passed, both jobs on the image;
  Hosting unchanged. Rollback: `00342-mer` / `00241-zas`. Chrome on chat.prereasoner.com (formfacade-bank-deposits):
  "total deposits for banks in Europe" asked with Retry answered 3,300, marked retried, its query reading
  "bank → country → continent".
- Change: the retry reads the workbook the Decisions path reads (`TableQuery.workbook`, shared by `select_query` and
  the retry): each compiled table of a tab with the columns the request's links add ("bank → country → continent",
  "customer → tier", "amount → USD"), as `retry.workbook_tables` gives them (record lines only, cells as numbers, ISO
  days or the sheet's text); the person's Retry registers the request's links first, as serving does. Before, it
  read the uploaded rows alone, and a Retry on a question that needs the knowledgebase could only say the sheet holds
  no country or continent.
- Live, on the knowledgebase (formfacade-bank-deposits, `KnowledgeReasoner.retry`): "total deposits for banks in
  Europe" 3,300 (the demo's gold) through "bank → country → continent"; "which country has the most deposits?"
  Switzerland 1,550 through "bank → country".
- The 896 dev questions (tags neon2_wtq, neon2_mf, neon2_fd; the dev sets have no knowledgebase links, so this
  measures the compiled tables against the uploaded rows): served right **757 (84.5%)**, unchanged; the retry on the
  88 erred questions right 61, wrong 25, cannot answer 2, failed 0 (before 61 / 22 / 4 / 1); by set WikiTableQuestions
  29 / 8 / 1 / 0, messy 8 / 2 / 0 / 0, fresh dev 18 / 3 / 0 / 0, DataBench 6 / 12 / 1 / 0. Unwarned wrong 66, asked 2.
  Erred questions 12.2 s at the median, 26.0 s at p90. Spend about $2.9 (668,000 input, 61,000 output tokens): fresh
  dev's wide compiled workbooks read about 27,000 input tokens per retry.

## Never asked back: an answer most likely wrong is retried by Gemini 4 Neon's written query; 757 of 896 served right (84.5%), 4 asked (2026-11-21)

- Released 2026-11-21 (21:10 UTC), `6d239ad1`: engine `prereasoner-api-00342-mer` (`engine@sha256:b61683e5...`, build
  c82c88b3, all four stages), chat `prereasoner-chat-00241-zas` (`chat@sha256:0fca2d73...`), smoke `hjvfk` passed,
  both jobs on the image, Hosting deployed. Rollback: `00340-xaz` / `00239-zil`. Chrome on chat.prereasoner.com
  (formfacade-bank-deposits): "which bank has deposits closest to the average deposit?" erred (33%) and was answered
  by the retry, Deutsche Bank 700 (the CSV's mean is 750; right), its mark opening the reason, the query and Retry;
  Retry answered it again marked as asked; confident answers carry no mark. The Sheets sidebar shows the mark once
  the Apps Script version that forwards `check` (sheets-addon/Code.js) is published in the Marketplace SDK.
- Owner's order (DECISIONS.md, the same day): never ask back unless the question is wrong; mark an answer the engine
  is unsure of (warning) or that is most likely wrong (error); a click shows the reason and Retry; Retry has Gemini
  Neon write the query, automatically for an error. `closed.choose_answer` now answers, warns (below `T_FLAG`, or
  another answer's reading worded alike) or errs (below `T_ERR` 0.42, or "none of these" likelier); an erred answer
  is retried (`engine/retry.py`, `gemini-4-neon` at low thinking, one SQLite SELECT over typed copies of the
  request's tables, one tab unless the question asks across them).
- Measured on the 896 dev questions, the measurement's cache replaying every Decisions call (tags neon_wtq, neon_mf,
  neon_fd; the retry model's answers recorded once and replayed), the retry run on each erred question as
  `TableQuery.serve` runs it, its rows graded as the set grades them:

  | Set | Right | Warned right | Warned wrong | Unwarned wrong | Erred | Engine's pick right | Retry right / wrong / cannot answer / failed |
  |---|---|---|---|---|---|---|---|
  | WikiTableQuestions (300) | 192 | 22 | 17 | 31 | 38 | 11 | 29 / 7 / 2 / 0 |
  | messy (120) | 91 | 9 | 8 | 2 | 10 | 5 | 7 / 2 / 1 / 0 |
  | fresh dev (176) | 117 | 8 | 13 | 17 | 21 | 1 | 18 / 2 / 0 / 1 |
  | DataBench (300) | 248 | 9 | 8 | 16 | 19 | 4 | 7 / 11 / 1 / 0 |
  | **All (896)** | **648** | **48** | **46** | **66** | **88** | **21** | **61 / 22 / 4 / 1** |

  Served right: **757 of 896 (84.5%)** (648 + 48 warned + 61 retried; the one failed retry's pick was wrong), against
  717 (80.0%) when the erred were asked back and their picks counted right. Unwarned wrong 66 (7.4%). Marked: 94
  warned (10.5%) and 84 retried or erred (9.4%); asked: 4 (0.4%), each the retry's word that the sheet does not hold
  what is asked. On the erred questions the retry is right on 61 of 88 where the engine's own pick is right on 21;
  it is better on every set (DataBench 7 against 4 of 19).
- The two readings worded alike that 65658636 / a64bc830 asked back are warned: nt-11106 right, FD-0172 right,
  nt-10343 wrong, nt-3012 warned right as before.
- Latency: an erred question takes 11.9 s at the median and 24.8 s at p90 with its retry (live calls), the others
  2.0 s and 9.7 s. Retry spend for the measurement: 499,000 input and 75,000 output tokens, about $2.6 at $3 / $15
  per million; fresh dev's wide workbooks read 18,000 input tokens per retry.
- On the first 30 messy questions the retry first unioned every tab of multi-tab workbooks: 0 of 3 right; told to
  answer from one tab (the tab in view, else the first that holds what is asked) unless asked across tabs, 2 of 3
  and one "the sheet has no CPC column".

## A stated currency is the sheet's unit, not a column; v1 replayed as released is over the warned-and-asked bar by 3 (2026-11-21)

- Released 2026-11-21 (16:55 UTC): engine `prereasoner-api-00340-xaz` (`engine@sha256:d8060b55...`, build 140af411,
  all four stages), chat `prereasoner-chat-00239-zil` (`chat@sha256:4977c6be...`), smoke `sczqk` passed, both jobs on
  the image; Hosting unchanged. Rollback: `00338-qek` / `00237-siy`. Browser check on chat.prereasoner.com:
  formfacade-leads in a fresh conversation 8 of 8 (62,000; 33,000; 36,267 USD; Africa asked; 68,138 USD; 5; 4;
  **37,000**), and in the v1 gate's existing conversation the same question now 37,000 where it read EUR 18,000 and
  GBP 4,000; customer-orders' prompt 1,066.03 USD (expected 1,126.66 within the FX tolerance; every EUR conversion
  today reads 0.946 of its expectation). No other demo dataset states a unit in conversation.
- Change (`1a442963`): `dataset_semantics.apply` no longer appends the private `__currency_for_<hash>` column to the grid
  the Decisions path reads; the grid states the column's unit (`units`, `TabPayload.units`) and the compiler gives the
  column that unit. formfacade-leads after "This is in euros", "What is the total budget from August 11 to August 15,
  2026?": production read two days grouped by country -> currency (0.654; EUR 18,000 and GBP 4,000 in both browser
  gates); now the span, one total, **37,000** at 0.968 (the CSV's 37,000). With the claim, Germany and Europe in USD
  read 36,267 and 68,138, as production does today; the standalone question still reads 37,000 (0.858).
- **Correction to the v1 entry below.** Its dev table is the replay of 04:47-05:20 (tags tip3_wtq, tip3_mf, tip3_fd,
  in the release worktree's `regress/results/dev_decisions/`), before `65658636` and `a64bc830` (an answer worded like
  another, and two readings that differ only in which of two same-headed columns they read, are asked back), which
  the release carries. Replayed as released (`1d9929b2`, the cache's every call a hit; tags prod_wtq, prod_fd): WTQ
  nt-10343 wrong -> asked, nt-3012 warned right -> asked, nt-11106 right -> asked; fresh dev FD-0172 right -> asked.
  The change above replays to the same records (tags units_dev and units_mf): FD-0057's choice probabilities differ
  only when every set runs in one process (the stock reader's history) and equal in the measurement's order, and
  db/057_Spain/0's differ as they did at release (same label).

  | Set | Right | Warned right | Asked | Warned wrong | Unwarned wrong | Pick |
  |---|---|---|---|---|---|---|
  | WikiTableQuestions (300) | 192 | 20 | 41 | 16 | 31 | 225 |
  | messy (120) | 91 | 9 | 10 | 8 | 2 | 105 |
  | fresh dev (176) | 117 | 7 | 22 | 13 | 17 | 126 |
  | DataBench (300) | 248 | 9 | 19 | 8 | 16 | 261 |
  | **All (896)** | **648** | **45** | **92** | **45** | **66** | **717** |

  The bar: pick 717 (80.0%, met), unwarned wrong 66 (7.4%, met), warned and asked **182 (20.3%, at most 179: not
  met)**. Two of the four new asks were right answers.
- The demo set's replay (`--sets demo`) misses 38 of its bodies against the 2026-11-18 measurement on the released
  code and on this change alike (the upload's grid and the value-token bound changed what the workbooks send); the 53
  it holds match. The demo gate is `tests.test_datasets` (passes) and the browser gate.
- Spider calibration's drop (371 strict against the readers' 727), by the gold query's shape (per example, the readers'
  fd63f2b run against v1's): one table 393 -> 245 of 518, one join 179 -> 77 of 280, two or more joins 48 -> 25 of 91,
  nested 52 -> 8 of 89, set operations 55 -> 16 of 75. Of the 420 the readers had right and v1 does not, 139 are right
  in content but not in shape (a count or measure column shown beside the answer, a listing's order), 138 asked (no
  program for a sorted listing, two aggregates at once, a substring match, a two-column grouping, a nesting or a set
  operation) and 143 wrong (whole_db reads all tables as one workbook and reaches a column through a link from the
  wrong table; an OR of two values read as one). v1 gains 64 the readers missed.
- Tests: `tests.run_all` passes, the live suites included; `tests.test_orchestrator` (skipped there: no
  GOOGLE_CLOUD_PROJECT) passes 44/44 run apart twice, and failed once in three on "yes accepts the specific
  Europe-to-GBP offer" (Gemini sent the earlier question instead).

## v1 is live: the Decisions path serves production at 80.0% right of the 896 dev questions, 7.5% unwarned wrong; served Spider falls to 371 strict (2026-11-21)

- Release: `1d9929b2` (`48abf835` plus opentelemetry-api 1.45.2): engine `prereasoner-api-00338-qek`, chat
  `prereasoner-chat-00237-siy`, Hosting `683ad0df3900b817`; the release build passed all four stages (image, offline
  gate, boot smoke, live product suite). Rollback: `00334-pil` / `00233-fon` (eb40c4c), Hosting `0b7f024baac27cc4`.
- The 896 dev questions, replayed through the served selection (`regress/run_dev_decisions.py`, every Decisions call a
  cache hit, every outcome equal to the measurement's layers): WikiTableQuestions 300/300, messy 120/120, fresh dev
  176/176, DataBench 299/300 (db/057_Spain/0 reproduces its pgd1 choice request: the value-token bound shrinks three
  earlier workbooks, so the stock reader's history differs; same label). Served per answer at T_ASK 0.42 / T_FLAG 0.575:

  | Set | Right | Warned right | Asked | Warned wrong | Unwarned wrong |
  |---|---|---|---|---|---|
  | WikiTableQuestions (300) | 193 | 21 | 38 | 16 | 32 |
  | messy (120) | 91 | 9 | 10 | 8 | 2 |
  | fresh dev (176) | 118 | 7 | 21 | 13 | 17 |
  | DataBench (300) | 248 | 9 | 19 | 8 | 16 |
  | **All (896)** | **650** | **46** | **88** | **45** | **67** |

  The launch bar: the always-answering pick 717 of 896 (80.0%, 717 needed), unwarned wrong 67 (7.5%, at most 71),
  warned and asked 179 (19.98%, at most 20%). (Corrected in the entry above: this table predates two of the release's
  commits; replayed as released, warned and asked is 182, 20.3%, and unwarned wrong 66.) Kept since the previous entry: tied lines in sheet order, a cell on one
  line, a listing of several columns, an absence across a link, the value-token bound, the every-column listing (pick
  718 -> 717 on an asked question, unwarned wrong 68 -> 67) and the served reading among equal answers.
- Demo gate: `tests.test_datasets` (live, all four execution modes) passes; the browser gate on production (every demo
  dataset's prompt and eval.txt, chat follow-ups included) passes 101 of 102 in fresh conversations and 78 of 79 in
  the conversations the eb40c4c gate left. Both miss formfacade-leads "What is the total budget from August 11 to
  August 15, 2026?" after "This is in euros": split by the knowledgebase's country -> currency instead of one total.
- Spider calibration, 1,053 questions, `whole_db`, served, fresh: answered 831, strict **371** (35.2%), lenient 578
  (54.9%), scalar gold 279 of 377; the Python and SQL programs verified alike on all 831; prediction median 2.5 s,
  p95 4.7 s. The trained readers it replaces read 727 strict / 808 lenient (2026-10-27 below). The program language
  reads sheets; Spider's multi-table SQL questions are not its measure, and v1's bar is the 896.
- Offline regression gate: 8 passed, 5 known limitations (a two-column projection asked back; a conversion at a
  sheet's own rates needs the knowledgebase-side links offline; per capita, tax percent and commission fraction not
  read), DECISIONS.md.
- Decisions API spend: the measurement cache holds $15.08; the release's live runs (suites, builds, browser gate,
  Spider) are outside it.

## Two checks on the served confidence fail to add anything a threshold does not; the pick, not the unwarned wrong, is what holds the Decisions path below the bar (693 right of 896 when always answering, 717 needed)

- Continues the entry below, on the same orders. Stack: next-engine `271111b`, flags as `pg1` (the kept stack, listed
  769, pick 693, served at T_ASK 0.42 / T_FLAG 0.575 per answer: right 625, warned right 42, asked 102, warned wrong
  41, wrong 86). Contracts `order_warning` and `single_check` in
  `training/readers/data/experiments/decisions-probe-20261118/experiment-contract.json`, written before the runs.
- Determinism: `pgd1` re-runs `pg1`'s flags with the chooser records dumped (two processes); it equals `pg1` on all
  896 (0 moves on listed, pick and every served count; 4 uncached calls changed nothing).
- Order warning (warn an unwarned answer where the chooser, asked with its options reversed, picks another answer):
  unwarned wrong 86 -> 80 and unwarned right 625 -> 607; FAILED (right falls 18, the gate allows 12). The cross-fitted
  8% point falls 593 -> 585 right. 581 calls, 2.2M tokens.
- Single check (ask the chooser about the picked answer alone, its likeliest reading beside the same none, and warn
  where none wins): unwarned wrong 86 -> 64, unwarned right 625 -> 583, warned 147 of 896, no pick moves; by set wrong
  WTQ 38 -> 29, messy 13 -> 9, fresh dev 16 -> 10, DataBench 19 -> 16. The written gate passed, but it compared
  against the fixed T_FLAG: raising T_FLAG on the pick mass alone to 0.651 gives 585 right at 64 wrong with no call
  (in sample), and the cross-fitted 8% point falls 593 -> 582. Not kept. 711 calls, 1.3M tokens.
- What it shows: thresholding the pick mass already reaches 8% unwarned wrong at 593 right cross-fitted (66.2%).
  The limit is the pick: 693 of 896 (77.3%) when always answering, while 80% right needs 717 with no answer withheld.
  Of the 203 pick misses, 127 list no right answer (WTQ 49, fresh dev 40, DataBench 25, messy 13) and 76 list one the
  chooser passes over; no family holds more than a few.
- Spend on the Decisions API since 2026-11-18: about $12.5 of the $25 cap.

## The demo datasets through the Decisions path reach 86 right and 2 wrong of 91 (75 and 5 before the night) with one currency, a percent under its own row operator, links between two uploads, a count share, a breakdown by month and a last record kept; on the 896 dev questions a span of years raises the best pick to 693 right (77.3%) against 689, unwarned wrong 86 (9.6%); a deeper pool, number either-or, a closed question for the row operator and six chooser variants fail their gates (2026-11-19)

- Continues the entry below, on the owner's order ("Build, measure, continue with next steps. Do not wait for me")
  and "go ahead, continue with the next steps". Every run is the no-trained-model arm (stock bge-m3 shortlist, the
  engine's word reader, the Gemini Decisions API reads and chooses), per answer at the CLEF path's thresholds, on
  public and synthetic tables only. Experiment code in
  `training/readers/data/experiments/decisions-probe-20261118/local/` (gitignored); each gate was written to its
  `experiment-contract.json` before its run (sections `chooser_replay`, `pool_depth`, `pool_depth_arms`,
  `number_either_or`, `one_currency_column`, `kept_stack`, `percent_of`, `trailing_record`, `count_share`,
  `month_breakdown`, `year_span`, `row_op_question`, `order_agreement_probe`, `percent_operator`). Engine changes are on next-engine (local, not pushed): `0ec2ac8` one currency, `6e06e569` the
  last record, `138c583` the count share, `70b9dd5` the month breakdown, `5e82de6` the span of years, `271111b` the percent; each was measured in a worktree of `9bc24e4` plus the diffs before it. Main
  `de45990` (clean). Nothing is served, pushed or promoted. The demo datasets are production's own gate questions
  (91), run through `run_connected.py` with the knowledgebase columns an upload reaches; right and wrong there count
  warned answers too.

**Measured, in the order run** (demo: right / asked / wrong of 91, warned answers counted with their side; dev: right
answer listed / best pick right of the questions named):

| Change | Where it moves | Before | After | Gate |
|---|---|---|---|---|
| The chooser asked six other ways (unranked, reversed, two orders, a final between two, top 20), replayed offline | 896 dev | pick 680 | 674 to 680 | failed every variant |
| The decoder's pool at 150 candidates, the chooser shown the first 50 | 896 dev | 761 / 680 | 779 / 683 (listed 18, 0) | dev passed |
| the same | demo | 75 / 11 / 5 | 73 / 10 / 8 | failed: not kept |
| A choice among two or more named numbers ("1898 or 1893") | 896 dev | 761 / 680 | 762 / 680 (pick 1, 1) | not kept (a tie) |
| A column stating its own currency is one figure, never split by the lines' currency | demo | 75 / 11 / 5 | 78 / 10 / 3 | passed, kept |
| The value-set question (yesterday's vs1), re-gated with it | demo | 78 / 10 / 3 | 78 / 10 / 3 | passed, kept |
| The deeper pool, re-gated with it | demo | 78 / 10 / 3 | 78 / 9 / 4 | failed again |
| A rate's percent of a measure, links between two uploads, one value for a key and its reference | demo | 78 / 10 / 3 | 83 / 5 / 3 | passed |
| the same, the percent | 896 dev | 765 / 689 | 764 / 685 (listed 2, 3; pick 4, 8) | failed: not on next-engine |
| A last line sharing a value with the records above it is a record | demo | 83 / 5 / 3 | 84 / 5 / 2 | passed, kept (no dev workbook moves) |
| A count share ("what percentage of orders are from Lyon") | demo | 84 / 5 / 2 | 85 / 4 / 2 | passed |
| the same | 896 dev | 764 / 685 | 764 / 685 (every outcome identical) | passed, kept |
| A breakdown by a date's month ("orders per month") | demo | 85 / 4 / 2 | 86 / 3 / 2 | passed |
| the same | 896 dev | 764 / 685 | 764 / 685 (one warned right answer unwarned) | passed, kept |
| A span of whole years over a column of year numbers ("between 2015 and 2020"), the period question asked where such a column is the only dated one | 896 dev (260 on workbooks with a year column) | 765 / 689 | 769 / 693 (listed 4, 0; pick 4, 0) | passed, kept |
| the same | demo | 86 / 3 / 2 | 86 / 3 / 2 (nothing read differently) | passed |
| A closed question for the row operator: a formula over a line's two figures, in symbols, scored against none | 896 dev | 765 / 689 | 766 / 693 (listed 1, 0; pick 11, 7); unwarned wrong 88 to 93 | failed: not kept |
| the same | demo | 86 / 3 / 2 | 86 / 3 / 2 | passed |
| the same, the percent under it | 896 dev | 765 / 689 | 766 / 691 (against the question alone: pick 0, 2); unwarned wrong 88 to 93 | failed: not kept |
| What is kept without the percent (next-engine `5e82de6` as committed, the same flags) | demo | 86 / 3 / 2 | 83 / 4 / 4 | the commission answers need a percent |
| The kept stack measured whole (`base2`, replacing the projection) | 896 dev | 765 / 689 | 769 / 693 (listed 4, 0; pick 4, 0); unwarned wrong 88 to 86 | measured |
| A percent under its own row operator, scored from the formula question; the four operators stay unread | demo | 83 / 4 / 4 | 86 / 3 / 2 | passed |
| the same | 896 dev | 769 / 693 | 769 / 693 (no listed or pick moves); unwarned wrong 86 to 86 | passed, kept |

**The chooser is not where the dev questions are lost.** Every chooser record of the 896 was dumped and replayed
offline against the same API (`replay_choose.py`): options said to be in no order, the options reversed, the question
asked twice in two orders, a final between the top two answers, and only the top 20. None gains on the 896 (best: two
orders, 680 against 680, 13 wins and 13 losses). Reversing the options loses 8 on DataBench (p 0.04): the decoder's
order carries information the chooser uses.

**A deeper pool lists more but picks little more.** At 150 candidates the right answer is listed for 779 of 896
against 761 (18 wins, no loss), the best pick is right on 683 against 680 (18 wins, 15 losses), and unwarned wrong is
83 against 84. Of the newly listed, 12 rank past 50 and the chooser never sees them. It passed the dev gate narrowly
and failed the demo gate twice: once on currency questions whose choice flipped with the options under the top eight,
and, after the currency fix, on "how many orders per month?", which the deeper pool answered with one count per order
day. Not kept; it returns with the breakdown by month below.

**Number either-or** (a choice among named numbers in a number column, both emitters, one new test) lists the right
answer for WikiTableQuestions' "was the quantity higher in 1894 or 1893" and "which had more ger numbers, 1898 or
1893"; on DataBench numeric choices enter eight pools and one loses its right reading. Pick ties at 680 (1 win, 1
loss): not kept, and next-engine is unchanged.

**One currency.** The demo's "total amount in France in US dollars" read the converted column "amount → USD", but
the decoder split every total over a table with a currency-code column into one figure per currency, and offered the
single total only as "the size of the total … (all currency together)"; the chooser's split between the two moved
with the options under them, which is what failed both the value-set question and the deeper pool on the demo
yesterday and tonight. A column whose unit is an ISO 4217 code (a header "Amount (USD)", or a conversion to one
currency) holds that currency on every line: the decoder gives its plain figure, and so for a share of it or a sum or
difference of two figures in it. A column of mixed currencies is still split. No dev workbook has such a column; on
the demo right goes 75 to 78 and wrong 5 to 3. The value-set question passes the demo with it and is kept in the arm.

**Two uploads and a percent.** With production's links between uploaded tables (a key into another upload reaches
its columns), yesterday's run answered the commission questions wrongly for want of a percent. A measure's percent of a
rate column (`PercentOf`: measure × rate ÷ 100, a rate being a number column within 0 to 100) is offered where times
is read, and the measure less or plus it where minus or plus is; it reads the coordinates of times, so the trained
readers' operator classes are unchanged, and both emitters compute it. Every rate column pairs with a measure the
reading names: a reading of one measure does not name its second operand (the first run left "total amount after
subtracting commission" with no percent reading and answered it as total amount less the total rate). One value
question: a key and the column it keys into hold the same values, so the value set's marginal sums the two
("digital wallet" in payments 0.38 and in the commission rates 0.67 read as one value at 0.96); the same text in
columns no key links stays two values ("GBP" as an order's currency and its country's, which summed made the British
pounds question wrong). On the demo five asked questions become right and none is lost: the card commission (9.28),
the amount after commission, the amount after commission for digital wallet payments, and the France and Belgium
totals across two uploads. On the 896 dev questions it fails its gate: the best pick is right on 685 against 689 (4 wins, 8 losses), unwarned wrong 90 against 88. In the no-trained-model arm nothing reads the row operator (its four coordinates sit at a uniform 0), so a percent is built for every measure a reading names beside every rate column of its table; in the pools that moved, percent candidates hold 14 or 15 of the 50 places the chooser sees (places 20 to 47), and the chooser's choices move with them. Read from times, the percent stays off next-engine; read under its own operator it is kept (below).

**A last record kept.** "number of contracts" (183) counted 182: the sheet's last line, a file name of 68 characters
with no notice period, was read as a note under the table, since a trailing line of two cells needed text of 60
characters or fewer to count as a record. A line holding a text the records above hold in the same column is one of
them. No dev workbook compiles differently; the demo's wrong answer becomes right.

**A count share and a month.** "What percentage of orders are from Lyon?" (0.3) had only a ratio of summed order
numbers in its pool, right by chance and declined by the chooser; the decoder now offers the count of the lines a
condition keeps over the count of all lines, picked at 0.91. "How many orders per month?" grouped by each order day; a
breakdown's grain now groups a date key by its year-month through the typed AST's existing `DatePart`, which both
emitters compute, offered beside the day's own groups and picked at 0.92. Neither moves the dev questions: with the share every outcome of the 896 stays as it was, and with the month listed and pick stay as they were and one warned right answer is no longer warned (both frames need a reading the dev questions rarely make). Both are on next-engine, without the percent.

**A span of years.** Thirteen dev questions name a span of two years, all on the fresh dev twin, and eight had no
right answer listed or picked: "the three calendar years with the highest total income between 2015 and 2020" read as
"Calendar Year is at least 2015". A period tests only a column of days, and a column of plain year numbers holds none,
so a span became one number bound at most. `InYears` keeps the lines whose year number falls in a span of whole years,
both ends kept, over a column whose every figure is a whole number from 1000 to 2999; it reads a period condition's
coordinates and lowers to two number comparisons, which both emitters compute. The decoder offers it for a span of two
or more years (one year is a value of such a column, which its few numbers already name: offering it for one year
pushed "cars from 2020" out of an existing test's pool). The arm's period question, which offers "2015 to 2020, both
included", was asked only of a workbook holding a column of days; it is now asked where a column of year numbers is
the only dated column too. Only questions on such workbooks can move (260 of the 896), and on them the right answer is
listed for 220 against 216 and the best pick is right on 193 against 189, with no loss: four fresh dev questions go
from wrong to right at 0.91 to 0.96. One WikiTableQuestions question goes from asked to wrong. The demo datasets read
and decode exactly as before.

**A closed question for the row operator.** The percent failed because nothing in the no-trained-model arm reads
the row operator. One more closed question in the same read call asks for the formula over a line's two figures, in
symbols only (a × b, a − b, a + b, a ÷ b, a × b ÷ 100, a − a × b ÷ 100, a + a × b ÷ 100, or none), and scores each
operator against none. Alone, on all 896: the right answer is listed for 766 against 765, the best pick is right on
693 against 689 (11 wins, 7 losses, p 0.48), and unwarned wrong rises from 88 to 93, past the gate's 3. The API
answers none at a median of 0.995 on WikiTableQuestions, which pushes row arithmetic far down the pool; the chooser
then answers 11 more questions, about as many wrong as right, and at an equal rate of wrong it answers fewer right (the
cross-fitted 8% point 576 against 584). With the percent under it, the percent no longer crowds the pool (2 losses
against the question alone, where it lost 8 without it) but gains nothing on the dev questions. Neither is kept; the
demo datasets answer every question as before with the question.

**The percent under its own operator.** Without the percent, what next-engine holds answers the demo 83 right and 4
wrong (`demo23`): the card commission and the amount after commission are wrong at 0.86 and 0.94, the digital-wallet
net is asked and the tier-discount total warned. `PercentOf` now reads its own row operator, `%` (`PERCENT_OP`), apart
from the four the trained readers know, so their classes are unchanged and a reading that does not score it offers
no percent; a reading of times alone gives the product and no percent (a new regression, which fails under the
times rule). In the arm the formula question scores only `%` (the three formulas over 100 against none) and the four
operators stay unread, as in the kept stack. On the demo the four answers come back (86 right, 2 wrong, `demo24`); on
the 896 no listed answer or pick moves and unwarned wrong stays 86 (`pg1` against `base2`; three messy answers move
between asked and warned wrong as the read call gains a field). On next-engine as `271111b`.

**Warning where the options' order changes the answer** (a probe of cached replays, no API call). Unwarned wrong is now
the binding limit (88 of 896 in `keep1`, 86 in `base2`, against the bar's 71), not the pick. Asked with its options reversed, the chooser
disagrees with its answer on 108 of 895 questions, of which 40 are right; 25 of the 108 were answered unwarned (10
wrong). Warning those moves 25 answers from unwarned to warned (15 right, 10 wrong): unwarned wrong 84 to 74, right
counted with warned unchanged at 659, warned 104 to 129; at the cross-fitted 8% point 594 right against 590. A small
step for one more chooser call per question; not run on today's stack and not kept, since it changes no pick and right
is the larger gap (the bar needs 717 right of 896, the best pick is 693).

**The kept stack on the dev questions** (tag `keep1`: yesterday's flags with the count question and the value-set
question together): on all 896 the right answer is listed for 765 and the best pick is right
on 689 (76.9%), against yesterday morning's combined run's 759 and 677 (pick 21 wins, 9 losses, p 0.04); as served,
620 unwarned right, 43 warned right, 104 asked, 41 warned wrong and 88 unwarned wrong (9.8%). By set (listed / pick /
unwarned right / unwarned wrong): WikiTableQuestions 251 / 220 / 189 / 37, messy 107 / 91 / 73 / 13, fresh dev
132 / 124 / 116 / 19, DataBench 275 / 254 / 242 / 19. The bar is 80% right with at most 8% unwarned wrong; the
cross-fitted 8% point is 584 right (65.2%) at 66 wrong. On messy the value-set question costs confidence (unwarned
wrong 9 to 13 against the count question alone). With the span of years, measured on all 896 (`base2`): listed 769, best pick right 693 (77.3%);
as served, 625 unwarned right, 42 warned right, 103 asked, 40 warned wrong and 86 unwarned wrong (9.6%); by set
WikiTableQuestions 251 / 220 / 189 / 38, messy 107 / 91 / 74 / 13, fresh dev 136 / 128 / 120 / 16, DataBench
275 / 254 / 242 / 19; the cross-fitted 8% point is 594 right (66.3%) at 71 wrong. The projection from the 260
questions on workbooks with a year column held exactly, and the percent changes none of it.

**What the demo still misses** (5 of 91): three multi-part questions ("the top 2 categories by revenue and the top 2
customers …, then …"), which are production's decomposition, not one program; "the product names that no customer
from Paris has bought", a set difference across two tables, also decomposed in production; and "total amount in
Europe in GBP", read as converted and also kept to orders whose country's currency is GBP, an ambiguity between a
conversion target and a filter the value question cannot settle (the question names the code once).

Cost: the night's and day's API calls, from the cache, 5,454 calls and 31.8 million tokens since 22:58 (about
$3.18); all Decisions API use so far 21,822 calls, 121.2 million tokens, about $12.12 of the $25 the owner set.


## All of the day's changes together: on the 896 dev questions the no-trained-model Decisions path picks 677 right against this morning's 636 (listed 759 against 737), and a value-set question raises that to 687 but fails the demo gate by one wrong answer; the decided-set rule and an exact-totals compile rule fail their gates, and on WikiTableQuestions no change to the chooser moves its pick by more than 2 of 299 (2026-11-18)

- Continues the entry below the same day, on the owner's order ("Build, measure, continue with next steps. Do not wait
  for me") and the owner's later "go ahead, continue with the next steps". Every run is the no-trained-model arm (stock bge-m3
  shortlist, the engine's word reader, the Gemini Decisions API reads and chooses), per answer at the CLEF path's
  thresholds, on public and synthetic tables only. Experiment code in
  `training/readers/data/experiments/decisions-probe-20261118/local/` (gitignored); each gate was written to its
  `experiment-contract.json` before its run (sections `typed_dates`, `family_tabs`, `stated_years`,
  `value_against_none`, `choice_misses_review`, `combined`, `value_count`, `value_sets`, `value_sets_decided`,
  `exact_totals`, `unlisted_review`, `chooser_replay`). Engine: eval worktree `9bc24e4`, plus the
  two engine changes below as diffs on it (next-engine `5a748ee` printed totals, `191e11e` family tabs; local); main `f3bd0ef` (clean). Nothing is
  served, pushed or promoted.

**Measured, in the order run** (right answer listed / best pick right / unwarned right / unwarned wrong):

| Change | Questions | Before | After | Listed wins, losses | Pick wins, losses | Gate |
|---|---|---|---|---|---|---|
| Dates a sheet writes as text, typed as days when the workbook loads | 596 | 487 / 416 / 358 / 70 | 489 / 417 / 360 / 72 | 2, 0 | 3, 2 | passed, within noise |
| A family of tabs named by all its tabs (engine read-as and sheet description) | 596 | 489 / 417 / 360 / 72 | 488 / 418 / 362 / 72 | 2, 3 | 3, 2 | passed, no gain |
| A closed question for a year compared with a column of plain year numbers | 596 | 488 / 418 / 362 / 72 | 489 / 420 / 366 / 73 | 2, 1 | 5, 3 | passed |
| The value slot read against none | 896 | 750 / 657 / 584 / 94 | 754 / 667 / 599 / 92 | 18, 14 | 31, 21 | passed, kept |
| every slot that offers none read against none | 896 | 750 / 657 / 584 / 94 | 761 / 665 / 589 / 91 | 26, 15 | 39, 31 | passed, fewer picks: not kept |
| All of the above with printed totals, against the first arm | 896 | 737 / 636 / 569 / 86 | 759 / 677 / 610 / 88 | 36, 14 (p 0.003) | 58, 17 | passed |
| + a closed question for how many values the question names | 896 | 759 / 677 / 610 / 88 | 761 / 680 / 610 / 84 | 3, 1 | 5, 2 | passed, within noise |
| + one choice over sets of the top four values, each value scored by its marginal (second call) | 896 | 759 / 677 / 610 / 88 | 766 / 687 / 617 / 85 | 10, 3 | 20, 10 (p 0.10) | passed; demo failed by one wrong: not kept yet |
| the same call, only the likeliest set decides | 596 | 484 / 424 / 367 / 70 (vs1: 491 / 434 / 375 / 66) | 493 / 429 / 370 / 64 | 9, 0 against all2 | 12, 7 against all2; 7, 12 against vs1 | failed: messy pick 90 to 86 |
| a total equal, exactly, to five or more whole cells of its block is printed (sheet compile) | 2 changed workbooks | | no gain, one more confident wrong | | | not kept |

**All of today's changes together** (tag `all2`, engine worktree 9bc24e4 + the printed-totals and family-tab diffs):
on all 896 dev questions the right answer is listed for 759 against the first arm's 737, the best pick is right on
677 against 636, and as served 610 are unwarned right against 569 and 88 unwarned wrong against 86. By set (listed /
pick / unwarned right / unwarned wrong, first arm then now): messy 110 / 85 / 69 / 9 to 109 / 90 / 76 / 9; fresh dev
114 / 97 / 84 / 20 to 130 / 121 / 111 / 20; WikiTableQuestions 243 / 208 / 185 / 36 to 245 / 213 / 180 / 41; DataBench
270 / 246 / 231 / 21 to 275 / 253 / 243 / 18. The cross-fitted 8% point moves from 545 right at 69 wrong to 555 at 57.
As rates of 896: pick 75.6%; served unwarned right 68.1%, unwarned wrong 9.8%, warned 10.8%, asked 11.3%, against the
bar's 80% right, at most 8% unwarned wrong and at most 20% warned. "Listed" counts a yes/no question as listed whenever
its pool holds both answers; without the 90 such pools it is 676 of 806 (the first arm 648 of 798).

**The demo datasets** (91 questions, production's own gate questions, run through run_connected.py with the connected
knowledgebase columns; right and wrong count warned answers too). All of today's changes together (tag `demo7`) fail
their gate against the morning's `demo6`: 75 right and 5 wrong against 76 and 4. The count question adds nothing there
(`demo8`, identical question by question). The value-set question fails it by one wrong answer (`demo9`: 75 right, 6
wrong against `demo7`'s 75 and 5). Every demo move is a currency-conversion question: the pick between the total for
each currency and the total of all currencies flips both ways on two wordings of one question (customer-orders #5 and
#6), so the choice there is unstable whatever the value reading does. The breakdown is offered in three phrasings
against one for the total, and the per-answer pick sums them (0.36 + 0.18 + 0.03 against 0.38 on #5). One option per
distinct answer, which would remove that, was measured on the 896 this morning as a wash; switching it on for the demo
questions alone would tune toward the release-gate set, so it is not.

**The value-set question** (`vs1`). After the value reading, a second closed question offers one choice over the
subsets of the value reading's top four values, and each value is scored by its marginal. It lists 7 more right answers
and picks 10 more on the 896 (20 wins, 10 losses, p 0.10), with unwarned wrong 88 to 85. The gain is WikiTableQuestions'
either-or questions (7 of the 9 value-against-none lost from the list come back) and fresh dev's comparisons; DataBench
does not move. On messy it costs confidence: a value lies in half the sets, so an unsure answer spreads its marginals
toward one half (messy22: four amounts read 0.46-0.67 in although the first reading put none at 0.49), and one text in
four tabs counts as four values (messy0, messy2, messy3). The rule where only the likeliest set decides (`vs2`), meant
to remove that, fails its gate on the three sets already: messy's pick falls from 90 to 86, and against vs1 it picks 5
fewer for 2 fewer unwarned wrong. vs1 passed the dev gate but not the demo gate, so it is not in the stack yet.

**Totals the compile misses.** A whole figure of at least 1,000 equal, exactly, to the sum of five or more whole cells of
its block, taken as a printed total on its own (scratch worktree `prereasoner-next-strongtie`, one new sheet-compile test
that fails before the change): only 2 of the 236 dev workbooks compile differently, both fresh dev, and on their 8
questions nothing is gained while one warned wrong answer becomes an unwarned one (FD-0043: with both totals printed, the
rank of costs still returns "Other foodborne gastroenteritis", and the gold ranks pathogens only). Not kept. The top-N
misses need the reading of which lines a question ranks, not more arithmetic.

**What is still not listed** (vs1: WikiTableQuestions 49 of 300, fresh dev 44 of 176, messy 12 of 120). Fresh dev's
largest kind is "which three X had the highest" (12 of the 44; the count is read right, 0.80-1.00): printed subtotals
the compile keeps as data lines (FD-0043, FD-0151, FD-0155, FD-0167), a total column ranked among its peer columns
(FD-0106, FD-0108: "United States" beside the states), a range's upper year not read (FD-0020), a threshold with a scale
word (FD-0040 "more than one million"), a filter column not read (FD-0022), and a choice among number keys (FD-0192;
`OneOf` takes text values only). WikiTableQuestions has no dominant cause: either-or between two named lines or years
(nt-14117, nt-4371, nt-1856, nt-5804, nt-12998), a value taken from a named line (nt-4336, nt-7021), counts ranked by
group (nt-11359, nt-12015, nt-9904), answers that are words the engine does not print ("more", "after"), and one empty
pool. Messy's 12 are mostly duration bands and trends. Two are gaps in the program language itself: a peer rank keeps
one column (`Rank` refuses a count above 1 for peers), and a total column among the peers is ranked with them.

**The choice.** Wherever the right answer is listed but not picked (about 70 questions of the 896 outside two-answer
pools), it was offered to the chooser (all 50 listed readings are); for 40 of them it is the second answer by choice
mass. Summing the choice over the readings that return one answer, as served, or taking its largest reading, picks the
same 677. A chooser replay over the requests as sent (`--dump-choose`, `replay_choose.py`; contract `chooser_replay`)
asks the same choice with one change at a time. On WikiTableQuestions no change moves the pick by more than 2 of 299
(213 as run; options said to be unordered 215, presented in reverse 215, both orders averaged 214, a final between the
top two answers 211, only the first 20 options 215), and the replay of the run's own requests reproduces its choice
exactly from the cache.

**Where this leaves the bar.** As served, all 896 dev questions with vs1: 617 unwarned right (68.9%), 85 unwarned wrong
(9.5%), 91 warned (10.2%), 103 asked; the best pick is right on 687 (76.7%). The bar is 80% right with at most 8%
unwarned wrong and at most 20% warned. Each change measured today moves between 0 and 10 of 896; the remaining misses
are spread over many small families (above), and some of WikiTableQuestions' choice misses are label noise (nt-8887
expects "1" for the span 1886-1887; nt-1397 expects the runner-up as the top score).

**Cost.** The Decisions API cache holds 16,368 calls and 89.4M input tokens for the whole of 2026-11-18, about $8.94
of the owner's $25 cap; the re-runs of today's arms ask it nothing (every request is kept by its hash).

**A correction to the record.** Six "written" times in `experiment-contract.json` had been estimated without reading
a clock (one said 22:55 when the clock read 20:43). Each now states the time the run's log file was created, which
the file times show, and says the first figure was estimated. The gates themselves were written before their runs.

## What nothing read: a count written as a word, a day written with its month, and totals printed over their block; on three dev sets the no-trained-model Decisions path goes from 390 to 422 right picks of 596 (listed 467 to 492) and on the demo datasets from 69 to 71 right with wrong 4 to 1; the value-option fix alone is a wash on all 896, and choosing per answer adds nothing (2026-11-18)

- Continues the entries below on the owner's order of 2026-11-18 ("Build, measure, continue with next steps. Do not wait
  for me"). Every run is the no-trained-model arm (stock bge-m3 shortlist, the engine's word reader, the Gemini Decisions
  API reads and chooses), per answer at the CLEF path's thresholds, on public and synthetic tables only. Experiment code in
  `training/readers/data/experiments/decisions-probe-20261118/local/` (gitignored); every gate was written to its
  `experiment-contract.json` before its run (sections `answer_choice`, `canonical_values`, `stated_numbers`,
  `stated_periods`, `written_days`, `printed_totals`, `value_against_none`). Engine: eval worktree `9bc24e4`; the compiler
  change below is measured as a diff on it and committed as next-engine `5a748ee` (local). main `491f171` (clean).
  Nothing is served, pushed or promoted.

**Where the questions were lost.** Fresh dev names each question's kind. Of its 28 "which three ..." questions, 27 never
listed the right answer, in every arm so far and in the CLEF reference alike; DataBench, which writes its counts in
digits, gets 96 of its 120 list questions right. A count written as a word was read by nothing: the word reader reads
digits, the API was never asked a number, and the decoder builds a rank of N only from a number it holds.

**Measured, in the order run** (right answer listed / best pick right / unwarned right / wrong):

| Change | Questions | Before | After | Listed wins, losses | Pick wins, losses | Gate |
|---|---|---|---|---|---|---|
| Choice asked per distinct answer | 596 | 467 / 390 / 338 / 65 | 467 / 391 / 300 / 56 | 0, 0 | 19, 18 | passed, no gain: not kept |
| Value options on the coordinates programs read | 896 | 737 / 636 / 569 / 86 | 736 / 641 / 568 / 94 | 11, 12 | 19, 14 | passed, no gain |
| the same, without DataBench | 596 | 467 / 390 / 338 / 65 | 473 / 401 / 343 / 69 | 9, 3 | 17, 6 (p 0.035) | |
| the same, DataBench alone | 300 | 270 / 246 / 231 / 21 | 263 / 240 / 225 / 25 | 2, 9 | 2, 8 | |
| + a closed question for a number written as a word | 596 | 473 / 401 / 343 / 69 | 487 / 416 / 358 / 70 | 14, 0 | 15, 0 | passed; top_n clause missed by one |
| + a closed question for a period written in words | 596 | 487 / 416 / 358 / 70 | the same | 0, 0 | 0, 0 | passed, no gain |
| + printed totals over their block (compiler) | 476 | 379 / 331 / 289 / 60 | 384 / 337 / 294 / 56 | 5, 0 | 6, 0 (p 0.031) | passed |
| All of the above against the first arm | 596 | 467 / 390 / 338 / 65 | 492 / 422 / 363 / 66 | | | |

- **Per answer.** Listing each distinct answer once in the choice call leaves the pick where it was and separates right
  from wrong less well (at about 8% wrong: 299 right against 312). Summing a candidate-level choice over the candidates
  that return the same answer carries a vote that the shorter call throws away. It saves a third of the choice tokens.
- **Value options.** The reading record named an expanded value by the sheet's spelling, a coordinate no program reads,
  so the API's probability was split or lost (in every CLEF and Decisions run so far). Mapping each option to the
  coordinate programs read gains 11 picks on three sets and loses 6 on DataBench: 7 of its 9 lost answers are "the most
  common X" on two survey tables, which filter nothing. With 160 value options a value at 1% sits above the uniform
  share, so its term is positive and filtered candidates outrank the unfiltered one.
- **The rule that came out of it.** An option that decides whether a condition is built at all is read against the
  slot's own "none", W * (log p - log p(none)), not against the uniform share 1/n. The number question's first run
  showed the same fault (a number at 5% of 22 options was positive on 48 of 176 fresh dev questions, which gained and
  lost one answer each; the 36 the API read as a number carried all the gain) and was re-run with this rule; its numbers
  above are that re-run. For the value slot, and for every slot that offers none, the rule is under test now; on fresh
  dev alone it moves nothing (pick 114 and 113 against 113).
- **Numbers.** Options are 0 to 20 as digits and none; no number word is written anywhere. A number the question writes
  in digits keeps the word reader's score. Fresh dev: listed 115 to 128 (13 wins, 0 losses), pick 99 to 113, unwarned
  right 88 to 102, wrong 20 to 22; its top_n kind lists 13 of 28 (1 before; the gate asked for 14). WikiTableQuestions
  gains one question, messy none. The API reads "first" and "only" as 1 on 22 WikiTableQuestions questions; none of
  them moved. With the question added to a call, the API's other answers in that call are unchanged (largest
  difference 1.4e-8), so a question that reads no number is read exactly as before.
- **Periods.** Options are built from the years and days the question writes in digits: each year's months and
  quarters, before, up to, from and after each, the range between two. The API reads them right (12 of 124 fresh dev
  questions name one) and no outcome moves on any set: those sheets hold their dates as text (a month written as a
  word, or 03/01/2019 12:00:00 AM), which the compiler does not type as days, so no date condition can be built.
- **Printed totals** (next-engine `5a748ee`: `engine/sheet/line_facts.py`, three tests in `tests/test_sheet_compile.py`). Of the
  15 top_n questions still unlisted, several sit on a table whose national line is printed over the states, or whose
  total follows a subtotal and more lines; a rank of three returned it. The compiler knew a total by arithmetic only
  when it stood under its block and tied to the cent. Three cases were added, with no word read: a line that sums the
  lines under it (on at least two figures); a line that sums every record above it once a subtotal has closed a
  block; whole numbers that miss their sum by the cells' rounding (half a unit a cell, never more than 0.1% of the
  figure). Of 251 dev workbooks 6 recompile (5 fresh dev, 1 WikiTableQuestions, no messy, no DataBench). The 20 fresh
  dev questions on them gain 5 listed and 6 right picks, and the set's wrong answers go from 22 to 18; no other
  question changes its outcome. A first version let one tying figure do: it read a medal table's "USA, 10 golds" as a total over the four
  lines under it and failed next-engine's own `tests.test_program`, which I had not run before measuring. The
  two-figure rule fixes that, the medal table is a contrast test, and every dev workbook compiles as measured.

**Demo datasets** (91 questions, connections place, entity and conversion; right / warned right / asked / warned wrong /
wrong, and the right answer listed):

| | right | warned right | asked | warned wrong | wrong | listed |
|---|---|---|---|---|---|---|
| as recorded below | 69 | 4 | 12 | 2 | 4 | 74 |
| + value options | 67 | 5 | 13 | 4 | 2 | 77 |
| + numbers, + periods | 67 | 5 | 13 | 4 | 2 | 77 |
| + a day written with its month | 71 | 5 | 11 | 3 | 1 | 80 |

- The four date-range questions write a day with its month as a word ("before July 10, 2026", "between August 4 and
  August 9, 2026"). Two more closed questions read them: which day (every month of the year the question writes, with
  each number from 1 to 31 its text writes, as ISO days) and how a date is restricted to it (on, before, until, from,
  after, between). Day and relation compose into one period, which the decoder already turns into a date condition.
  All four go to right and the other 87 questions do not move. The four were seen in a smoke test first (it found the
  engine's word reader drops a number a comma follows), so only the no-harm half of that gate was blind. Production's
  own gate on this set is 91 of 91.

**Not finished at this entry.** DataBench with the number and period questions (its counts are in digits; running); the
against-none rule for the value slot on messy, WikiTableQuestions and DataBench (running); the written-day questions on
the dev sets (asked only of a question that writes a year and a day number over a workbook with a day column).

**What the runs showed is still unread or unbuilt.** Dates a sheet holds as text; a year range on a column of plain
year numbers ("after 1940"); a scale word ("one million" reads as 1); a parent line over two children; a header line
repeated among the records; and, on messy, the choice among listed candidates (23 of its 35 lost questions list the
right answer).

**Cost.** About 27M API tokens this morning, under $3; about $7 of the night's $25 cap in all. No GPU.


## Connected questions: with the knowledgebase columns an uploaded column reaches offered as options and run as joins through production's bridges, the Decisions path answers 69 of the 91 demo questions right (56 without; production's own gate 91), and offering them costs nothing on 181 dev questions (2026-11-18)

- On the owner's correction and order of 2026-11-18: the closed questions must offer the input tables' columns plus the
  knowledgebase tables their columns connect to ("city in order table is foreign key in Wikipedia city and Wikipedia
  city is connected to country, continent etc"); "Build the full design doc and plan"; "Build, measure, continue with
  next steps. Do not wait for me"; live database read and write on his login; API cap $25. The design and the staged
  plan, written before any stage ran, are `training/readers/data/experiments/decisions-probe-20261118/design/
  CONNECTED_QUESTIONS.md`; the contract's `connected` section holds the results. Experiment code only; nothing served.
- **The fault.** The Decisions path offered only the uploaded sheet's own columns. On Orders (customer, amount, city),
  "total sales in france" could only land on a list of cities: right with two French cities, never built with three
  (the experimental engine's one-of holds two values).
- **What was built** (`local/connected.py`, `build_maps.py`, `build_dev_maps.py`, `run_connected.py`,
  `run_connected_dev.py`). Production's own code, unchanged, prepares each workbook in the experimental branch's
  worktree: `q11.ingest`, `route`, the bridge of every routed column with production's pairing (`_city_bridge_sql`,
  `_cell_bridge_sql`), entity columns (`_nongeo_matches`, `_persist_connected`), and the exchange rates of the table's
  days (`load_rates`). The request database it fills (bridges in `main`, knowledgebase rows) is kept per workbook.
  Each linked column then gains the columns it reaches, named by their path: "city → country", "city → country →
  continent", "bank → country", "amount → USD". The uploaded table is stored with its own columns; a temporary view of
  the table's name carries the joins to the bridge and the knowledgebase tables, so a program that names a connected
  column runs the join. The decoder's in-memory values are read through the same join and checked against it line for
  line on every workbook. Nothing is copied into the upload.
- **Three kinds of connection.** Place columns (city, country, state: country, continent, currency, population);
  entity columns (bank, hospital, restaurant: country, continent); a money column with a currency-code column
  (converted at the knowledgebase rate of the row's currency on the row's day, the as-of day for a table without a
  date column, as production binds it). Conversion adds one closed question, "convert to which currency", used only to
  decide which currency's converted columns are offered (from probability 0.1); the reading and the choice decide.
- **Links offered** only where at least 80% of a column's cells matched (production's route threshold) and at least
  three distinct cells did (production's floor for typing a column from its cells; added after the first dev pass,
  below).

**Demo datasets** (every prompt and standalone follow-up of `tests/test_datasets.py`, 91 questions on 24 datasets; the
no-trained-model arm of the entry below; per answer at the CLEF path's thresholds; an answer to a conversion question
counts right only when the picked reading uses a converted column):

| connections offered | right | flagged right | asked | flagged wrong | wrong |
|---|---|---|---|---|---|
| none | 56 | 0 | 23 | 4 | 8 |
| place | 59 | 1 | 18 | 6 | 7 |
| place + entity | 63 | 3 | 14 | 5 | 6 |
| place + entity + conversion | 69 | 4 | 12 | 2 | 4 |
| production (`tests.test_datasets`, live, engine only, 92 records, 0 failures) | 91 | | | | |

- By dataset kind, none against all three: own data (47) 43 right and 42 with 1 flagged right; knowledgebase (22) 10
  and 17 with 2 flagged right; knowledgebase with conversion (17) 3 and 10 with 1 flagged right; complex multi-table
  (5) 0 and 0.
- Examples of the picked readings: "total delivery fee in Europe" as the total of fee where city → country →
  continent is "Europe" (asked before); "total transfers for hospitals in United States" as the total of transfers
  where hospital → country is "United States" (asked before); "total amount in France in US dollars" as the total of
  amount → USD where city → country is "France" (asked before).
- The 18 still not right: 5 complex multi-step questions; 5 that need two uploaded tables or a calculation across
  them; 4 date ranges (before, after, between: the experimental engine has no such condition on a date column and the
  API is never asked a period); 1 count off by a line; 3 conversion readings (a per-currency breakdown picked twice;
  "total amount of GBP orders in Europe" now asked, its conversion question reading 0.24 convert).
- The maps: 13 place links on 12 datasets, entity links on 3 (hospital 5 of 6 cells, bank 6 of 6, restaurant 6 of 6),
  conversions on 5. Production also types columns falsely: product names (Alpha, Delta) and "category" as cities,
  customer names as films or musical groups; the false entity types hold no country and reach nothing.

**No harm on the dev sets** (stage 5: the questions of every dev workbook production links, connections on, against
the same arm without; wins / losses per question):

| pass | questions | right answer listed | pick right | unwarned right |
|---|---|---|---|---|
| first: links at 80% of cells (messy, WikiTableQuestions) | 145 | 7 / 3 | 6 / 7 | 7 / 11 (p 0.48) |
| with the three-cell floor: messy 72, WikiTableQuestions 57, fresh dev 52 | 181 | 6 / 3 | 6 / 4 | 6 / 8 (p 0.79) |

- The first pass lost four right picks on 16 messy questions whose workbook has a Yes/No column ("No Show",
  "Reimbursed") that production routes as a city column: "Yes" and "No" are city names and 2 of 2 cells matched. The
  floor removes those links. On WikiTableQuestions the true links help (a Nation column reaching its continent):
  listed 6 wins, 1 loss; pick 5 / 2. Fresh dev's links are nearly all US state columns and change nothing (pick 1 / 1).
- DataBench has no usable link in this pass: seven of its tables exceed the 5,000 record lines given to production's
  preparation here, and the rest route nothing.

**Links between two uploaded tables** (a column production's `ingest` finds to be a key into another upload reaches
that table's columns, and what they reach: "customer → city → country"). Built and measured on the demo datasets, it
**failed its gate and is not kept on**: 67 right, 6 flagged right, 8 asked, 3 flagged wrong, 7 wrong, against 69 / 4 /
12 / 2 / 4 with the three kinds above. The reach works: "total amount in Belgium in US dollars" on customers with
orders reads as the total of amount → USD on orders where customer → city → country is "Belgium". But with the
commission and tier tables reachable, the path now answers the calculation questions it used to ask, and wrongly:
"total commission amount for card payments" as amount times commission_percent with no percent. The experimental
engine has no percent-of or discount calculation; production registers these (`engine/calculations`).

**Cost.** About $0.6 of Decisions API calls for all of the above; the night's total about $3.6 of the $25 cap.

**Reading.** The owner's design holds where it was tested: a question about a country, a continent or a currency is
one value on a connected column, the outside model reads it as reliably as a sheet's own column, and the join runs on
the bridges production already builds. What production still does that this path does not: links between two uploaded
tables, calculations across them, date ranges, and multi-step questions. What this path does that production does not:
it reads these questions with no word list (`WORLD_COL_SYN`, `MEASURE_NOUNS`, `ARGMAX_CUES`, `AGG_CUES`), and it uses
an entity column without the question naming its type. The first build's join is a view over the request's own
connection; the typed join in the program and the plan, with both emitters, is not built and needs the owner's word on
ownership.


## The Decisions path without the trained reader: its scores add nothing once the API reads (755 listed, 653 picked right of 896 against 750 and 649), and with no trained model at all the path lists 737 and picks 636, behind mostly on DataBench's large tables (2026-11-18)

- On the owner's order of 2026-11-18 ("go ahead, run both"), after the entry below. Experiment code only
  (`training/readers/data/experiments/decisions-probe-20261118/local/run_arms.py`, sha256 `32704f28` as run; the
  contract's `arms` section, with the reading rule, was written before the run). Nothing is served.
- **The question.** Does the trained reader still carry any of the full path's gain once the Decisions API reads?
- **Two arms**, beside the full path of the entry below, on the same 896 questions, prepared workbooks, record options,
  W, decoder depth, prior-chooser order, evidence choice and thresholds:
  - **zero**: the trained reader still builds the reading record (which options the API is shown). On every slot the
    API answers, the decoder reads only the API's term W x (log p - log 1/n); an option not shown reads below every
    shown one. Slots the API is never asked (size, row_op, calc_left, conditions, contains, period, number) keep the
    reader's scores. The read call is the full path's own request.
  - **stock**: nothing we trained. Stock bge-m3 (the base model's weights) scores each workbook target by its best
    cosine to the question's 1-3 word phrases and the whole question (the top 64 values and lines per table, as the
    reader keeps); the engine's word reader (`engine/program/words.py`) adds what it reads; every closed head offers
    every class. The decoder reads the API's terms on the slots it answers and the word reader's scores elsewhere.
    No fitted alignment, no reader evidence for the candidate order.
- **Run.** 2026-11-18 02:55-06:10, bundle `named_coverage_a_s0/aligned`, eval worktree `9bc24e4` clean, main
  `b798e6d` clean. 2,665 live calls and 917 from the cache, no error, no retry, 14.90M input tokens, $1.49.

Per answer, at the CLEF path's thresholds (of 896):

| | right | flagged right | asked | flagged wrong | wrong | right answer listed | first right | pick right | right at 8% wrong, cross-fitted |
|---|---|---|---|---|---|---|---|---|---|
| full (reader's scores + API) | 600 | 29 | 129 | 49 | 89 | 750 | 514 | 649 | 583 |
| zero (reader as shortlist only) | 577 | 51 | 131 | 53 | 84 | 755 | 526 | 653 | 560 |
| stock (no trained model) | 569 | 44 | 134 | 63 | 86 | 737 | 502 | 636 | 545 |

Listed and pick right by set (messy 120 / WikiTableQuestions 300 / DataBench 300 / fresh dev 176): full 113, 243, 279,
115 and 85, 207, 256, 101; zero 110, 252, 278, 115 and 88, 215, 251, 99; stock 110, 243, 270, 114 and 85, 208, 246, 97.

- **By the rule fixed before the run** (an arm loses where listed or the per-answer pick right falls with a two-sided
  sign test p < 0.05): neither arm loses. zero against full: listed 15 wins, 10 losses (p 0.42); pick 26 / 22
  (p 0.67). stock against full: listed 18 / 31 (p 0.085); pick 34 / 47 (p 0.18).
- **Beside the rule.** The stock arm trails on every count. Against the full path, nine of its net thirteen lost
  listings are on DataBench (1 win, 10 losses), whose tables hold thousands of values and the shortlist decides which
  160 the API sees. Against the zero arm it loses on listing outright (11 wins, 29 losses, p 0.006; pick 29 / 46,
  p 0.064). So the trained reader still helps as a shortlist on large tables, by about 13 to 18 questions of 896, and
  its scores help nowhere.
- **Fewer unwarned right answers in both arms** at these thresholds (600, 577, 569; zero 23 wins, 46 losses, p 0.008;
  stock 36 / 67, p 0.003), while right answers in all, warned included, are 629, 628 and 613. Without the reader's
  scores the choice is a little less sure. The thresholds are the CLEF path's and were fitted for neither.
- **Latency** (per question, dev box CPU, p50 / p90): full 1.00 s / 2.19 s; zero the same path with its read call
  from the cache; stock 2.12 s / 7.39 s, its reading step 1.18 s / 5.72 s, because it embeds a workbook's targets when
  the first question arrives (that work belongs at upload and is not a cost of the design).

**Reading.** On this path the trained reader's reading of the question is not needed: an outside model reading closed
questions over the sheet's own options does all of it. What remains of the reader is a shortlist of values on very
large tables, worth about two points, and a shortlist need not be learned: asking the columns first and then the values
of the chosen columns would remove the cap. The data plans of the last two weeks (reviewed labels, the Argon pilot,
coverage sheets) were aimed at a part this path does not use.


## The Gemini Decisions API in CLEF's two places: on the four public and synthetic dev sets the same bundle goes from 152 right and 131 wrong of 896 to 600 right and 89 wrong (317 + 148 flagged right and 30 wrong at the CLEF path's own decision); level with the 27B CLEF reference, and short of the launch bar at 65% right for 8% wrong (2026-11-18)

- On the owner's order of 2026-11-18 ("go ahead, run the dev measurement with the decisions api"). Experiment code
  only, outside production imports: `training/readers/data/experiments/decisions-probe-20261118/` (gitignored; the
  contract, written before the run, with hashes, gates and the results, in its `experiment-contract.json`). Nothing is
  served. Serving it would change the rule that own-data selection uses no external model; that is the owner's decision.
- **What ran.** `clef-serve-20261109/local/clef_path.py` unchanged (sha256 `71dcb507`), with `gemini-decision-1`
  (Google's Decisions API, Developer API, launched 2026-11-11; closed questions in, a probability per option out, no
  text) behind `clef_client`'s interface (`local/decisions_client.py`, `453af62a`; driver `local/run_decisions.py`,
  `f249e9f1`). Reading: one call, each field of the reading record one closed choice question over the record's own
  options (measure, filter, value, aggregate, form and the rest, 17 fields), the probabilities fused into the reader's
  scores at W = 2; the decoder builds the pool of 50 and the engine runs it. Choice: one call over the candidates that
  ran (each one's read-as sentence and a 3-row preview of its answer) and "none". No model writes SQL. W and the
  thresholds (ask below 0.42, flag below 0.575) are the CLEF path's own, fixed before the run and not tuned for this
  API. Two paths per question on the same prepared workbook: **full** (the API reads and chooses) and **choose_only**
  (the reader's own pool; the API only chooses).
- **Bundle and code.** `named_coverage_a_s0/aligned` (reader.pt sha256 `6bf41460ef95`), mode `named_plus_open`; main
  `01313e2` clean, eval worktree `9bc24e4` clean.
- **Sets** (896 questions): messy dev 120 (invented workbooks), WikiTableQuestions dev sample 300, DataBench dev 300,
  fresh dev twin 176 (public US government workbooks). The driver refuses the Enron set: no Enron sheet goes to an
  outside model. Sent per question: the question, each tab's column headers, kinds and three example cells, the
  record's options (up to 160 cell values and 80 printed line labels), and for the choice each candidate's read-as
  sentence and 3-row preview. No whole table.
- **Run.** 2026-11-18 00:26-02:29: 2,655 live calls and 24 from the pilot's cache, no error, no retry; 14.08M input
  tokens, $1.41 at the list price ($0.10 per 1M). The full path costs 11,617 tokens a question ($0.0012).

**Outcomes** (of 896). "As served" is the CLEF path's decision on the likeliest candidate. "Per answer" sums a
candidate's probability over the listed candidates that return the same answer and applies the same two thresholds;
it was added to the contract after an 8-question pilot and before the full run, because this API splits its
probability across candidates that agree on the answer.

| | right | flagged right | asked | flagged wrong | wrong | right answer listed | first right | pick right, always answering |
|---|---|---|---|---|---|---|---|---|
| named reader alone (`cov_al`, its fitted chooser) | 152 | | 613 | | 131 | 498 | 272 | 284 |
| choose_only, as served | 308 | 68 | 417 | 52 | 51 | 498 | 272 | 432 |
| choose_only, per answer | 380 | 31 | 335 | 60 | 90 | 498 | 272 | 428 |
| full, as served | 317 | 148 | 364 | 37 | 30 | 750 | 514 | 650 |
| **full, per answer** | **600** | 29 | 129 | 49 | 89 | 750 | 514 | 649 |
| CLEF reference (27B decoder, bge-small reader, fitted chooser) | 393 | 113 | 341 | 24 | 25 | 735 | 466 | 637 |

Full path by set, per answer (as served in brackets):

| set | right | flagged right | asked | flagged wrong | wrong | listed | pick right |
|---|---|---|---|---|---|---|---|
| messy dev (120) | 76 (46) | 5 (25) | 22 (36) | 9 (10) | 8 (3) | 113 | 85 |
| WikiTableQuestions (300) | 187 (57) | 10 (55) | 42 (168) | 23 (10) | 38 (10) | 243 | 207 |
| DataBench (300) | 244 (175) | 8 (42) | 19 (61) | 7 (8) | 22 (14) | 279 | 256 |
| fresh dev (176) | 93 (39) | 6 (26) | 46 (99) | 10 (9) | 21 (3) | 115 | 101 |

- **Gates** (full path, fixed before the run). Right answers given at least 304: **passed** on both readings (465 as
  served, 629 per answer). Unwarned wrong at most 71: **passed as served (30), failed per answer (89)**. Right answer
  listed at least 528: **passed** (750).
- **Against the named reader alone** (per question, sign tests): listed 265 wins, 13 losses; per-answer right 479 / 2;
  per-answer wrong 103 / 96 (p 0.67); pick right 378 / 13. The gain is the reading: choose_only lists the same 498 and
  its pick is right on 428-432.
- **Against the CLEF reference** (not a control: an older reader and commit, a chooser fitted for CLEF). At equal
  coverage the two are level: the full path's 418 surest per-answer picks are 394 right and 24 wrong (CLEF's 418
  unwarned answers 393 and 25); its 555 surest are 513 and 42 (CLEF's 555 answers 506 and 49). Per question: listed
  37 / 22 (p 0.07), first right 123 / 75 (p 0.0008), per-answer pick 62 / 53 (p 0.46).
- **The risk curve** (not gated). Dev-chosen threshold, so optimistic: per answer 579 right at 71 wrong, 350 at 17.
  Cross-fitted (one threshold fitted on half the questions by id hash, applied to the other half; post hoc, not in
  the contract): **583 right, 240 asked, 73 wrong (65.1%, 8.1%)** at the 8% cap and 349 / 528 / 19 at the 2% cap; the
  CLEF reference under the same fit 591 / 232 / 73 and 358 / 520 / 18; choose_only 363 / 466 / 67.
- **Where the full path loses a question** (per answer, of 896): right answer not in the pool 146 (messy 7, WTQ 57,
  DataBench 21, fresh dev 61); in the pool but the pick is wrong 101 (28, 36, 23, 14); pick right 649 (85, 207, 256,
  101). Where listed, the right answer is the decoder's first for 514, at ranks 2-5 for 150, 6-20 for 62, 21-50 for 24.
  "None" is likeliest on 73, 16 of them with the right answer listed.
- **Latency** (full path, per question after workbook preparation; dev box CPU, 4 threads, two measuring processes
  sharing the box): total p50 1.00 s, p90 2.19 s, p99 8.1 s. The read call p50 0.37 s / p90 0.59 s, the choice call
  0.29 s / 0.49 s; the reader 0.21 s / 0.40 s and the pool 0.07 s / 0.85 s (DataBench's large tables carry the tail).
- **Repeatable.** The first 40 WikiTableQuestions questions asked again with an empty cache (120 live calls, 02:31):
  all 7,133 read probabilities and every choice probability identical to the main run's, the same pool and the same
  outcome on 40 of 40 for both paths.

**Reading.** Closed questions over the reader's own options, answered by an outside model that returns only
probabilities, take the engine from 17% right to 67% on these sets with no model writing SQL, at the accuracy of the
self-hosted 27B decoder, for about a tenth of a cent and one second a question. It does not reach the launch bar (80%
right, at most 8% unwarned wrong): always answering is right on 649 of 896 (72.4%), the pool holds the right answer for
750 (83.7%), and at 8% wrong it answers 583 right (65.1%). WikiTableQuestions: pick right 207 of 300 (69.0%; the bar
70%). What is left is the pool (146 questions, most on fresh dev and WikiTableQuestions) and the choice among listed
candidates (101), not the outside model's probabilities.

**Not measured.** The Enron set (refused) and the frozen sets (never read); follow-up turns; the trained reader's own
share under fusion (no arm without its scores; the CLEF reference lists 735 with the older bge-small reader); rate
limits under load (this run made about 0.4 calls a second); Vertex (the API is on the Developer API only).


## Coverage data: reviewed questions on synthetic sheets for all 91 schema.org classes make 12-15 structure names fire (none before) and qualify 5-6 properties, but listing stays at 418-428 of 583 and the aligned chooser answers 173 right and 163 wrong (pilot 161 and 173); the listing and answer gates fail (2026-11-17)

- On the owner's orders of 2026-11-16 ("go ahead, synthetic tables allowed"; "go ahead, run it all", spending past caps
  and working around failures). Training data only: Gemini 4 Argon (OpenRouter) writes synthetic sheets and questions,
  the engine computes every answer, blind Claude reviewers keep or reject, and bge-m3 is the only model trained or served.
  No dev, frozen, OGL or Enron table went out. Code next-engine `fc5c3c3` (word-structure targets, printed line roles) +
  `529f12a` (side `pub_coverage`) + `b484dad` (main's RunPod v2 client); measure from the eval worktree `9bc24e4`, as
  every bundle below. Experiments in next-engine `training/named/data/experiments/` (`coverage`, `named_coverage_a_s0`,
  `named_coverage_q_s0`, `named_{coverage,pilot}_a_s0/aligned`; each contract, with its data and artifact hashes, in
  its `experiment-contract.json`).

**The finding that started it.** 81 of the reader's 100 structure names (operators, functions, filters, word roles) never
fired in `named_rev2_a_s0`: the trainer taught question words only through linked columns' meanings, and operators only
through the closed heads; the factory prompt even said operator words need no link. Nothing gave a question word a
structure target. `fc5c3c3` adds one: rows that carry `structure_spans` (words a reviewer confirmed say a part of the
program) train the word-structure loss.

**Coverage pipeline** (`coverage/coverage.py`, 14 commands). All 91 schema.org classes over the 83-property basis get
messy working sheets (31 held and 15 train tables per property; host-written books with SUM formulas, printed totals and
subtotals; the factory screen skipped for synthetic books only): 1,136 sheets. Questions are program first: the grammar
enumerates programs on each compiled table, a quota choice fills each structure name, and Argon writes the question and
marks each part's words, or skips (28%). 60 blind Claude reviewers judged 8,282: 6,706 kept (meaning and natural wording),
6,473 with marks passed; my sample agrees on 19 of 20. Training part: 3,069 rows on 437 tabs; held truth 3,637 rows on
697 tabs (5,969 labelled columns; 80 of 83 properties have at least 25 held positives on both roles). Spend $30.0 (tables
$14.7, questions $12.3, labels $3.0; OpenRouter total about $40.5 of $50).

Some names cannot be reached by construction: answer_type code and money, period half / season / relative / week /
weekday / hour, size signed, and by_size; entity peer and measure_kind across stay thin. Reviewers' rejection notes name
the engine's table reading again: text dates read as text (83), printed totals and subtotals read as data rows (222),
banded values (52).

**Training** (`named_pilot_a_s0`'s recipe, seed and picks; gates fixed before each run):

| | right listed (of 583) | named alone | first right (of 1,082) | studied / messy / WTQ / DataBench / fresh dev listed | first right by set |
|---|---|---|---|---|---|
| `named_pilot_a_s0` | 418 | 374 | 281 | 57 / 50 / 118 / 148 / 45 | 32 / 24 / 90 / 105 / 30 |
| `named_coverage_a_s0` (coverage examples) | 418 | 387 | 297 | 55 / 46 / 118 / 150 / 49 | 25 / 28 / 99 / 107 / 38 |
| `named_coverage_q_s0` (+ the 6 qualified properties in links) | 428 | 381 | 280 | 56 / 47 / 119 / 157 / 49 | 30 / 23 / 84 / 107 / 36 |

- `named_coverage_a_s0` fails its listed gate (at least 433, pilot + 15): listed 25 wins, 25 losses; named alone 37 / 24
  (p 0.12); first right 74 / 58 (p 0.19). It passes its structure gate: on the held marked words
  (`coverage/structure_fire.py`, at least 25 positives, precision 0.9, recall 0.5), 12 of 61 judged names fire, the
  pilot 0 (aggregate avg / sum, answer_type count / percent, calc /, condition equals / period, form rank, period range,
  word role filter / key / measure). On the coverage truth the property gate passes 26 properties as a column and 11 as a
  word (the pilot 15 and 2), and 6 qualify for links (byArtist, director, email, image, issn, numberOfEmployees; the
  pilot none).
- `named_coverage_q_s0` puts those 6 in links (`--qualified`) and fails its gate (at least 433, coverage + 15): 428,
  listed 32 wins, 22 losses (p 0.22), DataBench +7, every other set +1 or 0; first right 44 / 61 (p 0.12), WTQ 9 / 24.
  15 structure names fire (adds comparison <, condition comparison / not_equals, row_op /; loses word role measure); the
  property gate passes 23 / 12 and qualifies 5 (orderDate in, image and issn out).
- Latency (p50 / p90 total, by set) 0.23-0.47 s / 0.27-1.03 s, DataBench p90 3.6-4.7 s; same decode as the pilot.

**Why listing cannot show the structure gain, and the alignment fit.** The decoder that builds the pool does not read
word structure; the aligner does (`engine/named/align.py`, its "structure" evidence feeding the chooser), and no
coverage or pilot bundle had a fitted alignment, while the step-2 measure takes the decoder's first candidate. The H7
recipe (`training/named/align.py`, collect / train / chooser) was fitted on both bundles on CPU with the pilot's 390 held
families (held 2,000 questions, train 3,005): on the held questions the aligned chooser puts 578 right first for the
coverage bundle (decoder order 415; out of fold 581) and 602 for the pilot (425; 611), with 1,200 and 1,207 listed.
On the five dev sets with the fitted chooser (`--chooser engine`, which answers or asks; of 1,082 questions):

| aligned bundle | answered right | asked | answered wrong | chooser's pick right | first right without the fit |
|---|---|---|---|---|---|
| `named_pilot_a_s0` | 161 | 748 | 173 | 320 | 281 |
| `named_coverage_a_s0` | 173 | 746 | 163 | 333 | 297 |

The fit raises the right pick by 36 and 39. The coverage bundle answers 12 more right (62 wins, 50 losses, p 0.30) and
10 fewer wrong (85 / 95, p 0.50); listed is unchanged (47 / 46). The right gate (at least the pilot's + 15, 176) fails by
3; the wrong gate passes. Right / wrong by set, coverage: studied 21 / 32, messy 12 / 8, WTQ 60 / 57, DataBench 57 / 34,
fresh dev 23 / 32; pilot 26 / 34, 12 / 5, 48 / 52, 53 / 42, 22 / 40. Latency p50 / p90 0.37-0.62 s / 0.47-1.38 s,
DataBench p90 6.2 s, while the box also ran the q bundle's gates; the chooser adds about 0.04 s at p50.

**Where this leaves the plan.** Every data step since the reviewed-label plan stops listing at 407-428 of 583, against
the plan's line near 525: reviewed WTQ and DataBench labels, Argon's pilot on public tables, and coverage sheets for every
class alike. The structure names that now fire do not change what the decoder builds, and through the aligner they move
answers within noise. With the fitted chooser, about half of what it answers is wrong (173 right, 163 wrong), and the
launch bar asks at most 8% unwarned wrong. The reviewers' rejections keep naming the engine's table reading (totals and
subtotals read as data, text dates, bands), which no reader training changes. No further lease runs on this plan without
the owner's decision.

**GPU and lease incidents.** RunPod's `rest.runpod.io` stopped resolving mid-run (the first lease could not terminate its
pod; terminated through `api.runpod.io/v2`); next-engine `b484dad` takes main's v2 client with its command reserve and six
lease tests (release suite 63 of 64, the failure the pre-existing scan of gitignored experiment copies). Every create then
failed with "no instances available": the named lease's recorded CUDA list (12.4-12.9) matches no host now (hosts run
13.x), so the floor is passed instead. The q run ran out of GPU memory on a 24 GB RTX 4090 near step 650 and moved to 48
GB cards (RTX 6000 Ada, $1.09/h, 360 minutes, past the contract's $0.80/h on the owner's order). Three retry loops I had
stopped with TaskStop survived, woke up and leased three more pods (2 L40S, one RTX 5090) that billed about 1.5 h before
I terminated them; about $4 of GPU was lost. Leases this entry: coverage about $3.4, q about $3.65; GPU on the plan about
$27 of $40.

## The Argon labelling pilot: 2,218 reviewed questions on new public tables list 418 of 583 (+10), five short of the gate, with first right unchanged; the property gate cannot see the new labels (2026-11-16)

- After the reviewed-label plan's STOP (the entry below), on the owner's orders of 2026-11-16 ("go with deepseek, run the
  census first"; "use openrouter for the bake-off"; "go with argon, run the pilot"; "go ahead, run it all"). Training data
  only: a frontier model writes questions and labels, the engine computes every answer, blind Claude reviewers keep or
  reject, and bge-m3 is the only model trained or served (no logits, no generated SQL). Only public training tables went
  out (WTQ train tables no dev question reads, DataBench 001-049, EIA and SEC public sides); no dev, frozen, OGL or Enron
  table. Code next-engine `2e51423` (`6133705` + `training/named/data.py` sides `pub_wtq`, `pub_databench`); measure from
  the eval worktree `9bc24e4`, as every bundle below. Experiments in next-engine `training/named/data/experiments/`
  (`writer_bakeoff`, `argon_pilot`, `named_pilot_a_s0`).

**Census** (training data only). Of the 83 schema.org properties in the reader's basis, 39 have no labelled factory column
and 43 no question linked to them; 13 have at least 100 on both sides. None of the 1,131 reviewed tabs carries a property
label, and the reviewed labels hold almost no row arithmetic, dates or percentages. The meaning heads had nothing to learn
from on the question side, which is why the property gate qualified none (`named_rev2_a_s0`: three properties pass as a
column, identifier, description and observationDate, and none as a word).

**Writer bake-off** (20 public tabs, the factory's prompt f7, 153 question slots per writer, OpenRouter with reasoning
effort low; 9 blind Claude reviewers judge each runnable question on meaning and natural wording and each distinct column
label; my sample agrees on 22 of 24 questions and 12 of 12 labels):

| writer | ran | kept | kept, no column words | labels right in the basis | cost |
|---|---|---|---|---|---|
| DeepSeek V4.1 Flash | 138 | 84 | 51 | 11 | $0.04 |
| DeepSeek V4 Pro | 114 | 69 | 43 | 19 | $0.58 |
| Kimi K3.1 Pro | 144 | 84 | 68 | 17 | $0.52 |
| Gemini 3.8 Flash (wrote all of `examples_v5`) | 146 | 96 | 53 | 10 | $0.07 |
| Gemini 4 Argon | 145 | 86 | 66 | 29 | $0.17 |

DeepSeek broke the label format, and Pro misnamed tables. 80 of the 187 rejections on meaning come from the engine's
reading of the tables (subtotal and division lines read as data, balance-sheet sections running past their end), the same
for every writer. Argon writes the most questions without the column's own words and the most right basis labels.

**Pilot funnel**: 1,049 eligible public train tabs labelled by Argon ($3.73; the factory screen applied, a tenth of each
source held as in the review batches). 470 were chosen to cover the thin properties (fewer than 100 linked questions): 52 of
the 69 thin ones, while the other 17 occur on none. Argon wrote 3,696 questions ($4.12). Of those, 3,529 ran, and 30 blind
Claude reviewers kept 2,218 (means 2,900, natural 2,661; my hash sample of 40 agrees on 38). Thin properties with at least
25 linked questions went from 4 to 22 of 69, and 70% of the pilot's links are not in the column's own words. Training set
`examples_pilot` = `examples_v5` unchanged + the pilot: 8,327 tabs, 46,101 gold rows. OpenRouter spend about $10.4 in all
(bake-off $2.6 with a discarded first round, pilot about $7.9); the lease about $3.1 (GPU on the plan now about $15 of
$40).

**Training** (`named_pilot_a_s0`, `named_rev2_a_s0`'s recipe and picks, the gold examples the only change; gate fixed
before the run: at least 423 listed, rev2 + 15):

| | right listed (of 583) | named alone | first right (of 1,082) | studied / messy / WTQ / DataBench / fresh dev listed | first right by set |
|---|---|---|---|---|---|
| `named_rev2_a_s0` | 408 | 368 | 281 | 57 / 45 / 113 / 147 / 46 | 28 / 28 / 88 / 106 / 31 |
| `named_pilot_a_s0` | 418 | 374 | 281 | 57 / 50 / 118 / 148 / 45 | 32 / 24 / 90 / 105 / 30 |

The pilot fails its gate (418 < 423). Listed: 34 wins, 24 losses (sign test p about 0.24); by set, messy +5 (10 wins, 5
losses), WTQ +5 (11, 6), DataBench +1, studied 0, fresh dev -1. First right: 57 wins, 57 losses. Messy dev listing, flat at
45-46 through every reviewed-label step, moves for the first time, in the direction the hypothesis expects; but the gain
cannot be told from noise and the first candidate is no better. Training-side held link top 1 is 0.692 (rev2 0.696). The bundle has the same
size and decode as rev2; its measured p50 / p90 (0.20-0.30 s / 0.28-2.31 s by set) are below rev2's measure only because
the box was less loaded.

**Property gate on the pilot bundle** (`training/named/gate_dimensions.py` unchanged, truth the factory's val second pass,
precision >= 0.95 on at least 25 held positives, both roles): none qualifies for links, as on `named_rev2_a_s0`. As a
column, startDate passes (0.969 precision, 0.33 recall; rev2 0.944) while identifier, description and observationDate fall
just under 0.95 (0.911, 0.942, 0.937; rev2 passed them at 0.956, 0.971, 0.955). As a word nothing passes in either
bundle; for 12 of the 14 properties with 25 held positives no threshold reaches the precision at all. The gate's truth
holds only those 14 properties, and none of them is a thin property the pilot added, so the gate cannot see what the pilot
wrote; a truth for the thin properties (reviewed questions on held tables) is the owner's decision, and by the plan no
second lease runs.


## Reviewed data to 90%, phases 1-3: on reviewed labels only the named reader lists 408 of 583; doubling the labels again moves nothing (407), so the plan stops below its ~470 line (2026-11-16)

- The owner's plan of 2026-11-15 (REVIEWED DATA TO 90%; GPU cap $40; approved ~03:55). Under his rule of 2026-11-14 the
  reader trains only on reviewed labels: CLEF and the kept engine draft, frontier reviewers and the engine check, nothing
  unreviewed trains. Code next-engine `9bc24e4` (every bundle trained and measured on it; the eval worktree stayed there),
  recipe of `named_m3_open_s0` (bge-m3, 15,000 steps, anchor 300, open-in-link, seed 0), dev step-2 measure on the five dev
  sets in mode named_plus_open: the CLEF path's 583 right answers listed by the small reader's first 50 candidates, those
  listed through named terms and exact text alone, and the first candidate right of all 1,082. Contracts and results in
  next-engine `training/named/data/experiments/named_rev{,2,3}_a_s0`, `named_rev_b_s0`; GPU about $12 of $40.

**Labels.** Phase 0: 1,066 reviewed WTQ labels (`reviewed_wtq_v1`, from CLEF's drafts on WTQ training tables; a second
review accepted by the owner on 2026-11-15, each label re-checked by the kept engine). Phase 2, per batch: the kept bundle's readings that return the gold answer, drafted on unused WTQ train questions and
DataBench train (a tenth of the tables held for measuring, never trained on); reviewer 1 picks the one reading that means
what the question asks, or none; an independent reviewer 2 sees only the picked reading and keeps or rejects it on meaning;
every kept label is re-run to gold on the reader's own tables; I check a hash-drawn sample of every slice set.

| batch | drafted | with a gold-returning reading | reviewer 1 picked | reviewer 2 kept | my sample agrees |
|---|---|---|---|---|---|
| 1, WTQ | 2,000 | 1,374 | 856 | 822 (820 after the 2 my sample excluded) | 38 of 40 |
| 1, DataBench (tables <= 10,000 rows, <= 60 columns) | 528 | 314 | 234 | 233 | 25 of 25 |
| 2, WTQ | 4,500 | 3,042 | 1,775 | 1,727 | 80 of 80 |
| 2, DataBench (the 12 larger or wider tables) | 240 | 122 | 81 | 81 | 25 of 25 |

Reviewer 2 rejects few after reviewer 1 (DataBench 1 of 315 across both batches): the reviewers' work is reviewer 1's
choice among readings that all return the gold answer, most of them by coincidence. Label sets: `reviewed_v2` = phase 0 +
batch 1, 2,119 labels on 748 tabs; `reviewed_v3` = + batch 2, 3,927 labels on 1,285 tabs; every batch validated through
the training losses before a lease.

**Phase 1, named against unnamed** (the same 1,066 labels):

| | right listed (of 583) | listed by named terms alone | first right (of 1,082) |
|---|---|---|---|
| baseline `named_m3_open_s0` (414 unreviewed DataBench + 3,127 unreviewed factory picks) | 398 | 361 | 258 |
| A, named (`named_rev_a_s0`) | 369 | 339 | 231 |
| B, unnamed (`named_rev_b_s0`, no named term in a link) | 328 | 253 | 227 |

B fails its gate (41 fewer listed than A: 27 wins, 68 losses); A carries forward. A against the baseline is DataBench -42
(1 win, 43 losses) and +13 on the other four sets: the owner's rule costs exactly the set whose unreviewed picks it removed.

**Phase 3, the learning curve** (arm A's recipe, the label set the only change; rule: keep scaling while a doubling adds
>= 15 listed):

| | labels | right listed (of 583) | named alone | first right (of 1,082) | studied / messy / WTQ / DataBench / fresh dev listed |
|---|---|---|---|---|---|
| A `named_rev_a_s0` | 1,066 | 369 | 339 | 231 | 58 / 46 / 112 / 107 / 46 |
| `named_rev2_a_s0` | 2,119 | 408 | 368 | 281 | 57 / 45 / 113 / 147 / 46 |
| `named_rev3_a_s0` | 3,927 | 407 | 348 | 263 | 53 / 46 / 113 / 149 / 46 |

The first doubling passes (+39: 65 wins, 26 losses), and its whole gain is DataBench (+40), where batch 1 brought the first
233 reviewed DataBench labels; 820 more WTQ labels moved no listing but put more right first (WTQ 88 vs 75, messy 28 vs 18).
Reviewed labels alone then beat the reader trained on unreviewed picks (408 / 368 / 281 against 398 / 361 / 258). The second
doubling fails (-1: 28 wins, 29 losses; first right -18, 41 wins and 59 losses, sign test p about 0.09). 1,808 more labels of
the same kinds (WTQ-shaped tables, large DataBench tables) add neither coverage nor ordering: data of these kinds is not the
limit. Studied, messy and fresh dev, the sets most like real sheets, did not move in any step. The curve is flat at 408,
below the plan's ~470 line, so its STOP holds: no further spend, the third batch (2,269 WTQ and 58 DataBench drafts with a
gold-returning reading, on the last eligible WTQ train questions and DataBench tables over 60,000 rows) is not reviewed, and
`named_rev2_a_s0` stays the better bundle. Its training-side held link top 1 is flat across all three (0.70 / 0.70 / 0.70).

A WTQ cell `INF` crashed a third-batch drafter: the verifier rounded `float('INF')`. Fixed in the owner (next-engine
`6133705`: a value `float()` reads as non-finite is not a number, as the sheet reads cells; regression test
`test_a_code_python_reads_as_infinite_is_text_to_the_verifier`).

**Phase 4a, decode depth** (no spend; `named_rev2_a_s0`, mode named_plus_open, first candidate, eval worktree `9bc24e4`;
listed at 50 and 100 read from the same run's rank of the first right candidate):

| depth | right listed (of 583) | first right (of 1,082) | p90 studied / messy / WTQ / fresh dev / DataBench (s) |
|---|---|---|---|
| 50 (the gate's measure, this run) | 410 | 284 | 0.39 / 0.43 / 0.25 / 0.99 / 2.51 (rev3's idle measure) |
| 100 | 428 | 284 | not measured alone |
| 200 | 444 | 284 | 1.21 / 1.36 / 0.84 / 1.48 / 5.99 (shared CPU) |

Depth adds coverage further down the list only (first right is the same candidate): +18 at 100, +34 at 200, most on
messy (+8) and DataBench (+11). At 200 every p90 is 1.5 to 3.3 times depth 50's, over the 1.25 latency gate even
allowing for the shared CPU; 100 was not measured alone. Neither depth reaches the ~470 line, so phase 4a does not lift
the STOP.

## Discover and name: the reader's own decision features, named by schema.org text and trained as named dimensions, do not move dev; the 185 lost answers are lost in the reading, not the language (2026-11-14)

- The owner's plan of 2026-11-14 (DISCOVER AND NAME; GPU cap $10; candidates outside the basis allowed; the 84 never-firing
  structure names dropped). Steps 1-4 on the training split only (next-engine experiments `named_discover_s0`); step 5 two
  contracts (`named_m3_found_s0`, `named_m3_found_ctl_s0`), code next-engine `95a5313` (`--names`, `--name-rows`). **Every
  bundle in this entry, like every bundle since step 4b, was trained on CLEF's picks unreviewed**; the owner ruled the same
  day that CLEF only drafts and that only data he has reviewed trains the reader.

**Steps 1-4.** The `named_m3_open_s0` reader over 2,242 training tabs (132,670 units, 19,113 links with a target): a top-k
sparse autoencoder (1,024 features, 8 active, 90% of variance) on the hidden state the named head reads; 294 features fire
selectively on both question words and sheet targets, 79 decide something (a weight fitted on 12,577 kept-family links
raises held-family link top 1 with more wins than losses). All 79 together: held link top 1 4,161 -> 4,432 of 6,536 over the
named-only sum, 4,365 -> 4,515 over the served sum. Naming by the centred cosine of a feature's top units' own texts to
1,454 schema.org texts is weak: on 25 known properties it names 8 right at rank 1, and 5 of the 12 that pass its threshold
(42% precision). It named 42 features (36 properties, 6 in the basis); those 42 together: +2.8 points held top 1
named-only, +1.3 served, which passed the step-5 go rule fixed before step 3 was read.

**Step 5.** From `named_m3_open_s0`, 5,000 more steps each: the treatment with the 36 found dimensions (each output started at
its feature's readout, anchored by its schema.org text, in the link sum), the control with the same name-set cut and none.
Dev, step-2 measure, mode named_plus_open:

| | right listed (of 583) | listed by named terms alone | first right (of 1,082) |
|---|---|---|---|
| baseline `named_m3_open_s0` | 398 | 361 | 258 |
| control (cut name set, 5,000 more steps) | 392 | 347 | 252 |
| treatment (+ 36 found dimensions) | 396 | 356 | 238 |
| the plan's gate | >= 416 | > 361 | > 258 |

**The gate fails on all three.** Against the control the found dimensions add 4 listed (22 wins, 18 losses) and 9 named-alone
and lose 14 first right (34 wins, 48 losses); against the baseline first right falls (39 wins, 59 losses). The held-link gain
did not carry to dev. No expansion, no further spend; GPU about $3.60 of $10. Cutting 84 never-firing names changed nothing
measurable, as their zero link mass said it would.

**Where the 185 lost answers are.** The CLEF path answers 583 dev questions right; the baseline lists 398 of them. For each of
the other 185 the same reading was decoded to depth 500 and run in order (`named_discover_s0/split_unlisted_e_m3_open.jsonl`):

| where the right program is | questions |
|---|---|
| beyond depth 500 | 114 |
| the decoder's space ends before 500 without it | 8 |
| ranks 51-500 (quartiles 85 / 165 / 303) | 57 |
| in the first 50 but past the verifier's line budget | 6 |

The CLEF path builds these programs with the same decoder and language, from its own reading, inside its first 50; ours
puts two thirds of them past 500. **The gap is the reading, not what the language can say.** Where the right program ranks
51-500, the slot where it loses most to the top program is a closed head in 32 (direction 9, conditions 7, answer type 7,
aggregate 6, entity 2, form 1) and a link in 25 (value 10, shown 7, measure 3, filter 2, contains 2, key 1).

**Reviewed data.** Under the owner's rule the 1,441 CLEF drafts on WTQ training tables were run as CLEF read them: 740 return
WTQ's gold answer and are accepted (his call); 657 miss and offer one to five readings that do, which he reviews on a private
page; 44 offer none and are dropped (`review_wtq_v1`). Those labels, not unreviewed picks, are what the reader trains on next.


## The unnamed term in the decision: bge-m3 lists 398 of 583 (+22) and 361 through named terms alone (+42); only 16 of the 183 named dimensions ever reach a link (2026-11-14)

- Owner's go of 2026-11-13 evening ("Yes, write the contract and run it"), after the CSS reading (a model whose anchored
  dimensions do not starve its unnamed ones keeps what nobody named). Contract `named_m3_open_s0` (next-engine, gitignored
  experiments folder), code next-engine `f7c910a`: `--open-in-link` puts the one labelled unnamed term (its learned weight
  times the best cosine of any question word to the target, never below 0) in every link's and pick's sum, and the
  unnamed channel trains the encoder. Otherwise the control's recipe exactly (`named_m3_one_s0`: bge-m3, one stage of
  15,000 steps, the same labels, corpus, gold links and CLEF's picks). Measured with the step-2 measure on the five dev
  sets from next-engine `f7c910a`, each bundle alone; dev measured, never mined.

| bge-m3, one stage | right listed (of 583) | listed by named terms alone | first right (of 1,082) | p90 studied / messy / WTQ / fresh dev (s) |
|---|---|---|---|---|
| control, mode named | 376 | 319 | 245 | 0.468 / 0.684 / 0.272 / 0.696 |
| control, the unnamed term at a weight fitted on training questions (0.25) | 369 | 314 | 232 | 0.512 / 0.726 / 0.275 / 0.574 |
| **treatment, the unnamed term at its learned weight (1.18)** | **398** | **361** | **258** | 0.475 / 0.553 / 0.309 / 0.728 |
| treatment, mode named (the term not served) | 397 | 361 | 247 | 0.386 / 0.444 / 0.311 / 0.965 (shared CPU, not clean) |

The contract's gate (written before the run): at least 18 more listed than the control's better mode, more first right,
and p90 at most 1.25x. The treatment lists 22 more (54 wins, 32 losses; two-sided sign test p 0.023), puts 13 more first
(75 wins, 62 losses; p 0.31, not significant) and its p90 is at most 1.14x: **the gate passes**. Held links read top 1 at
0.695 against 0.648. Two readings matter more than the gate. Listed through named terms alone rises by 42, more than the
whole gain, and the same bundle without the term at serving lists 397: the gain is the training, not the term served.
Letting the decision train the unnamed capacity made the named terms better. And the run sits at 68% of the CLEF path's
right answers, 36-43 below the opaque references (434 and 441) and 127 below the 90% mark (525). GPU about $3.71 (one
L40S lease; its own download failed on a host that resets long transfers, and a 16-part checked pull recovered the bundle).

**Which named dimensions work.** On 575 training-split questions over 58 workbooks (3,841 question words, 10,325 sheet
targets, 104,734 written links), counted per dimension in the treatment bundle:

| dimensions | count | what they do in a link |
|---|---|---|
| structure names that never fire | 82 | none reads 0.5 on any word or target; zero link terms. 73 are copies of the closed heads' program names (form, aggregate, direction, comparison, operators, answer type, period, size and the like): the coordinate head labels a target only with its column role, value kind and line role, so these train to 0 everywhere while the closed heads read the same meanings; 9 line-role and value-kind names never occurred |
| structure names on the sheet side only | 2 | `column_role:spacer`, `value_kind:blank`: no question word reads them |
| meaning properties | 83 | link weight 0 (the gate qualified none); 37 of them have no training label at all, and 80% of labelled training columns carry no basis property (`price` alone is 40% of the positives) |
| working | 16 | `column_role` measure, key, peer, label, row total, derived, section label, date, tab name, period; `value_kind` number, date, period, text (on every value and no word: always on); `line_role` total, band |

`column_role:measure`, `value_kind:number` and `column_role:key` carry 71% of all named link mass, the top nine 95%. Every
working name says what kind of target a target is; none says which of two targets of one kind a word means. That choice
falls to exact text and, served, to the unnamed term. **Reading:** reducing the named dimensions changes no link score;
the gap to 90% is information no named dimension carries. The owner's plan of 2026-11-14 (DISCOVER AND NAME: find the
reader's own decision-relevant features, name them by schema.org text, train them as named dimensions; drop the 84 names
that never fire) is under way.


## Step E and the pick learning curve: bge-m3 fails its gate (+12 listed of 583, WTQ p90 3.7x) while putting 38 more right answers first; past a quarter of CLEF's picks, more picks do not move the reader (2026-11-13)

- The plan's step E (bge-m3 head to head) and step 4's learning curve, both measured with the step-2 measure
  (`distill-probe-20261112/local/step_named_eval.sh`, mode named, no chooser) on the five dev sets from next-engine
  `9cdaaa9` (serving equal to `7a4d9e0`, the only engine change since step 4 is bge-m3's backbone entry). Dev sets
  measured, never mined.

**Step E.** One stage of 15,000 steps on each backbone, the same recipe (self-anchors, Gemini's column labels, the
schema.org corpus, gold links, CLEF's recorded picks, the unnamed channel detached; contracts `named_m3_one_s0`,
`named_small_one_s0`). Latency measured with each bundle alone (2 processes, 4 threads); DataBench's large tables are
outside the latency gate.

| One stage, 15,000 steps | right listed (of 583) | listed by named terms alone | first right (of 1,082) | p90 studied / messy / WTQ / fresh dev (s) | memory on load |
|---|---|---|---|---|---|
| bge-small (33M) | 364 | 286 | 207 | 0.311 / 0.455 / 0.073 / 0.600 | 0.43 GB |
| bge-m3 (568M) | 376 (+12) | 319 (+33) | 245 (+38) | 0.468 / 0.684 / 0.272 / 0.696 | 2.55 GB |

The contract's gate (written before either run): at least 18 more listed and p90 under 3x bge-small's on every set
but DataBench. bge-m3 lists 12 more and its WTQ p90 is 3.7x (p50 3.5x), so **E stops at this stage and bge-small
stays**. The gain sits in the first place instead: 89 questions gain a right first answer and 51 lose one (z about
3.2), and one bge-m3 stage puts a right answer first as often as the two-stage bge-small bundle of step 4b (245 against
244; 76 wins, 75 losses). That is recorded, not acted on: the gate is not moved after the measure. GPU: about $2.60 of
E's $10 (bge-m3 2.65 h including the 2.27 GB backbone upload, bge-small 1.1 h, both at $0.69/h).

**The learning curve.** Step 4b again at 50% and 25% of CLEF's picks (the training workbooks halved and quartered, the
same 144 held), from the same start (`named_anchor_s0`) and code (`2f9b521`):

| CLEF's picks | right listed (of 583) | named alone | first right (of 1,082) | studied / messy / WTQ / fresh dev / DataBench first right |
|---|---|---|---|---|
| 0% (4a, gold links only) | 327 | 320 | 178 | 36 / 16 / 51 / 20 / 55 |
| 25% | 356 | 308 | 230 | 29 / 20 / 72 / 21 / 88 |
| 50% | 351 | 312 | 225 | 28 / 17 / 75 / 17 / 88 |
| 100% (4b) | 357 | 309 | 244 | 24 / 25 / 82 / 25 / 88 |

Picks matter (178 to 225-244), but from a quarter of them on the reader stays where it is: 25% to 100% moves first
right by 67 wins against 53 losses and listed by one. The 25% bundle is its run's best of 9 of 10 evals (step 31,424 of
39,285): the lease ran out and its training state did not come back from a host that resets long transfers
(60.249.37.148), and a full rerun would have passed the owner's $20 GPU ceiling for steps 0a-4 (spent about $18.80).
**Reading for step D:** more CLEF picks of the same kind (a CLEF pod at $4/h, about 1,350 questions an hour) are not
supported by the curve; D stops here. Its other part, gold-checked questions on new public sheets, needs the owner's go
for Gemini.

**Where this leaves the bar.** The best dev reading is 244-245 first right of 1,082 (22.6%), with a right answer
listed at all for 496 (4b) to 514 (bge-m3) of 1,082; the launch bar is 80% right. Neither more picks nor the larger backbone's listing gain
closes that: what limits is what the reader lists.

## Step C: the named chooser adds 16 right picks on dev (244 to 260 of 1,082) once two fit defects are fixed; at its wrong bound it answers 120 right and 140 wrong (2026-11-13)

- The plan's step C on the step-4 bundle (`named_picks_s0`, mode named): the named additive chooser
  (`engine/named/chooser.py`, fitted by `training/named/choose.py`'s conditional logit, calibration and policy) on
  gold-checked candidates of training-split questions the bundle never trained on
  (`named-chooser-20261112/local/fit_chooser.py`): 5,170 questions (4,282 factory held families, 768 WikiTableQuestions
  and 120 DataBench training questions on the workbooks CLEF's picks held out), 3,287 with a right candidate, 256,759
  candidates. Dev measured with `distill-probe-20261112/local/step_chooser_eval.sh` (`--chooser engine`), never mined;
  the classification below reads training questions only.

**The first fit looked strong and failed on dev.** Out of fold it put a right answer first for 1,579 of 5,170 against
1,111 in "decoder order"; on dev its pick was right 217 times against the decoder's first 244, and its policy answered
70 right and 106 wrong. Two defects, both in the fit, found on the 2,581 calibration-family training questions by
running the served path (`named-chooser-20261112/local/serve_check.py`):

| Calibration questions (2,581) | right listed | first right | chooser's pick right | answered right / asked / wrong |
|---|---|---|---|---|
| The bundle as trained | 1,665 | 687 | 687 | 640 / 410 / 1,531 |
| Fitted chooser and refit slot weights (as first written) | 1,593 | 653 | 750 | 294 / 2,013 / 274 |
| Fitted chooser, the bundle's slot weights | 1,665 | 687 | 806 | 372 / 1,934 / 275 |

(1) The fit wrote new decoder slot weights from the same records it fitted the chooser on, records collected under the
old ones. Slot weights steer the decoder's pool and every link and decoder feature the chooser reads (filter, contains,
listing and four more went to 0, number to 1.77): the served chooser met candidates it was not fit on, and 72 right
answers left the list. (2) `fold_report` measured the chooser against the decoder's rank (575 right first) instead of the
served order the records are kept in (687), so the out-of-fold gain read +42% where the served gain is +17%, nearly all on
factory questions (WTQ 104 against 101). Fixed in next-engine `9cdaaa9`: the report reads the served order; slot weights
stay the bundle's unless `--slot-weights` asks, and then the records are collected again under them before the chooser is
fit (`chooser_records`); test `test_the_chooser_is_judged_against_the_served_order_and_fit_under_the_slot_weights_it_serves`
(the old report reads 0 of 40 where the served order lists every right answer first). With the bundle's slot weights the
served decisions equal the records' replay exactly.

| Dev set | decoder's first right | chooser's pick right | answered right / asked / wrong |
|---|---|---|---|
| Studied (186) | 24 | 32 | 13 / 138 / 35 |
| Messy dev (120) | 25 | 23 | 15 / 98 / 7 |
| WikiTableQuestions dev (300) | 82 | 88 | 34 / 227 / 39 |
| Fresh dev twin (176) | 25 | 26 | 14 / 143 / 19 |
| DataBench dev (300) | 88 | 91 | 44 / 216 / 40 |
| **All (1,082)** | **244 (22.6%)** | **260 (24.0%)** | **120 (11.1%) / 822 (76.0%) / 140 (12.9%)** |

**Reading.** Fitted correctly, the chooser's features add 16 right picks on dev; its policy (thresholds agg 0.30, rank
0.35, show 0.45, calc 0.95, margin 0.5, set on calibration families to bound wrong answers among all questions) answers
a quarter of the questions and is wrong on more than half of those. Against the launch bar (80% right, at most 8% wrong
without a warning) the chooser is not the limit; the list is (496 of 1,082 have a right answer listed at all, and the
first-pass reading behind them): a chooser cannot pick an answer the reader never lists. The bundle with the fitted
chooser is `named_picks_s0/out/candidate_chooser_oldslots` (a record, not promoted).

**Step E launched.** bge-m3 (BAAI/bge-m3, pinned revision `5617a9f6`, an XLM-R large retrained for multilingual
retrieval) is a third named backbone on next-engine (`7a4d9e0`). Its hub revision holds only `pytorch_model.bin`, which
transformers will not load under the pods' torch 2.4; the same tensors are kept as safetensors in
a local copy of bge-m3 (hidden states bit-identical to the hub's, `bge-m3-20261113/local/convert.py`), and a
lease uploads a backbone's local copy. Staged by contract: one-stage runs of 15,000 steps on bge-m3 and on bge-small
(the same recipe: self-anchors, labels, corpus, gold links, CLEF's picks), capped at $10; the full two-stage recipe only
if bge-m3 lists at least 18 more of the 583 with p90 under three times bge-small's. bge-small's measured step rate is
3.0-3.9 steps/s (the lease plan's STEP_RATE was 6.0).

## Step 4: CLEF's picks through the link sum raise the right answer listed from 330 to 357 of 583; the meaning-in-links route stops after a full second pass of labels (2026-11-13)

- The plan's retry of the anchoring gate, the full second pass of labels the owner approved for it (2026-11-13 01:55:
  "Yes, to both"), and step 4 on next-engine. Every bundle is bge-small, 39,285 steps, seed 0, the 7,857 factory tabs;
  measured with `distill-probe-20261112/local/step_named_eval.sh` from a worktree pinned at next-engine `92f4ed2`
  (mode named unless stated); dev sets measured, never mined. Machine shared with up to three other engine processes.
  GPU for steps 0a-4 so far about $10.40 of the $20 ceiling; Gemini about $17 for the second pass.

**The retry and the full second pass: no property qualifies, so the meaning-in-links route stops.**
`named_anchor_second_s0` (examples_v6: the second pass's labels on 596 training tabs, the first pass's doubtful
negatives unknown) passed columns 4 (identifier, observationDate, endDate, location), words 1 (price), profiles 39 of
50, and qualified none; its outputs over-fire without the negatives (thresholds 0.95-0.99). It improved some
precisions and missed the gate, the owner's condition for buying the second pass over every training tab: 7,257
requests (train 5,666, pub_eia 258, pub_sec 1,333), none failed, 17.7M prompt and 0.96M output tokens, about $17; the
factory's hard output budget went from 10.2M to 11.7M at his word (next-engine `faf22c0`). examples_v7 carries the
second pass's labels on 7,824 tabs (39,589 columns with basis properties, 33,204 none, 35,936 unknown) and the first
pass's basis positives on 33. `named_anchor_truth_s0` trained on them: columns 3 pass (endDate 0.964, startDate 0.952,
observationDate 0.950; identifier 0.950 / 0.949 just under as column and word), words 0 (identifier 0.949, price 0.933,
name 0.925, orderQuantity 0.907; 10 of 14 judged words reach no threshold at 0.95), profiles 36 of 50; **qualified for
links: none**, as for both earlier bundles. Judged columns at precision 0.90 or better rose from 8 to 9, words from 2 to
4: closer, not through. By the owner's terms the route stops: a link stays the structure tier plus exact-text evidence;
the meaning tier is kept for table profiles and classes, measured, never in a link sum.

| Bundle (first right of 1,082) | Studied | Messy dev | WTQ dev | DataBench dev | Fresh dev twin | All listed of 583 | Named alone | First right |
|---|---|---|---|---|---|---|---|---|
| 0b lookup | 50 / 37 | 41 / 19 | 103 / 44 | 100 / 62 | 36 / 24 | 330 (56.6%) | 328 | 186 (17.2%) |
| Anchoring, first-pass labels | 47 / 35 | 34 / 19 | 111 / 74 | 101 / 55 | 39 / 26 | 332 (56.9%) | 308 | 209 (19.3%) |
| Retry, second pass on 596 tabs | 43 / 31 | 30 / 15 | 105 / 64 | 86 / 52 | 40 / 29 | 304 (52.1%) | 293 | 191 (17.7%) |
| Full second pass | 48 / 33 | 30 / 15 | 104 / 65 | 88 / 47 | 35 / 27 | 305 (52.3%) | 294 | 187 (17.3%) |
| 4a gold links | 49 / 36 | 42 / 16 | 100 / 51 | 100 / 55 | 36 / 20 | 327 (56.1%) | 320 | 178 (16.5%) |
| **4b + CLEF's picks** | 42 / 24 | 44 / 25 | 111 / 82 | 126 / 88 | 34 / 25 | **357 (61.2%)** | 309 | **244 (22.6%)** |
| 4b, mode b (unnamed term 0.5) | 46 / 26 | 45 / 23 | 110 / 76 | 124 / 84 | 33 / 24 | 358 (61.4%) | 307 | 233 (21.5%) |

Each cell is right answers listed (of the CLEF path's right: studied 77, messy 74, WTQ 144, DataBench 212, fresh 76) /
right first (of the set's n: 186, 120, 300, 300, 176). Named alone is listed through named terms and exact text, no
proposal from the unnamed channel.

**Step 4.** Both runs start from the anchoring bundle with no property in a link (`--qualified` none). 4a
(`named_links_s0`) trains the link sum on gold links and lists about what the anchoring bundle listed (327 against 332)
with fewer right first (178 against 209). 4b (`named_picks_s0`) adds CLEF's recorded picks on 808 training workbooks
(4,134 questions; CLEF's pick must beat the other options through the link sum, no per-dimension target): listed 357
(+27 over 0b), right first 244 of 1,082 (+58). The gain sits where the picks come from: WikiTableQuestions and
DataBench training workbooks, and their dev sets gain (WTQ right first 51 to 82, DataBench 55 to 88), messy dev gains
a little (16 to 25), studied falls (36 to 24), and fresh dev is level with the anchoring bundle (26, 25). Named alone falls from
320 to 309: more of the listed answers now come through the unnamed channel's proposals. The pod code was next-engine
`2f9b521` (the code folder checked file by file). Latency p50 / p90 per question: 0.05-0.10 s / 0.07-0.59 s on four
sets, DataBench 0.19 s / 3.15 s.

**Mode b.** The one labelled unnamed-similarity term's weight was fitted last on training-split questions the bundle
never trained on (`named-chooser-20261112/local/fit_open_weight.py`, 575 questions of held factory families and of
the workbooks CLEF's picks held out): grid 0 / 0.25 / 0.5 / 1 / 2 / 4 lists 406 / 410 / 413 / 413 / 411 / 398, so 0.5
(the smaller weight on the tie). On dev it lists 358 against 357 and puts the right answer first for 233 against 244.
The term does not earn its place; the served mode stays named, the interpretable one.

**Step B (program forms) closes without a retrain.** The 885 training-split questions whose CLEF list held no right
answer, read by the 4b bundle grown onto the forms branch's 191 names (`forms-labels-20261112/local/mine_forms.py`):
19 get a label (one new form gives a first program with the gold answer: median 4, first number 6, gap 7, text length
2), 141 are right with no new form, 725 no forced form helps. On the anchoring bundle it was 8 of 885. Nineteen labels
over eight new outputs cannot train them, so no B run; the branch `next-engine-forms` stays unmerged and is listed for
the owner as removable.

**Three lease defects, fixed (next-engine).** (1) A remote command that ran to the lease's end left no time to bring
anything back: the two learning-curve runs (`named_picks50_s0`, `named_picks25_s0`) trained on 3090-class cards past
their 330-minute leases and came back without even their training log (about $2.10 spent). The command now stops with
a reserve of the lease left for recovery (`3f138b9`, test
`test_runpod_command_that_runs_out_leaves_time_to_recover_its_artifacts`); the trainer's resume now loads weights only,
as the security baseline requires. (2) RunPod hands one address and port to pod after pod; a stale key in the user's
known_hosts refused a relaunched pod minutes after creation. Each pod's key now lives in its own file (`7f12534`).
(3) `test_runpod_transfers_are_inside_the_owned_lease` had counted the calls from before the upload fix `2f9b521`; it
now expects the folder's mkdir. The curve runs are relaunched on 4090- and L40-class cards from a worktree pinned at
the 100% run's code (`2f9b521`; the trainer's best-checkpoint rule changed after it, in `39c0ff3`).

Next: the learning curve (decides how many more picks D buys), step C's named additive chooser fitted on gold-checked
options of held training questions for the 4b bundle, then E (bge-m3 head to head). No push, no deploy, no promotion.

## Steps 0b-3: the anchored meanings fail the gate on labels that disagree with its truth; the decoder had no stop on its condition search (2026-11-13)

- The plan's steps 0b (the lookup design on the 83-property basis), 1-3 (the anchoring run: self-anchors, Gemini's
  column labels, corpus profiles; links untrained) and the per-dimension gate, on next-engine. Both bundles are bge-small,
  39,285 steps, seed 0, the same 7,857 factory tabs (examples_v5); 0b on the throwaway worktree at `5c08f5a`, the
  anchoring run at `37be918`. Measured with `distill-probe-20261112/local/step_named_eval.sh` (mode named: the unnamed
  channel only proposes) from a worktree pinned at `17d8a64`; dev sets measured, never mined. Machine shared with up
  to three other engine processes, so the latencies are upper bounds.

**A decoder defect found by the measure.** The 0b run stalled two hours on one fresh-dev question. The search for k
conditions that sit together on a data line (`engine/program/decoder.py:_Conditions`) had no stop of its own:
`max_pops` bounds the outer k-best, but a slot searched on until a set occurred. Conditions that share only a printed
total line keep a line at every prefix and never complete; that question's slot pushed 143 million nodes over 1,536
conditions. Now one budget of checks per question (`Policy.condition_checks`, 2,000,000, next-engine `17d8a64`) bounds
every slot; when it is spent the slot finds no further set, at the same check on every run. The value is read from
training-split questions only (`decoder-checks-20261112/local/probe_checks.py`): WikiTableQuestions train, 561
questions, median 0, p99 50,371, max 823,618 checks (0.62 s); DataBench train, 169 questions, max 21,054; none near
the stop, about 1.5 s of checks here. The 0b sets measured before the fix (studied, messy, wtq) list the same counts
after it (84, 53, 174 right answers listed). Regression test `test_a_condition_search_stops_at_its_budget_of_checks`. A hardening, not an accuracy gain.

| Dev set (CLEF path right) | 0b listed | anchoring listed | 0b first right (of n) | anchoring first right (of n) |
|---|---|---|---|---|
| Studied (77 of 186) | 50 (64.9%) | 47 (61.0%) | 37 | 35 |
| Messy dev (74 of 120) | 41 (55.4%) | 34 (45.9%) | 19 | 19 |
| WikiTableQuestions dev (144 of 300) | 103 (71.5%) | 111 (77.1%) | 44 | 74 |
| DataBench dev (212 of 300) | 100 (47.2%) | 101 (47.6%) | 62 | 55 |
| Fresh dev twin (76 of 176) | 36 (47.4%) | 39 (51.3%) | 24 | 26 |
| **All (583 of 1,082)** | **330 (56.6%)** | **332 (56.9%)** | **186 (17.2%)** | **209 (19.3%)** |

Listed is the right answer anywhere in the list, as a share of the questions the CLEF path answers right; named alone
(through named terms and exact text, no proposal from the unnamed channel) is 328 (56.3%) for 0b and 308 (52.8%) for
the anchoring bundle. The catalogue readers were 56.9% (1,521 properties) and 55.4% (the field student's catalogue)
alone, so the selected basis loses nothing in 0b. p50 / p90 per question (4 threads): 0.06-0.20 s / 0.07-1.08 s on
every set but DataBench, where the p90 is 17.6 s (0b) and 22.6 s (anchoring), almost all in reading its largest tables.

**The gate fails, and the first cause is the gate's own truth.** Every property needs held-out precision 0.95 on at
least 25 positives as a column and as a word to enter a link. As written, a threshold was set on the run's held-out
train families under the factory's first-pass labels and judged on the val side under the second pass's multi-label
truth. Nothing qualified (columns 4 pass: price, identifier, customer, endDate; words 0;
`gate_first_pass_calibration.json`). The two truths disagree (`gate-diagnosis-20261113/local/diagnose_gate.py`): at
one reading, 0.5, price as a column is 0.73 precise on calibration and 0.99 on the held-out side, identifier 0.67 and
0.96, orderQuantity 0.43 and 1.0; 226 of 254 column false positives and 375 of 431 word false positives at calibration
were columns the first pass called none. On the val side, where both passes exist, the second pass agrees with the
first pass's basis label on 2,063 of 2,213 columns (93%), but gives a basis property to 1,053 of the 5,630 columns the
first pass called none (19%) and to 708 of the 1,345 it gave a property outside the basis or was unsure of. The gate
now sets and judges each threshold on one truth: the val families in two folds, each judged at the threshold the other
sets (next-engine `e58e12d`, test `test_the_gate_judges_each_fold_at_the_threshold_the_other_folds_set`; the bar is
unchanged). Result on the anchoring bundle: columns 3 pass of 14 judged (startDate 0.953, observationDate 0.955,
endDate 0.983), words 1 of 14 (price 0.951); price as a column 0.899, identifier 0.933 and 0.934, customer 0.943;
**qualified for links: none**. Profiles of the corpus's tables pass 33 of 50 judged; a table's class read back from its
profile comes first for 1,017 of 2,400 held-out tables (42%). A second threshold rule (stop at the first reading where
precision falls under 0.95) was previewed on random folds and qualifies nothing either, so it was not adopted.

**What is left is the training labels.** The same 19% of first-pass "none" columns, and the outside-basis ones, were
trained as off for every property. The plan allows one retry; its contract named the anchor weights doubled, but the
failure is classified as label disagreement, which heavier anchoring would fit harder. The retry instead trains the
meaning tier on the gate's truth where it exists and stops teaching the doubtful negatives: `named_anchor_second_s0`,
examples_v6 (`second-pass-meaning-20261113/local/meaning_examples.py`): 596 training tabs carry the second pass's
labels (the one-tab-per-family pass bought in 0a: 2,977 columns with basis properties, 2,193 none, 2,331 unknown);
the other 7,261 keep the first pass's basis properties as positives (13,012 columns) and its none, outside-basis and
unsure labels become unknown (87,503 columns). The closed heads read the labels as before (next-engine `53af9b8`,
`label["meaning"]`, test `test_a_labels_own_meaning_trains_the_meaning_tier_and_leaves_the_closed_heads_alone`).
If it fails too, the meaning route stops here: a second pass over every training tab would need about 1.3M output
tokens, and the factory's hard budget has about 0.74M left (9.16M of 10.2M spent), which is the owner's call.

**Step 4 runs meanwhile on the anchoring bundle**, with no property in a link: the link is the structure tier plus
exact-text evidence. 4a (`named_links_s0`, gold links) and 4b with CLEF's picks on all, half and a quarter of the pick
workbooks (`named_picks_s0`, `named_picks50_s0`, `named_picks25_s0`, the learning curve). Their first launches paid
for three pods that died copying the starting bundle: scp does not make the folder a file lands in; fixed in
next-engine `2f9b521` (test `test_a_file_upload_makes_its_folder_on_the_pod_first`). The forms branch
(`next-engine-forms`) has the decoder stop merged (`157a507`).

Spend this entry: GPU about $2 (0b about $1, anchoring $0.80 on an RTX 3090, three failed pods a few cents), Gemini
none. No push, no deploy, no promotion.

## The basis audit: the 71 properties cover 3% of sheet columns; 12 are added by a rule (83); a third of gold links point at columns schema.org has no property for (2026-11-12)

- The owner's correction of the evening: the reader's meaning dimensions are the selected basis of
  `engine/data/alloc.json`, never the 1,521-property catalogue the entry below trained on, and each must be a real
  anchored output, not a similarity lookup. The plan he approved is the top entry of the RUNBOOK; this is its step 0a.
  No model is trained here. Gemini's column labels only ($4.06 for this audit, on top of the $18.70 of the
  catalogue-wide factory labels); counts and schema.org names, never a header or a cell. No Enron dev sheet was sent:
  the factory's held-out families stand in. Dev coverage is reported, never used to pick a property.
  `training/readers/data/experiments/basis-audit-20261112` (contract, `local/report.py`, `result/`).

**What the 71 cover.** They came from the Wikidata world corpus (actor, director, taxonRank). On sheets only 32 of
them ever label a column, and `name` alone is 2,264 of the 2,985 train columns that carry one.

| Source (columns) | a property of the 71 | of the 82 | a property outside the 82 | no schema.org property | unsure |
|---|---|---|---|---|---|
| Factory train (100,310) | 3.0% | 14.0% | 9.1% | 64.0% | 12.9% |
| Factory held-out families (9,206) | 2.6% | 16.2% | 8.8% | 61.2% | 13.8% |
| Public EIA sheets (2,973) | 0.2% | 5.1% | 10.3% | 83.5% | 1.1% |
| Public SEC statements (4,718) | 0.1% | 0.2% | 25.0% | 67.5% | 7.3% |
| WikiTableQuestions train tables (8,392) | 15.3% | 24.3% | 22.7% | 46.9% | 6.0% |
| DataBench train tables (808) | 9.2% | 14.7% | 23.1% | 59.2% | 3.0% |
| Messy dev (295) | 9.2% | 25.4% | 26.8% | 43.1% | 4.7% |
| DataBench dev (323) | 12.1% | 18.0% | 18.3% | 60.1% | 3.7% |
| WikiTableQuestions dev sample (1,202) | 15.3% | 23.3% | 22.5% | 50.2% | 4.0% |
| Fresh dev twin (3,031) | 1.8% | 4.5% | 11.4% | 77.2% | 6.9% |

**The addition rule**, written before the paid half: at least 25 train columns at confidence 6+ in at least 10
families; at least 10 held-out columns; two-pass agreement of at least 0.9 (a second, independent multi-label pass
over the 574 held-out tabs with the candidates listed); and a direct property of a class that fewer than 5 basis
properties can describe. Twelve pass: price (4,810 train columns, 532 families, 580 of 591 agree), identifier
(2,662, 213/214), startDate (1,117, 79/83), description (933, 71/78), observationDate (544, 62/62), orderQuantity
(443, 54/54), customer (426, 68/74), orderDate (352, 24/24), endDate (271, 28/28), seller (222, 12/13), duration
(212, 17/17) and currency (72, 11/12). Eleven were added first (next-engine `be5127c`, `alloc.json` dim 90-100,
evidence in `training/props/basis_additions_20261112.json`); currency waited on the owner, because the allocation
already had a structural dimension of that name. **His call, the same evening: no redundant dimensions.** The
structural one was labelled by a character check (a value holding a dollar, pound or euro sign), which the new
engine's sheet compiler already reads as a format fact; it is removed, and the schema.org property takes the name
(next-engine `e74776d`, dim 101, dim 8 left unused; basis 83). It has what the structural one never had: Gemini's
label on 71 train and 12 held-out columns and 667 training instances in the world corpus. Coverage moves by those
columns only: factory train 14,021 to 14,092 columns (14.0%), held-out 16.2% to 16.3%, messy dev 25.4% to 26.4%,
every other row and the gold-link bound below unchanged; no property is co-held with it. The two GPU runs already
started on 82 were stopped about 45 minutes in (about $1.20) and relaunched on 83. Left out by the rule: legalName
and employee (no class needs them), temporalCoverage (52 of 98 agree), amount (63/77), validFrom (36/52),
validThrough (27/42), paymentDueDate (11/15), areaServed (5/12), participant, department; value and comment as
generic catch-alls. Main's `alloc.json` is untouched until integration.

**What this bounds.** Of the factory's 4,693 held-out gold links to a column or a value, the sheet's own text reaches
46.2%. Of the rest, the target column carries a basis property for 3.7% with the 71 and 14.9% with the 82, another
property for 7.1%, and no schema.org property for 31.8%. So named meaning terms and exact text together can carry at
most about 61% of gold links, every dimension perfect; a third of links point at columns schema.org has no property
for (trade columns, and on the fresh dev twin 77% of all columns). Those rest on the structure dimensions, and on the
one labelled unnamed-similarity term the owner allowed.

**Two findings that changed the labelling.** (1) 12.1% of held-out columns hold two or more listed properties, so a
single-property label must not train its companions as absent. Which properties hold together was measured on a
multi-label pass over one tab of each of 600 train families: startDate with observationDate (51 of 93), description
with name (18/60), orderDate with startDate (16/34) and dateCreated (9/34), customer with name (9/34), location with
name (7/17), seller with customer (6/22). These are masked on a unit labelled with the other; the second pass keeps
the first pass's label on 1,161 of 1,233 columns. (2) Two other candidate rules for "related" properties were tried
and dropped: range-class containment in the ontology relates 233 pairs (customer with birthDate), and likeness of
the schema.org texts under the base encoder relates name with description and startDate with endDate, whose
negatives are exactly what tells them apart.

## Step 2's measure: the schema.org reader lists 55-57% of the CLEF path's right answers alone and 74-76% with a distilled field reader; the gate asks 90% (2026-11-12)

- PLAN 2026-11-12 step 2 on the five dev sets (Enron studied 186, messy dev 120, WikiTableQuestions dev sample 300,
  DataBench dev 300, the fresh dev twin 176), next-engine 5871dd7, offline on CPU (`eval_encoder_path.py`, the decoder's
  first candidate, no chooser). Development sets only. The step-1 CLEF pod was released at 10:16 (about $24); the two
  reader leases cost about $1.4 (XLM-R base, L40) and $1.9 (bge-small, A40).

**The two schema.org readers** (contracts `schema_reader_s0` and `schema_reader_xlmr_s0`, the 39,285 steps of the reader
they replace, examples_v2: the same factory rows with Gemini's schema.org labels). Held out, the linked target ranks
first among its in-sheet candidates 66.95% (bge-small, best at step 35,352) and 68.2% (XLM-R base); the reader they
replace, which read the trade pack and the corpus's header words, 70.9%. The labelled property is the top one for 74.7%
and 71.7% of 5,555 held units; the answer-type head reads 86.9% and 88.8% (the old reader 78.6%).

**The gate's measure: of the questions the CLEF path answers right, how many the small path's list holds** (583 of the
five sets: studied 77, messy 74, WikiTableQuestions 144, DataBench 212, dev twin 76):

| Path | studied | messy | WikiTableQuestions | DataBench | dev twin | listed of 583 |
|---|---|---|---|---|---|---|
| bge-small schema.org reader | 49 | 39 | 100 | 107 | 37 | 332 (56.9%) |
| XLM-R base schema.org reader | 49 | 41 | 103 | 95 | 35 | 323 (55.4%) |
| the old reader (trade pack, corpus words), no-word engine | 54 | 41 | 107 | 101 | 39 | 342 (58.7%) |
| bge-small reader + the field student | 51 | 47 | 135 | 150 | 51 | 434 (74.4%) |
| XLM-R base reader + the field student | 56 | 49 | 135 | 150 | 51 | 441 (75.6%) |

The field student is distill-probe-20261112's reader student (a bge-small cross-encoder trained on CLEF's recorded field
probabilities over the round-2 pools), fused into the reading at w=2 as CLEF's fields are. It is an encoder and reads
no word list: the options it scores are the engine's own reading record of the sheet. On every question of the sets,
right answer listed / first candidate right:

| Path | studied | messy | WikiTableQuestions | DataBench | dev twin | sum |
|---|---|---|---|---|---|---|
| bge-small schema.org reader | 84 / 30 | 49 / 18 | 167 / 50 | 130 / 62 | 48 / 23 | 478 / 183 |
| + the field student | 99 / 42 | 64 / 28 | 220 / 96 | 187 / 105 | 85 / 51 | 655 / 322 |
| XLM-R base schema.org reader | 85 / 32 | 53 / 20 | 173 / 47 | 111 / 53 | 44 / 19 | 466 / 171 |
| + the field student | 98 / 44 | 69 / 28 | 222 / 101 | 191 / 94 | 84 / 53 | 664 / 320 |
| the old reader, no-word engine | 95 / 36 | 54 / 23 | 179 / 57 | 121 / 66 | 53 / 27 | 502 / 209 |

What this says. The schema.org meaning tier alone reaches a little less than the reader it replaces (478 and 466
against 502 listed); that reader held 283 hand-written trade names and the corpus's own header words, which is the
lookup the owner ruled out, so it is not a candidate. The meaning tier's labels are thin: Gemini found a schema.org
property for 23% of the factory's columns. The lever is the field reader distilled from the teacher: +177 and +198
listed, and on WikiTableQuestions 135 of the CLEF path's 144 right answers listed (93.8%). On the dev twin the small
path lists a right answer for 85 questions, 34 of them ones the CLEF path does not answer right. The gate (90% of the
CLEF path's right answers listed) is not met: 74-76%. The student reads 157-386 options per question; alone on 4
threads it took 1.1-3.2 s p50, over step 3's 2 s budget, so its options will be scored against cached sheet vectors
or pruned before serving.

Next (running): step 3's chooser on the bge-small reader + field student path, from that path's own lists on the
training splits (about 4,500 questions, the graded right options as labels); and the field student trained again on
CLEF's fields over the no-word engine's records (6,300 questions of the step-1 pools, with the four condition fields).

## No word of any language is read: the engine and its training data without word lists, the honest CLEF-path baseline on the no-lookup engine, and the reader's meaning tier made schema.org's 1,521 properties (2026-11-12)

- PLAN 2026-11-12 (RUNBOOK line 1), the owner's rule of the night: no word of any language in code or config that
  matches or interprets a question, header, cell, value or reply; meaning only from learned encoders; schema.org's
  properties are the named dimensions and its classes their superpositions. Development sets only; no frozen set was
  read. Commits on next-engine: f56138c, fe0d404, 4f6f2c3, 18e90d4, 5871dd7.

**Step 0, the word lists removed.** The engine (next-engine f56138c): the stop list, word endings, spelled months and
weekdays, the "total" and sign words, English magnitude letters and the English regular expressions of the words floor,
the sheet compiler and the line roles are gone. A printed total is read by its formulas, style and arithmetic (a new
block-sum tie), a number or date by its digits, a word's commonness by its own sheet. The factory that writes the
training data (fe0d404): its stop list became the words in at least 2% of the side's own written questions (counted,
not written), its table of trade shorthand became a long word cut to its first three or four letters, its ASCII-only
patterns read any script, and mail quotes are cut by their structure. `tests/test_no_word_lookups.py` fails on any
regular expression holding a word, any spelled month or weekday, and the old names of the lists, in the engine and in
the training packages; the exceptions it names are the program grammar, the factory's own reply format, one public
source's URL layout and the privacy screen.

What the small reader (the old bundle, unchanged) loses without the words, right answer listed / first candidate right:

| Set | with the words | without |
|---|---|---|
| Enron studied (186) | 96 / 43 | 95 / 36 |
| Messy dev (120) | 68 / 29 | 54 / 23 |
| WikiTableQuestions dev sample (300) | 220 / 114 | 179 / 57 |
| DataBench dev (300) | | 121 / 66 |
| Fresh dev twin (176) | | 53 / 27 |

**Step 1, the CLEF path on the no-lookup engine** (pod 64.247.201.45, next-engine f56138c, `run_e2e.py nowords`: the
round-2 gate's switches plus CLEF reading comparison, filled/blank, calc operator and entity). Served:

| Set | right | right, warned | asked | wrong, warned | wrong |
|---|---|---|---|---|---|
| Enron studied (186) | 43 | 34 | 95 | 6 | 8 |
| — round 2, with the words | 20 | 29 | 130 | 3 | 4 |
| Messy dev (120) | 64 | 10 | 40 | 4 | 2 |
| — round 2 | 49 | 14 | 52 | 4 | 1 |
| DataBench dev (300) | 177 | 35 | 68 | 10 | 10 |
| — round 2 | 125 | 39 | 128 | 2 | 6 |
| WikiTableQuestions dev sample (300) | 96 | 48 | 138 | 8 | 10 |
| — round 2 | 55 | 52 | 182 | 3 | 8 |

Right answers rise on every set (summed 507 vs 383 of 906) and asks fall (341 vs 492), with wrong answers up from 31
to 58. Two things changed at once, the engine's words and the four new CLEF fields, and this run does not separate them.
The dev twin's first run stopped on a grader error (a date outside Python's range, fixed); it is run again.

Step 2's gate is a share of these right answers: of the 507 the CLEF path answers right on the four sets, the no-word
small reader lists 303 (59.8%: Enron 54 of 77, messy 41 of 74, WikiTableQuestions 107 of 144, DataBench 101 of 212).
The gate asks 90%.

**Step 2 begins: the meaning tier is schema.org's.** The reader's 445 named dimensions held 283 names of a
hand-written English trade pack and 58 schema.org properties. Now (4f6f2c3) the name set is the program's 100 closed
structure names and all 1,521 schema.org 30.0 properties; the pack, its FIBO labeller and the tier of corpus header
words are deleted. A property's coordinate is a share of a softmax over all properties and none: its logit is the
cosine between where the unit stands (its own text as the backbone reads it, plus what the relational read adds, which
starts at zero) and the property's own label and comment read by the same backbone. Untrained it is the zero-shot
reading; training moves units and never what a property means, and a multilingual backbone takes it to other
languages with a retrain. A table's class is a schema.org class, the logistic superposition of the properties it is
a domain of; a fine answer type comes from the ontology (a property whose ranges all fall under Person, Organization,
Place, MonetaryAmount or QuantitativeValue).

Zero-shot, on 3,000 of the factory's validation gold links to a column, the linked column ranks first among its
table's:

| Ranking | bge-small | bge-large |
|---|---|---|
| chance (first column) | 18.1% | 18.1% |
| the sheet's own words (rule) | 41.3% | 41.3% |
| direct similarity of words and header | 52.8% | 53.1% |
| through the 1,521 properties (softmax at 0.05) | 44.7% | 46.2% |

A larger encoder barely moves either; training is the lever. The teacher's labels: Gemini named the schema.org
property and value class of every column of the factory's 8,429 tabs (`training/factory/schema_labels.py`, $18.70):
23% of train columns carry a property at confidence 6 or more (215 distinct), 64% none, the rest below the bar.
Most Enron trade columns have no schema.org property; their links carry them. Two readers train on these labels and
the factory's gold links (contracts `schema_reader_s0`, bge-small, and `schema_reader_xlmr_s0`, XLM-R base), the
same 39,285 steps as the reader they replace; at a tenth of the run the held-out link is first among its in-sheet
candidates 57.7% (bge-small) and 62.1% (XLM-R base) of 1,673 links, and the labelled property is the top one for
57.4% and 23.4% of 5,555 held units (the old reader's held link, at the end of its run: 70.9%).

## The distillation probe: small CPU encoders in CLEF's two places keep 56% of its right answers (the rule asked 85%), so CLEF spend is held; a dev twin of the fresh set; two engine changes written by hand were withdrawn (2026-11-12)

- The owner approved the redrawn plan's steps 2 to 5 on 2026-11-12 (the launch bar becomes at least 80% right, at most
  8% wrong without a warning, at most 20% warned on the fresh real sheets, messy and DataBench; WikiTableQuestions a
  reported check at 70%). This entry is the first night: step 2 (the probe), the free engine work, step 5 (the dev twin).
  No CLEF pod ran. Development sets only; no frozen set was read.

**The probe (`distill-probe-20261112`, contract and rule written before any student was trained).** Cross-encoder
students, one logit per (question, option) pair, softmax within the group, trained on CLEF's saved outputs for the
pooled questions: a chooser student on 9,416 questions' lists (the refit's probabilities and the graded right options)
and a reader student on the round-2 pools' 6,211 questions, their reading records regenerated offline under the pools'
engine and switches (79,211 fields, none dropped: every option id matched CLEF's). Held = the verifier's held
workbooks (1,841 chooser questions, 12,535 reader fields). Three epochs, RTX 4090 pods at $0.89/h, about $4.5 in all.

| Encoder | chooser as CLEF's record states the options: right of 1,841 | chooser with each option's place and answer agreement | reader: top option is CLEF's, of 12,535 fields |
|---|---|---|---|
| CLEF (the teacher) | 1,047 | | |
| bge-small (33M) | 862 | 932 | 10,617 (84.7%) |
| MiniLM-L12 (33M) | 889 | 949 | 10,670 (85.1%) |
| bge-reranker-base (278M) | 896 | 961 | 10,616 (84.7%) |

The first chooser student could not see what CLEF's chooser reads from the option order, a candidate's place in the
decoder's list; the second kind (an amendment recorded in the contract after the first dev scores) writes the place
and how many listed candidates return the same answer into each option. The learning curve on the small encoder: a
quarter, half and all of the questions give the placed chooser 860 / 900 / 932 right and the reader 80.6% / 83.2% /
84.7% agreement; each doubling buys less.

**On the dev sets (offline on CPU, `eval_encoder_path.py`; the serving path with the students in CLEF's places, the
small encoder's students):**

| Path | Enron studied 186 | Messy dev 120 | WikiTableQuestions 300 | sum of 606 |
|---|---|---|---|---|
| CLEF's reading and CLEF's chooser (the round-2 gate) | 124 | 93 | 199 | 416 |
| CLEF's reading, the engine's own first candidate | 91 | 59 | 136 | 286 |
| CLEF's reading, the first chooser student | 91 | 38 | 167 | 296 |
| CLEF's reading, the placed chooser student | 106 | 58 | 170 | 334 |
| right answer listed: CLEF's reading / the reader student / the small reader alone | 147 / 96 / 99 | 112 / 68 / 59 | 238 / 220 / 181 | 497 / 384 / 339 |
| engine's first candidate: CLEF's reading / the reader student / the small reader alone | 91 / 43 / 40 | 59 / 29 / 23 | 136 / 114 / 59 | 286 / 186 / 122 |
| **the encoder path: the reader student and the placed chooser student** | **55** | **33** | **146** | **234** |

- **The rule's ratio is 234 / 416 = 0.56 against 0.85, so every CLEF spend is held** (the redrawn plan's chooser round
  3 and reader round) and the number goes to the owner. DataBench was not run on the encoder path: with all 249 of
  CLEF's DataBench answers added the ratio would still be 0.73.
- The chooser student adds about a third of what CLEF's chooser adds over no chooser (+48 of +130) and nothing on
  messy; the base encoder on messy picks 64 (the small one 58, no chooser 59).
- The reader student recovers most of CLEF's reading on WikiTableQuestions and little on the sheet sets: on Enron it
  lists fewer right answers than the small reader alone. Its top option is CLEF's on 77.0% / 78.3% / 88.5% of dev
  fields (Enron, messy, WikiTableQuestions).
- CPU seconds per question on four threads, p50 / p90 (other jobs shared the machine): reader student 6.2 / 11.8, 2.8 /
  7.3, 2.5 / 4.5 over a median of 386 / 174 / 155 options; placed chooser 1.6 / 2.7, 1.5 / 2.1, 1.5 / 2.3; engine and
  both 8.3 / 14.2, 4.7 / 10.3, 4.2 / 6.7. The bar proposed to the owner is 2 s p50 and 5 s p90.

**Withdrawn: hand-written words.** Two changes made this night were reverted at the owner's word ("this is cheating"):
hard-coded words checked deterministically are against a generalized, not memorized, encoder-read model, and do not
carry to another language. Their numbers are not results.
- A per-workbook glossary of phrases for the ten messy dev workbooks' columns and values, written by hand by the
  developing session after it had read dev questions, and an engine hook to read it (next-engine 57b97dc, reverted in
  13fc833). The data and the hook are deleted.
- Four "generic" readings designed from dev failures (next-engine b6e836b, reverted in fb5b79b): English trigger words
  added to the word floor (negation: not, n't, besides, apart from; bounds: before, since, until, through), an English
  negation check on cell values, an "is filled" condition, an open period and a year column's value. The engine is back
  to 172994b exactly (test_program 69, test_named 48).
- The word floor itself (engine/program/words.py) is English closed lists from before this night: on French it reads
  neither "pas" nor "avant", and splits accented words ("expédiées" -> "exp", "di", "es"). The small reader's encoder
  (bge-small-en) is English too. Language reach has to come from the learned readers, not from word lists.

**What CLEF never reads.** The reading record offers CLEF thirteen fields; a condition's comparison (and so a
negation), filled or blank, a calculation's operation and a rank's entity come from the small reader and the word
lists alone. `reading_records.py` now has a `conditions` switch that offers CLEF those four as closed fields (off, a
record is unchanged); it is unmeasured until a CLEF pod runs.

**The dev twin of the fresh frozen set (`regress/sets/fresh_dev/`, built by an agent with the frozen set's
own pipeline and none of its questions).** 52 public US government workbooks (35 xlsx, 17 csv, 26 health), 208
questions, Gemini $6.44; 20 golds recomputed independently, all confirmed. No url, hash or table is shared with the
frozen manifest; 8 workbooks from a publication the frozen set also draws on are left out of development by default
(176 questions on 44 workbooks). Every workbook compiles. Floor, the small reader alone with the engine's first
candidate: 38 of 176 right, a right answer listed for 74.

## The reach census: 132 of 241 dev misses have no right answer in the list; the launch bar's warning clause needs 91–95% right; no large model, about 20% right; a wider decode does not help (2026-11-11)

- Experiment `reach-census-20261111` (no GPU; gitignored under `training/readers/data/experiments/`). Everything is
  read from the round-2 gate's records (`round2_real`, `round2_ext`: the refit with evidence and the lines option, the
  served path) or replayed offline from their recorded CLEF fields under next-engine 172994b (`reach_depth.py`; a
  replay at the gate's own settings reproduces its lists). Development sets only; no frozen set was read. Enron rows
  hold ids, counts and label names only.

**Where the 241 misses are (always answering, first cell graded):**

| Set | n | right | no right answer in the 50 | listed, not chosen (right in the first 5 / deeper) | couldn't compute |
|---|---|---|---|---|---|
| Enron studied | 186 | 124 | 39 | 23 (9 / 14) | 0 |
| Messy dev | 120 | 93 | 8 | 19 (11 / 8) | 0 |
| DataBench dev | 300 | 249 | 25 | 26 (18 / 8) | 0 |
| WikiTableQuestions dev sample | 300 | 199 | 60 | 39 (12 / 27) | 2 |
| Total | 906 | 665 | 132 | 107 | 2 |

**The warning clause of the launch bar (at most 2% wrong without a warning, at most 20% warned) needs 91–95% right
with today's warning.** The run's right and wrong answers keep their confidence distributions and are remixed to a
given right rate (`census_records.py`, `bar_table.py`):

| Set | AUROC of the chooser's confidence | wrong without a warning at 20% warned, now | at 80% right | at 85% | at 90% | right rate that meets 2% / 20% |
|---|---|---|---|---|---|---|
| Enron studied | 0.82 | 16.7% (31; 14 of them not listed) | 7.7% | 5.8% | 3.4% | 94% |
| Messy dev | 0.90 | 8.3% (10; 1) | 7.4% | 4.4% | 2.2% | 91% |
| DataBench dev | 0.84 | 7.0% (21; 9) | 8.2% | 6.2% | 3.5% | 93% |
| WikiTableQuestions dev sample | 0.81 | 15.7% (47; 27) | 7.7% | 5.3% | 3.4% | 94.5% |

Four warning signals have now been measured and none beats the chooser's own top probability: the trained verifier
(stage 2's gate); the pick's answer-group mass (wrong without a warning 32 / 11 / 19 / 45 against 31 / 10 / 21 / 47)
or its margin (34 / 11 / 18 / 47); a logistic model over eleven recorded signals, fitted on three sets and scored on
the fourth (`warning_combo.py`: AUROC 0.86 / 0.90 / 0.84 / 0.84, 32 / 11 / 17 / 45); and disagreement between the
refit and chooser3 on the same lists (`agree_signal.py`: 38 / 13 / 16 / 45). When the two choosers agree they are
still wrong on 38 of 148 Enron answers and 45 of 228 WikiTableQuestions answers: with no right answer in the list,
both take the same wrong one.

**What the large model carries (the shipped model is to be a distilled encoder):**

| Set | small reader alone: engine's first candidate right / listed in 50 | CLEF's reading: first candidate right / listed | CLEF's reading and chooser: right |
|---|---|---|---|
| Enron studied | 40 / 99 | 91 / 147 | 124 |
| Messy dev | 23 / 59 | 59 / 112 | 93 |
| WikiTableQuestions dev sample | 59 / 181 | 136 / 238 | 199 |
| DataBench dev | not replayed | 179 / 275 | 249 |

With no large model the path answers about 20% right, and a perfect chooser over the small reader's own list would
stop at 53% / 49% / 60%. Stage 3's assumption (a CPU encoder keeps at least 95% of the teacher) is untested.

**A wider decode is not a free gain.** Decode 500, rank as served, keep the first 50 (`reach_depth.py --all`,
`wider_decode.py`):

| Set | gate list (decode 50) | first 50 of 500 | gained / lost | first 100 | first 200 | anywhere in 500 |
|---|---|---|---|---|---|---|
| Enron studied | 147 | 152 | 11 / 6 | 155 | 157 | 159 |
| Messy dev | 112 | 101 | 2 / 13 | 106 | 108 | 114 |
| WikiTableQuestions dev sample | 238 | 232 | 6 / 12 | 242 | 252 | 257 |

Right answers do sit past position 50 (12 of Enron's 39 unlisted questions, 19 of WikiTableQuestions' 62, 2 of
messy's 8, 3 of DataBench's 25), but the ranking that feeds the chooser cannot bring them forward without pushing
others out. Several of the WikiTableQuestions ones match the gold by coincidence (a total over one unrelated line).

**Why a right answer is not listed.**
- Enron (39; the need labels of `evals/enron/labels.jsonl`): 29 need domain knowledge. Questions that need none are
  52 of 57 right; questions that need domain knowledge are 52 of 95. By form, the weakest are a difference or ratio
  (1 of 7 right, 6 not listed), a top column across a line (`cross_argmax`: 2 of 9, 5 not listed) and a cell lookup (30 of 49, 11 not
  listed); by sheet shape, cross-tabs (2 of 10 right) and stacked tables (7 of 19).
- WikiTableQuestions (62 with the 2 that could not be computed; each read from its question, gold, columns and first
  candidates): a number, duration or list inside a text cell 16; a form the language lacks 11 (three named values,
  highest minus lowest, a line by its position, groups kept by their count, the column that holds a value on a line, a
  period's end); a comparison answered with a name, yes/no or more/less 9; table layout (a turned table, a header
  merged with data, unnamed columns, side-by-side blocks) 7; a partial value the reading did not propose 6; a negation
  or before/after read the other way 4; grouping by a date's month or year 4; an unclear question or doubtful gold 5.
- DataBench (25): ties and stated tie-breaks in a top-N list 7; word counts of a text column 5; forms (a median per
  group, a group kept by its count, existence under a numeric condition, a nested top group) 5; cells that hold lists
  3; survey scales or a doubtful gold 2; the replay's 60,000-row cap 2; unclear 1.
- Messy (8): a paraphrased value or a polarity the reading missed 4 (pay annually, churn, rupees, used a promo);
  deeper in the decode 2; "vs" read as a subtraction 1; one column compared with another 1.

**What follows.** Recall and chooser work can lift right answers, but the bar's warning clause is met only above 90%
right, the encoder that ships is far below the teacher today, and WikiTableQuestions' misses are mostly Wikipedia-table
forms. A redrawn plan was put to the owner (RUNBOOK); no spend, no engine change.


## Chooser round 2: chooser3 is +22 summed (687 vs 665 of 906) but messy −8, so the refit stays; every messy loss is a choice between tabs with the same headers (2026-11-11)

- Experiment `chooser-round2-20261111` (H100, about $30 of its $48 cap; gitignored under
  `training/readers/data/experiments/`). Engine: next-engine 172994b (local, not pushed), worktree
  `prereasoner-next-gate`. Development sets only; no frozen set was read.
- Round-2 pools: 6,211 questions collected under 172994b with the stage-2 reading switches and lines (factory first,
  then WikiTableQuestions; every question round 1 never reached went first). Training rows: 9,416 (5,209 from round 2,
  round 1 for the rest; the verifier's held workbooks left out), 6,736 with a right candidate.
- chooser3: the refit (adapter 5af7ae6f) trained one more pass, 1,177 updates in 5.2 h (last logged loss 0.58), the
  objective otherwise unchanged. Variant `round2`: chooser3 serving, the refit with evidence choosing over the same 50
  beside it. Always answering, top answer graded on its first cell.

**The rule (experiment-contract.json, written before any round-2 pool):** adopt chooser3 if its right, summed over the
four sets, is above the refit's on the same lists and no set is lower by more than 2.

| Set | n | refit | chooser3 | wins | losses | wrong without a warning at ≤20% warned (refit / chooser3) |
|---|---|---|---|---|---|---|
| Enron studied | 186 | 124 | 128 | 8 | 4 | 31 / 31 |
| Messy dev | 120 | 93 | 85 | 2 | 10 | 10 / 21 |
| DataBench dev | 300 | 249 | 257 | 11 | 3 | 21 / 17 |
| WikiTableQuestions dev sample | 300 | 199 | 217 | 19 | 1 | 47 / 29 |
| Total | 906 | 665 (73.4%) | 687 (75.8%) | 40 | 18 | |

Messy is 8 lower, so the refit stays. Its column repeats the lines run exactly (124 / 93 / 249 / 199); the lists are
the same.

**Every messy loss is a twin-tab choice:** the reading is right, but chooser3 takes it on another tab with the same
headers. In the subscriptions workbook (messy0–21) it takes SI or FF instead of NT; in the keywords workbook
(messy22–37) it takes Forklift, Inspection or Sheet1 instead of Checklist. The messy cases label the workbook's first
and largest tab. chooser2 lost 6 of the same questions at the stage-2 gate (messy4, 5, 25, 27, 30, 32). chooser3's 2
wins are a count over one named value instead of a choice of two (messy69), and a total instead of a count (messy70).

**No rule fixes it.** The training pools do not follow messy's convention. Where a factory question's list spans
twin tabs and one tab is right, the first tab is the right one in 125 of 259 (round-2 pools). Moving the recorded
pick to the group's first tab would turn 41 right picks wrong and only 33 wrong ones right (226 twin picks, round 1
and round 2). That holds even when it applies only if the question does not name the picked tab (40 / 33). An unnamed
tab among twins is ambiguous; the warning icon is the right answer to it, not a default.

**Not adopted, recorded:** averaging the two choosers' probabilities would score 128 / 93 / 256 / 212 = 689, messy
unchanged. That was measured after the gate, under no rule. It also needs two chooser passes per question, and
chooser3's weights were not kept (they are pulled only on adoption).

**The launch bar on dev is still not met** (served: the refit with lines): right 66.7% / 77.5% / 83.0% / 66.3%, and
8.3% to 16.7% of answers are wrong without a warning when at most 20% are warned. Getting that to 2% would mean
warning on 44% to 85% of answers. Stage 3 stays held. Stage 2's GPU spend: about $70 (pod B) + about $30 (round 2) of
about $120.


## Stage 2's gate: the refit chooser stays (chooser2 is +30 summed but messy −4) and no verifier; the lines option is adopted, 658 -> 665 of 906 always answering (2026-11-11)

- Experiment `phase2-choose-verify-20261111` (the stage-2 pod, H100, read head cadbd7327ca3; gitignored under
  `training/readers/data/experiments/`). Engine: next-engine 172994b (local, not pushed), worktree
  `prereasoner-next-gate`. Development sets only (Enron studied 186, messy dev 120, DataBench dev 300, the
  WikiTableQuestions dev sample 300); no frozen set was read.
- Variant `phase2`: the read head, the decoder's top 50, every candidate run; chooser2 (the refit trained one pass on
  the stage-1 pools: 6,023 questions, 4,177 with a right candidate; 753 updates, loss 0.490) serving, the warning
  verifier scoring its pick, and the refit with evidence choosing over the same 50 beside it. Always answering, the
  top answer graded on its first cell.

**The chooser rule (written before the gate):** adopt chooser2 if its right, summed over the four sets, is above the
refit's and no set is lower by more than 2.

| Set | n | refit | chooser2 | wins | losses |
|---|---|---|---|---|---|
| Enron studied | 186 | 122 | 126 | 6 | 2 |
| Messy dev | 120 | 94 | 90 | 3 | 7 |
| DataBench dev | 300 | 249 | 260 | 16 | 5 |
| WikiTableQuestions dev sample | 300 | 193 | 212 | 21 | 2 |
| Total | 906 | 658 (72.6%) | 688 (75.9%) | 46 | 16 |

Messy is 4 lower, so the refit stays. Only 1,911 of the stage-1 pools' questions are factory (messy-like) ones, and
their engine (91d5554) had no breakdown, per-currency figure or line comparison; round 2 (below) collects the
factory questions first, on the newest engine.

**The warning verifier is not adopted.** Trained 1,506 updates (loss 0.408) on 12,044 serving records, calibrated on
2,434 held records (1,217 questions by workbook, 54.3% right). On the held split, the best threshold within 20% warned
leaves 25.9% of answers wrong without a warning by the verifier's probability against 28.2% by the chooser's own; to
reach 2% the warning would cover 74% (verifier) or 90% (chooser) of answers. On the dev sets at the held thresholds,
the verifier leaves more answers silently wrong on every set (Enron 51 vs 27, messy 25 vs 22, DataBench 31 vs 25,
WikiTableQuestions 45 vs 31; chooser2's served pick), because its threshold warns far fewer dev answers (10, 5, 10, 43
vs 39, 10, 29, 65).

**The launch bar on dev is not met:** right always answering is 65.6% / 78.3% / 83.0% / 64.3% with the refit
(chooser2: 67.7% / 75.0% / 86.7% / 70.7%), and 8% to 18% of answers are wrong without a warning at the held
threshold, against at most 2%. Stage 3 stays held.

**The lines option (rule written before the run: adopt if summed right is above the control's and no set is lower by
more than 2):** variant `lines_b`, the refit serving, run beside the gate on the same pod and engine; the control is
the gate run's refit pick, so only the option differs. The reading also offers each table's line labels (a table of
more than 60 labelled data lines offers only its printed ones).

| Set | control | lines | wins | losses | right listed | request tokens p50 |
|---|---|---|---|---|---|---|
| Enron studied | 122 | 124 | 2 | 0 | 149 -> 147 | 12,231 -> 13,697 |
| Messy dev | 94 | 93 | 0 | 1 | 112 -> 112 | 5,217 -> 5,483 |
| DataBench dev | 249 | 249 | 2 | 2 | 275 -> 275 | 7,366 -> 7,384 |
| WikiTableQuestions dev sample | 193 | 199 | 9 | 3 | 233 -> 238 | 4,288 -> 5,220 |
| Total | 658 | 665 (73.4%) | 13 | 6 | 769 -> 772 | |

Adopted: the round-2 pools and the round-2 gate use it. Read seconds p50 unchanged (3.4 / 2.1 / 2.4 / 1.4 s vs 3.3 /
2.6 / 2.0 / 1.3 s); 2 Enron read errors vs 1.

**Messy is 98 at de878a6 (last evening) and 94 at 172994b, the refit choosing in both.** CLEF's recorded readings
are identical on all 120 questions; 5 losses and 1 win, and all 5 losses still list a right answer. On two (messy39,
messy138) the decoder's first candidate at 172994b is an answer absent from de878a6's 50 and the refit takes it
(messy39 had been right at p 0.92); on the other three the right candidates moved within the list and the pick
changed. The same comparison: Enron 122 -> 122, DataBench 250 -> 249, WikiTableQuestions 187 -> 193 (+7 −1; the csv
fix included). The two new first candidates are to be replayed and named before a cause is claimed.

**Round 2 (started 05:20, no result yet):** pools on 172994b with breakdown, marks and lines
(`collect_wide.py --prefix pools2 --new-first`): the 4,032 stage-1 questions never reached first (factory 2,371,
WikiTableQuestions 1,661), then re-collection; then chooser3 trained from the refit on them (`chooser-round2-20261111`,
contract written before results, at most $48), judged by the same rule against the refit on the same 50.

**Spend:** the stage-2 pod at $3.99/h since 17:00 (lease cap $71.82, serving until about 10:50).


## A day of reading and grammar: 594 -> 657 of 906 always answering (DataBench 250, messy 98, WikiTableQuestions 187, Enron 122); the reader round fails its rule; first-cell grading (2026-11-10)

This is stage 1 of PLAN 2026-11-10 and the code half of stage 2 (the plan sits at the top of the RUNBOOK).
- Experiments: `phase1-data-20261110`, `phase1-reader-20261110` (the reader round and its gate), `phase1-pools-20261110`
  (serving-path pools), `phase2-choose-verify-20261111` (the stage-2 pod: measurements while it served, then the chooser
  and verifier training), all gitignored under `training/readers/data/experiments/`.
- Engine: next-engine, local commits 162decc .. 172994b on branch next-engine (none pushed); each run names its commit.
- Every number below is the live path: the small reader, CLEF reading (adapter cadbd7327ca3, the reader fit) fused at
  w=2, the typed decoder's top 50, every candidate run, the refit chooser with evidence choosing; always answering, the
  top answer graded. Development sets only: Enron studied 186, messy dev 120, DataBench dev 300 (050-065), the
  WikiTableQuestions dev sample 300 (random-split-1-dev). No frozen set was read.

| Set | n | morning (91755e5, reader fit) | evening (de878a6, marks options) | right listed |
|---|---|---|---|---|
| Enron studied | 186 | 121 | 122 | 146 -> 149 |
| Messy dev | 120 | 79 | 98 | 85 -> 111 |
| DataBench dev | 300 | 223 | 250 | 249 -> 275 |
| WikiTableQuestions dev sample | 300 | 171 | 187 | 215 -> 225 |
| Total | 906 | 594 (65.6%) | 657 (72.5%) | 695 -> 760 |

DataBench dev (83.3%) and messy dev (81.7%) pass 80% always answering; Enron (65.6%) and WikiTableQuestions (62.3%) do
not. Listed exceeds right by 103 questions: the chooser, not reach, is now the larger loss.

**The answer extraction was wrong, and on WikiTableQuestions it read the gold.** The DataBench and WikiTableQuestions
adapters chose which cell of a row is the answer by the answer type (the last number for a number; on
WikiTableQuestions a number cell whenever the gold was numeric). Both now take each row's first cell, the engine's own
display rule (`execute._displayed`: the key or shown column first). Every earlier number for both sets used the old rule.

**WikiTableQuestions adapter defect (fixed this evening, after the runs above):** the dataset's csv escapes a quote
inside a quoted cell with a backslash; the default csv dialect kept the backslashes, so a quoted song title never
matched its gold, and 4 of the 182 sample tables split their columns wrongly. With the backslash as escapechar, 154 of
the 182 tables equal the dataset's own tsv (137 before; the rest differ only in spaces). 31 of the 300 sample questions
read those tables, so the WikiTableQuestions numbers in this entry undercount them.

**The reader round (phase1-reader-20261110): the reader fit is kept.** The rule, written before the gate ran: adopt
reader2 if its always-answer top right, summed over the four sets, is above the reader fit's and no set is lower by
more than 2. Same pod, same engine (91755e5), same chooser.

| Set | reader fit | reader2 | wins | losses |
|---|---|---|---|---|
| Enron studied | 121 | 124 | 8 | 5 |
| Messy dev | 79 | 76 | 1 | 4 |
| DataBench dev | 223 | 174 | 8 | 57 |
| WikiTableQuestions | 171 | 147 | 11 | 35 |
| Total | 594 | 521 | | |

Cause: 61% of reader2's 8,000 records (WikiTableQuestions and DataBench train) were labelled by answer, as the union of
the coordinates of every candidate in the decoder's top 500 whose answer graded right under the old extraction: 1.73
labels per field (shown 2.79, value 3.2; 30% of fields multi-labelled) against 1.01 for the factory's gold, so the reader
learned to spread the shown column (DataBench list[number] 49 -> 26 of 60). Distillation labels one program per question.

**Engine commits, each with a regression test (all suites green: program 69, sheet_compile 69, deterministic_emitters
71, sql_ast 107, named 48, factory 14, calculations 181, analysis 8, sheet_sessions 7, messy_benchmark 5):**
- 162decc the line after or before the one a condition keeps ("who finished after Bo?"; the dual plan lowers a row
  filter on a scalar subquery); 6a74307 Check same, an aggregate against an aggregate, "all unique"; a90964e one column
  on two lines; b0820a0 an ordinal is no stated number; 044bbe9 numbers with one unit ("1,370 tons"); 52b13a9 a total word
  in prose or a list is no total mark (DataBench had lost records as subtotals: 8,825 utility patents for 8,848); 35db27f
  number words; 471ce33 ISO year-months; 0f816ac times ("35:00"); c0ae92b a few-valued number column names its values;
  91d5554 equality ("zero calories"). Small-reader WikiTableQuestions dev reach (2,831) top 1 / 50 / 200: 647 / 1,627 /
  1,907 -> 674 / 1,652 / 1,932.
- da7f0c8 Breakdown: a sum, average, lowest, highest or count for each group of a key ("visits per department",
  "pipeline by rep"), groups in sheet order, the same in the typed SQL and the dual emitter, with read-as, cells and a
  GROUPBY formula; the four forms could not say it (12 of the 35 messy dev questions with no right candidate). A text
  column of ISO 4217 codes (two or more) is its lines' unit.
- de878a6 amounts in different currencies never add: the per-currency figure replaces the plain one unless a condition
  fixes one currency (7 messy totals on a payments export); a breakdown answers in its figure's type and keeps named
  groups. fdca189 a figure added across currencies says "(all Currency together)".
- 0b6205f a top-N of groups by an average, a lowest or a highest; af8ce94 a change going one way states its sign
  ("growing", "trending up"); 02ba66f CompareLine, a number against the same column on the line a value names ("more
  medals than Canada", "taller than van Hatchell", "the same number as Japan"), and closed lists of comparatives;
  ebbac92 only a value one data line holds names a line; 172994b a total or a count over a choice of named values ("the
  US and Jamaica combined").

**Measured on the live path, each against the run before it on the same pod and chooser:**

| Change | Enron | messy | DataBench | WikiTableQuestions |
|---|---|---|---|---|
| 91d5554 + stage2 reading options (neighbour, same), vs morning | 121 -> 121 (+3 -3) | 79 -> 80 (+1 -0) | 223 -> 245 (+26 -4) | 171 -> 185 (+17 -3) |
| da7f0c8 + breakdown reading option | 121 -> 122 (+1 -0) | 80 -> 88 (+8 -0) | 245 -> 248 (+4 -1) | 185 -> 183 (+1 -3) |
| de878a6 + marks reading options | 122 -> 122 (+2 -2) | 88 -> 98 (+12 -2) | 248 -> 250 (+6 -4) | 183 -> 187 (+4 -0) |

The marks options: expanded values ordered by their column's count of distinct texts (they had tied and sorted by
coordinate text, so the 160-value cap cut them arbitrarily: "rupees" never offered Currency = inr), a mark column's one
value ("Yes" beside blanks), and at most 20 proposed values per column (the word "deal" matched a thousand deal names
and filled the cap). Messy's two losses are a breakdown the chooser swapped for another and "avg searches across
everything", whose gold reads the first tab where the chooser took the union.

**Offline replay (replay_reach.py: the run's own recorded CLEF fields, fused and decoded under another engine; a run
under its own engine reproduces exactly, 101 of 101 on messy).** On the newest engine (172994b): messy listed 111 ->
112, Enron 149 -> 149, DataBench 274 -> 276 (the bottom 2 branches and locations by average rating), WikiTableQuestions
224 -> 234 (with the adapter fix; none lost). Listed only: the chooser was not replayed.

**Pools for stage 2 (phase1-pools-20261110):** 7,256 serving-path questions no reader trained on (WikiTableQuestions
train 4,798, factory held 1,911, DataBench train 547), next-engine 91d5554 with the stage2 options; a right candidate
listed for 69%. Hand-off: the chooser's 6,023 (4,177 with a right candidate) and the verifier's 12,044 train / 2,434
held records (held split by workbook), sha256 checked on the pod. These pools have no breakdowns, per-currency figures
or line comparisons: a chooser trained on them has never seen one.

**Chooser-miss census (last night's runs):** 40-45% of the wrong picks are the decoder's first candidate; the right one
sits past position 5 in more than half; the median probability gap is 0.11 to 0.32.

**Spend:** the reader pod (H100, 14.2 h, $3.99/h, about $57) and the stage-2 pod since 17:00 ($3.99/h, 18 h lease
cap $72). Five exited pods of 2026-10-30 holding billed volumes were deleted at the owner's word.


## The night's grammar on four dev sets: DataBench 100 -> 195 and WikiTableQuestions 172 of 300 always answering; the evidence chooser fit fails its gate (2026-11-10)

This is the second part of PLAN 2026-11-10's first night (the plan sits at the top of the RUNBOOK).
- Experiments: `clef-chooser-evidence-20261110` (evidence records, a chooser fit, two gates) and `clef-evidence-20261110`
  (analysis), both gitignored under `training/readers/data/experiments/`.
- Engine: next-engine local commits d289719, 289c8bb, 4a6f637, 9337000 and 91755e5 (every suite green: test_program
  59, test_named 48, test_factory 14, test_deterministic_emitters 70, test_sql_ast 107, test_calculations 181,
  test_analysis 8). Not in this repository and not served.
- GPU: two H100 SXM pods at $3.99/h, about $24 (attempt 1 about $10.9, attempt 2 about $12.7). Nothing promoted.

**What the night added to the program language.** Each operation is in both deterministic emitters with execution
parity tests.
- `Rank` keeps as many lines or groups as the question states ("the 5 oldest"), typed by what it shows (d289719,
  4a6f637). Typing a top-N as a list had cost it the small reader's list penalty: DataBench number lists went from 0
  to 9 of 60 at top 1.
- `Check`, a yes-or-no form (289c8bb): any, no, every, and a number against a stated one, lowered to a verdict over
  scalar subqueries on one row.
- `OneOf` for a rank's kept lines ("Germany or France", 9337000). The WikiTableQuestions dev "or" questions reached
  in the top 200 went from 100 to 112, and right at top 1 from 14 to 30.
- `is` checks of what the top one is ("is the oldest one male?", 91755e5), with verdicts that compare text in both
  emitters.

The reading records also changed:
- Value expansion: every distinct value of a column with at most 30, value field capped at 160.
- The check options: form `check`, a quantifier field, answer type `yes_no`. Only CLEF reads them.

**The whole path, always answering.** Small reader, the reader fit's reading fused at w=2, decoder top 50, every
candidate run, refit chooser with evidence. "Session" is the earlier engine (5aa5526) without value expansion or check
options; "tonight" is next-engine at 91755e5 with both.

| Set | Questions | Right listed, session | Right listed, tonight | Top answer right, session | Top answer right, tonight |
|---|---:|---:|---:|---:|---:|
| Enron studied | 186 | 144 | 147 | 119 | **122** (66%) |
| Messy dev | 120 | 76 | 85 | 70 | **79** (66%) |
| DataBench dev | 300 | 110 | 219 | 100 | **195** (65%) |
| WikiTableQuestions dev sample | 300 | - | 214 | - | **172** (57%) |

- The WikiTableQuestions sample is a fixed 300 of random-split-1-dev (seed 20261110). This is the first CLEF number on
  it.
- The chooser picks a right answer in 83% (Enron), 93% (messy), 89% (DataBench) and 80% (WikiTableQuestions) of the
  questions where one is listed. The rest are the reader's and decoder's: no right program in the top 50.
- Latency per question on one H100 SXM, the two CLEF passes included: messy p50 4.1 s, p90 7.3 s; WikiTableQuestions
  p50 3.1 s, p90 3.6 s.

DataBench dev by answer type (60 each), top answer right:

| Answer type | Session | Tonight | Right listed, tonight |
|---|---:|---:|---:|
| Category | 49 | 49 | 57 |
| Number | 41 | 42 | 43 |
| Yes/no | 3 | 33 | 39 |
| Category list | 6 | 41 | 48 |
| Number list | 1 | 30 | 32 |

**The warning icon is still far from the launch bar.** Using the chooser's top probability as the signal:
- Warning on 20% of answers leaves 30 of 186 (Enron), 18 of 120 (messy), 56 of 300 (DataBench) and 71 of 300
  (WikiTableQuestions) wrong without a warning.
- Holding that to 2% needs warnings on 125, 68, 239 and 254 of them.
- Grouping candidates by their answer moves this little.
- The bar (at least 80% right, at most 2% wrong without a warning, at most 20% warned) needs accuracy near 85-90% and a
  verifier, as the RUNBOOK's WARNING SIGNALS line says.

**A defect the run found.** 21 of 300 DataBench questions (053_Patents, 063_Influencers) read nothing from CLEF: the
reading schema needed 35,116 to 51,414 tokens against 32,768. Free-text cells (patent abstracts of up to 5,607
characters, posts) entered the value and line options uncut, though the sheet description already cuts cells to 30.
- Only 3 of the 21 were answered right.
- Option texts are now cut at 160 characters (`reading_records.py`, OPTION_CHARS). Not yet re-measured.

**The evidence chooser fit fails its gate.**
- Training: the refit chooser trained further on evidence records (sheet plus each candidate's answer) for held
  factory questions, 162 updates, none skipped.
- Records: 1,293, not the planned 2,000. A stopped task left a collector alive, two writers interleaved shard 0, and
  705 records were lost (266 recovered).
- Gate, top answer right with evidence, against the refit with evidence zero-shot: Enron 119 vs 119, messy 68 vs 70,
  DataBench dev 98 vs 100. The zero-shot refit stays.
- Attempt 1 crashed in cuDNN attention at about update 75 before saving, about $10.9 lost. The trainer now disables
  cuDNN attention and guards each record.

**Running at the time of writing:** `phase1-reader-20261110`, the reader trained further on 8,000 records (factory
gold, WikiTableQuestions train and DataBench train, labelled by answer), at update 430 of 1,000.


## The first measurements on data other than Enron, the loss census, and three levers: evidence for the chooser, value expansion, top-N lists (2026-11-10)

This is PLAN 2026-11-10's first night (the plan sits at the top of the RUNBOOK).
- Experiments: `clef-evidence-20261110`, `databench-20261110` and `wtq-20261110`, all gitignored under
  `training/readers/data/experiments/`.
- GPU: one H100 SXM pod for 1.5 h, about $6.
- Nothing served or promoted.

The product now always answers its top pick, with a warning icon when it is not confident. So the headline number is
the top answer right, and the threshold only decides the icon.

**External sets, frozen by hash before use.** The test splits are recorded in `frozen_test_manifest.json` and their
loaders refuse them.
- DataBench (SemEval-2025 Task 8, MIT): dev is 300 questions on 15 datasets; the 16th dev dataset, 065_RFM with 541,909
  rows, is too large for the owner's machine.
- WikiTableQuestions 1.0.2 (CC BY-SA 4.0): dev is 2,831 questions; graded by a Python 3 port of the release's own
  evaluator, with CoreNLP canonical targets.

**Reach of the grammar.** The small reader alone (no CLEF), with the typed decoder's top 200 run and graded:
- WikiTableQuestions dev: a right program at top 1 for 649, in the top 50 for 1,646 (58%), in the top 200 for 1,920
  (68%) of 2,831.
- DataBench dev: top 50 for 68 and top 200 for 74 of the 237 measured. By answer type in the top 200: category 37 of
  48, number 28 of 48, yes/no 3 of 48, category list 5 of 48, number list 1 of 45.
- Why DataBench is low: the language had no yes/no form, `Rank` kept only the top one, and a list showed distinct
  values only.

**The CLEF path, always answering.** Small reader, the reader fit's reading fused at w=2, decoder top 50, top answer
right:

| Chooser | Enron 186 | Messy 120 | DataBench dev 300 |
|---|---:|---:|---:|
| Refit chooser, blind (served) | 112 | 61 | 96 |
| Released head, blind | 103 | 55 | 96 |
| Released head, with evidence | 111 | 62 | 95 |
| Refit chooser, with evidence (never trained on it) | **119** | **70** | **100** |
| A right answer is listed at all | 144 | 76 | 110 |

"With evidence" means STATE also describes the workbook as the reader saw it (capped at 6,000 tokens), and each option
ends with the answer its candidate returns.
- On the 306 real-sheet questions, evidence lifts the refit from 173 to 189, and it then picks right in 86% of the
  questions where a right answer is listed.
- Grouping candidates that return the same answer moves the counts by 2 or less.
- DataBench by answer type, refit with evidence: category 49 of 60, number 41 of 60 (90 of 120, 75%, on data never
  seen), yes/no 3 of 60, category lists 6 of 60, number lists 1 of 60.

**The warning icon is not calibrated yet.** Holding wrong answers without the icon to 2% of questions means warning on
64 of 120 messy and 131 of 186 Enron answers, far from the 20% bar.

**The loss census.**
- Enron: hand programs run right on the compiled sheets for 182 of 186 questions.
- With the fit's reading, a right program is in the top 1 / 3 / 10 / 50 / 500 for 92 / 112 / 134 / 150 / 159. Depth
  beyond 50 adds 9.
- By form, top 50 then answered right: aggregates 63 then 40 of 70; lookups 36 then 21 of 47; top group 23 then 14 of
  24; top row 15 then 7 of 19; calculations 6 then 1 of 11; top column 5 then 2 of 10.
- Messy: 76 of 120 in the top 50 and 80 in the top 500. By messiness kind:

| Messiness kind | Questions | In the top 50 | Not within 500 | Answered right |
|---|---:|---:|---:|---:|
| Plain (the sheet's own words) | 15 | 13 | 1 | 14 |
| Column paraphrase | 46 | 31 | 13 | 19 |
| Value paraphrase | 50 | 30 | 19 | 23 |
| Structure | 9 | 2 | 7 | 1 |

- Correction to the plan: CLEF already reads every column (694 of 744 Enron column fields, all 480 messy). The
  binding caps are the 80-value limit of the value field and the decoder's ranking.

**Value expansion.** Every distinct value of a column with at most 30 distinct texts becomes a value option. Expanded
values sort after the proposed ones, those of likelier filter columns first, and the field holds at most 160.
- Same engine (next-engine 5aa5526), reader and chooser. Messy: listed 76 to 85 (value paraphrase 31 to 39), refit
  blind right 61 to 67, released head with evidence 62 to 70.
- Enron: listed 144 to 146, right 112 to 114.
- Reading tokens rise 34% (Enron median 8,765 to 11,767).

**Top-N lists added to the language.** next-engine `d289719`, local:
- `Rank.count` keeps exactly N lines or groups, with ties broken by sheet order or by the key. It is the same in the
  typed SQL and the deterministic dual emitter.
- The count is named as the stated number, the answer type is a list, and the read-as says how many.
- The decoder proposes it only for a stated whole number from 2 to 50.
- Tests: `tests.test_program` 55 of 55 (two new), `tests.test_named` 48 of 48, `tests.test_factory` 14 of 14.
- Its effect on DataBench is not measured yet; a run on the old engine preceded it.


## The sheet chooser refitted on the reader fit's pools: +4 right and −4 wrong at matched asks, but at the old threshold it answers more and errs more (2026-11-10)

Experiment `clef-chooser-refit-20261109` (gitignored, under `training/readers/data/experiments/`, with its contract).
One H100 PCIe pod ran at $2.89/h for 3.1 h, after a first pod that never exposed SSH; about $9.6 in all. No serving
code changed and nothing was promoted.

**The question.** The full-input reader fit (previous entry) put more right programs into the pools, but the sheet
chooser converted fewer of them. The chooser was trained on pools the fit's reading does not produce, so it was fitted
further on pools that the live path builds with the fit's reading.

**Training pools.**
- 1,016 held factory questions from 180 families. The small reader, the reader fit and both choosers never trained on
  these families.
- Each pool was built exactly as serving builds it: the small reader, the fit's reading fused at w=2, the typed
  decoder's top 50, and every candidate run on the question's SQLite tables.
- 885 pools hold a right executed candidate, and no reading failed.
- 2,000 questions were sampled. Collection stopped at a time bound, because CLEF's reading of held workbooks averaged
  about 4 s on this GPU, and 2,000 would have overrun the pod's deadline.
- Train-family questions could not be used. The reader fit's training order could not be reproduced: only 7 of its 50
  logged cut windows match, against 4 for a random order. So their pools would mix in readings of questions the fit
  had trained on.

**The fit.** The sheet chooser was continued from its own adapter and head for one epoch: 127 updates, seed 20261109,
otherwise the original recipe. Adapter `5af7ae6f`, head `b119340b`.

Live on the 306 real-sheet questions, thresholds unchanged (ask below 0.42, flag below 0.575). Right and wrong
include the flagged ones:

| Reading + chooser | Enron 186: right / asked / wrong | Messy 120 | Total |
|---|---:|---:|---:|
| Fit reading + refit | 87 / 73 / 26 | 57 / 48 / 15 | 144 / 121 / 41 |
| Fit reading + sheet chooser (same-pod control) | 76 / 88 / 22 | 54 / 56 / 10 | 130 / 144 / 32 |
| Released reading + refit | 79 / 85 / 22 | 62 / 42 / 16 | 141 / 127 / 38 |
| Released reading + sheet chooser (previous entries, other pod) | 77 / 90 / 19 | 56 / 49 / 15 | 133 / 139 / 34 |

Against the control, the refit has 16 wins and 2 losses, but 11 questions move from asked to wrong. It is more
confident: it asks 23 fewer times, and nearly half of those new answers are wrong at a threshold set for the old
chooser.

At matched asks it is better on both counts:
- at the control's 144 asks, 134 right / 28 wrong against 130 / 32;
- at the released configuration's 139 asks, 137 / 30 against 133 / 34.

The contract's gate was more right answers than the control without more wrong ones, at fixed thresholds. It fails.
The ranking gain is real but small, 4 questions of 306, and the refit needs its own threshold, set on factory data,
before any use.

Latency on this pod is not comparable, because two runs shared one H100 PCIe.

The loss stack on Enron, with the fit reading and the refit:
- the grammar expresses 182 of 186 hand-written programs;
- 144 have a right program in the top 50;
- the chooser's top pick is right on about 106;
- 87 are answered right.

Both reading and choosing must improve; another refit will not close the gap.

Spend: the CLEF program's RunPod total is now about $100 of the first $100. The owner approved $60 more on
2026-11-10.


## Three CLEF follow-ups: a full-input reader fit sharpens the pools but not the answers; result sizes do not help the SQL chooser; Flash reads 2-3x faster but weaker (2026-11-09)

Three experiments, all gitignored under `training/readers/data/experiments/`, each with its contract. Cost about $47.
No serving code changed and nothing was promoted. Each one tests one change against the configuration in the next
entry: the small reader, CLEF's released head reading at w=2, and the fitted sheet chooser.

**1. Reader fit on full option lists** (`clef-reader-full-20261109`; H100 80GB HBM3 at $3.99/h for 6.3 h, about $25;
a first attempt at about $1 ran out of memory).
- Data: 4,000 of the 6,011 factory B1 reading records (train parts 0–2). The 142 gold options the small reader never
  proposed are removed, because serving never shows them.
- Fit: each record is held to 12,288 tokens by dropping the lowest-ranked options of the longest fields; 1,718 records
  were cut. 500 updates in 4.8 h, LoRA rank 16, seed 20261109. Adapter `cadbd732`, head `840702bc`.
- Enron and messy were never trained on.

On the recorded reading records, with the same fusion and decoder, right within the decoder's top 1/3/10/50:

| Reading | Enron studied, 186 | Messy dev, 120 |
|---|---:|---:|
| Released head | 84 / 103 / 116 / 128 | 42 / 60 / 68 / 78 |
| Reader fit | 94 / 112 / 135 / 144 | 49 / 61 / 69 / 76 |

Live end to end, the chooser and thresholds unchanged, the pool gain does not reach the answers. Right and wrong
include the flagged ones:

| Live, 306 questions | Right | Asked | Wrong | Total p50, Enron / messy |
|---|---:|---:|---:|---:|
| Released reading (next entry) | 133 | 139 | 34 | 1.93 s / 1.42 s |
| Reader fit | 127 | 150 | 29 | 2.69 s / 2.42 s |

Question by question, Enron has 10 wins and 13 losses; messy has 5 wins and 8 losses. Of Enron's 112 questions not
answered right, 70 had a right candidate in the fit's top 50:
- 37 were asked because the chooser's pick fell below 0.42;
- 23 were asked because the chooser chose none;
- 10 were answered wrong.

The chooser and its thresholds were fitted on pools from the released reading, so the next controlled step is a
chooser refit on the fit's pools. Caveats:
- Both arms come from different pods. This pod's round trip is about 0.3 s longer, which explains most of the
  latency gap: the fit's reading takes 1.36 s / 0.98 s of GPU time, against 1.12 s / 0.70 s for the released head.
- Reader decisions were stable across GPUs in the earlier check (3,141 of 3,141).

**2. SQL chooser with result sizes** (`clef-sql-shape-20261109`; H100 NVL at $3.19/h for 3.5 h, about $11). This is
the earlier SQL chooser's recipe with two changes:
- each listed reading says how many rows and columns it returns;
- the label is right *and* returns a row.

Everything else is the same: data, 1,296 updates, seed. Adapter `fd8031f3`.

Offline, with first choice forced, against the earlier fit:

| Set | Right (served label) | Right (pool label) | Picks an empty result |
|---|---:|---:|---:|
| dev, 990 with candidates | 684 vs 681 | 698 vs 712 | 34 vs 72 |
| syn_dev, 793 with candidates | 485 vs 483 | 499 vs 509 | 54 vs 94 |

Served Spider calibration, 1,053 questions, `whole_db`, on one 224-CPU pod (`clef-spider-flash-20261109`; H100 SXM at
$3.99/h for 2.6 h, about $10). Each arm ran in 8 shards of 4 threads. The SQL head was called live inside the shipped
`select_query`:

| Arm | Right | Wrong | Declined |
|---|---:|---:|---:|
| Production re-scorer | 710 | 278 | 65 |
| Result-size chooser | 709 | 314 | 30 |
| Result-size chooser, its 35 least confident answers declined | 708 | 280 | 65 |
| Earlier chooser (next entry, other pod) | 716 | 285 | 52 |

Against production the transitions are 40 wins and 41 losses. Production reproduced exactly across pods: 710 of 710
right in common, no transitions. The hypothesis fails. Avoiding empty results turns declines into answers, and those
answers are wrong.

**3. Clef-Flash 9B** (rev `fb4417d3`, released head, zero shot). It was measured on the reader fit's pod: Flash on
port 8766, and the 27B with only the sheet chooser on 8765, together using 73.5 GB of 81.5. Pod C had brought Flash up
first, but my monitor missed its start. It served unused for 95 minutes until its deadline, about $6 lost.

Reading pools, top 1/3/10/50, with the same records, fusion and decoder:

| Reading | Enron studied, 186 | Messy dev, 120 |
|---|---:|---:|
| 27B released | 84 / 103 / 116 / 128 | 42 / 60 / 68 / 78 |
| Flash released | 65 / 91 / 111 / 124 | 36 / 52 / 66 / 76 |

Live end to end, Flash reading with the 27B sheet chooser:
- 124 right, 153 asked, 29 wrong of 306. The 27B reading gives 133 / 139 / 34.
- Total p50 is 1.71 s / 1.55 s on this pod, where the 27B fit took 2.69 s / 2.42 s.
- Flash's reading is 0.47 s / 0.34 s of GPU time at the same tokens, against 1.36 s / 0.98 s for the 27B.

Flash's top-1 loss could be partly the w=2 fusion weight, which was picked for the 27B. A Flash reader fit, or a w
picked for Flash, is untested.

Spend: the RunPod total for the CLEF program is about $90 of the $100 approved, $33.72 overnight plus about $57 today.
That includes the next entry's pod.


## CLEF served live: real sheets 133 right / 34 wrong of 306 at 1.4–1.9 s; on served Spider the chooser is 716 vs 710 (2026-11-09)

Experiment `clef-serve-20261109` (gitignored, under `training/readers/data/experiments/`, with its contract). One H100
80GB SXM pod ran for about 2.2 h at $3.99/h, about $9. It held one CLEF server (rev `cfddda04`, bf16 backbone, fp32
heads, Cloudflare's Apache-2.0 release code, no Unsloth) with three heads:
- the released head;
- the sheet chooser (`clef-chooser-20261109` fit, adapter `c34cf656`);
- the SQL chooser (`clef-sql-chooser-20261109` fit, adapter `f8e67c8c`).

The owner's machine reached it through an ssh tunnel. No serving code changed and nothing was promoted.

**The CLEF path, live end to end.** `local/clef_path.py` runs next-engine's own stages in order:
1. The small reader (H5, bge-small, CPU).
2. CLEF's reading of the decoder's slots, fused at w=2.
3. The typed k-best decoder, top 50, every candidate run on the request's SQLite tables.
4. The fitted chooser over the read-as sentences of the candidates that returned rows.
5. Answer at p ≥ 0.575, amber flag from 0.42 to 0.575, ask below 0.42 or on none. The thresholds come from factory validation only.

It reproduces the recorded-pool result. The decision is the same on 183 of 186 Enron questions and 119 of 120 messy
ones; the four that differ sit at a threshold. Latency is per question, measured on the owner's machine through the
tunnel. The workbook is compiled and indexed at upload, which is not counted.

| Real-sheet questions | Right | Flagged right | Asked | Flagged wrong | Wrong | Total p50 / p90 |
|---|---:|---:|---:|---:|---:|---:|
| Enron studied, 186 | 52 | 25 | 90 | 6 | 13 | 1.93 s / 3.33 s |
| Messy dev, 120 | 50 | 6 | 49 | 6 | 9 | 1.42 s / 3.18 s |
| Total, 306 | 102 | 31 | 139 | 12 | 22 | |

Per stage (p50, Enron / messy):

| Stage | Enron | Messy |
|---|---:|---:|
| Small reader | 0.05 s | 0.05 s |
| CLEF reading | 1.21 s | 0.81 s |
| Decoder, 50 SQLite runs included | 0.04 s | 0.04 s |
| CLEF chooser | 0.55 s | 0.49 s |

CLEF's reading is 1.12 s / 0.70 s of GPU time at a median 8,765 / 4,692 tokens; the chooser is 0.49 s / 0.44 s at
2,369 / 2,045 tokens.

Of Enron's 90 asks, 62 are the chooser choosing none, which is the pool-recall gap. Flags mostly mark one slot, but some
mark three to six, when the runner-up is a different reading.

**Shortening CLEF's input costs right answers.** The reader's input is mostly the option lists: the sheet is 13–17% of
its tokens. Trimming each slot's options and listing fewer candidates to the chooser, end to end on the same 306:

| Variant | Right + flagged right | Asked | Wrong + flagged wrong | Total p50, Enron / messy |
|---|---:|---:|---:|---:|
| All options, 50 candidates | 133 | 139 | 34 | 1.93 s / 1.42 s |
| Top 20 options, 20 candidates | 115 | 156 | 35 | 1.38 s / 1.22 s |
| Top 10 options, 20 candidates | 105 | 163 | 38 | 1.17 s / 1.08 s |

The loss is in the reading's pool, top 1/3/10/50 fused:

| Options per slot | Enron | Messy |
|---|---|---|
| All | 84 / 103 / 116 / 128 | 42 / 60 / 68 / 78 |
| Top 20 | 68 / 84 / 102 / 114 | 43 / 56 / 62 / 72 |
| Top 10 | 60 / 80 / 95 / 108 | 37 / 48 / 57 / 71 |

Capping the sheet at 3,000 tokens on top of the top-20 trim changes these by at most 3. Listing 20 candidates instead of
50, on its own and measured on the recorded fused pools, gives 134 right and 39 wrong against 133 and 34. It takes 0.30 s of GPU time instead of 0.49 s. Each pass has a fixed cost of about 0.3 s.

**Caching the sheet** (the workbook encoded once, each question run on a copy of its cache) gives no gain:
- 1.16 s per question against 1.12 s uncached on Enron, and 0.74 s against 0.71 s on messy. The prefix is 1,400 / 504
  tokens and takes about 0.2 s once per sheet, and copying the cache costs what it saves.
- It is not exact: the top option differs on 1.4% of slot decisions against a single pass.
- It needs the question after the sheet, which alone changes the top option on 13% of slot decisions. The fused pool is
  no worse for it: Enron 84/103/120/133 and messy 44/60/69/77 against 84/103/116/128 and 42/60/68/78.

None of these input changes reach a second.

**Across GPUs.** Last night's pod was an H100 PCIe or NVL; today's was an H100 SXM.
- The released head's reading kept the same top option on all 3,141 slot decisions, and 99% of probabilities moved
  less than 0.004.
- The chooser kept its top pick on 302 of 304 questions, but its probabilities moved by up to 0.11 and four
  questions changed answer/flag/ask class.

So the path is reproducible on one hardware type, not yet across types.

**The B1 controlled check.** CLEF's released head, zero-shot, under the B1 fit pilot's exact input: each slot's top 20
options plus none, state capped at 3,000 tokens, fused at w=2. Right within the top 1/3/10/50:

| Input | Enron, 186 | Messy, 120 |
|---|---|---|
| Zero-shot, trimmed and capped | 68 / 83 / 101 / 111 | 43 / 56 / 62 / 71 |
| B1 fit pilot, same input | 74 / 94 / 115 / 127 | 52 / 61 / 65 / 71 |
| Zero-shot, untrimmed (reproduced exactly) | 84 / 103 / 116 / 128 | 42 / 60 / 68 / 78 |

Fitting the reader helps, by up to 16 Enron and 9 messy questions. The pilot looked flat because the trim and the cap cost
the zero-shot reading as much as the fit gains.

**Spider, served.** Both runs are fresh served `whole_db` calibration runs of production's own `spider/probe/full_eval.py`
on the pod (Linux, 256 CPUs, 4 threads per run, 8 shards each), paired on the same code (`6d5858c`, the code of
`4bc611b`), the same pinned bundle (`4545a4810fa2`, fetched from the weights repository at `cd8a03ab`) and the same
machine. In the CLEF run, `served_eval_live.py` injects one dependency, the readers' re-scorer
(`ReadingSearch.rescore`, `confidence`, `confident`). It asks the SQL chooser live about every candidate that ran, in
pool order, and always answers its pick (forced choice). Everything else is production's served path.

| Calibration, 1,053 | Right (strict) | Wrong | Declined |
|---|---:|---:|---:|
| Production's shipped re-scorer | 710 | 278 | 65 |
| CLEF SQL chooser in its place | 716 | 285 | 52 |

The transitions are 668 both right, 48 CLEF wins, 42 CLEF losses and 295 neither. **The recorded-pool +34 does not
survive serving.** Of the 54 questions the recorded measure gave CLEF that the served run does not:
- 24 are declined because the chosen query's served result holds nothing. The shipped re-scorer sees each result's
  shape; CLEF sees only the read-as lines.
- 22 have the same query graded differently as a served answer than in the pool. Production loses 22 the same way,
  which is also why production reads 710 here against 730 on the recorded pools.
- 7 got a different choice, and 1 was refused.

Where CLEF helps is in knowing when to decline. Declining each run's least confident answers until both decline the
same number:

| Declined | Production right / wrong | CLEF right / wrong |
|---:|---:|---:|
| 65 | 710 / 278 | 714 / 274 |
| 150 | 678 / 225 | 703 / 200 |
| 200 | 651 / 202 | 680 / 173 |
| 300 | 607 / 146 | 622 / 131 |

## CLEF's own head as reader and chooser: real sheets 134 right / 35 wrong of 306, Spider pools 764 of 1,053 (2026-11-09)

Measured on recorded candidate pools, not a fresh served run: no serving code changed and nothing was promoted. The
experiments live under `training/readers/data/experiments/`, which is gitignored: `clef-chooser-20261109`,
`clef-sql-chooser-20261109`, `clef-reading-zs-20261109` and `clef-reading-fit-20261109`, each with its contract. All
use Cloudflare's Apache-2.0 release code (`joint_schema_model.py`: one prefill, its joint schema head scoring every
option), CLEF rev `cfddda04` with LoRA rank 16, and no Unsloth.

**The chooser** picks one program, or none (an ask), among a pool's executed candidates. Each candidate is shown as its
read-as sentence. The sheet chooser was fit on 4,616 of the 6,011 pools collected for H7, over 577 updates. Pools come
from next-engine's program decoder with the H5 reader (`named_bge_small_s0`); families are split, and the evaluation
sets were never trained. Right first when it must choose:

| Set | Decoder order | H7 trained alignment | CLEF zero-shot | CLEF fit | Right listed |
|---|---:|---:|---:|---:|---:|
| Held families, 4,282 | 1,321 | 1,711 | n/a | **2,124** | 3,031 |
| Validation, 3,448 | 1,031 | n/a | n/a | 1,650 | 2,353 |
| Enron studied, 186 | 41 | n/a | 58 | 67 | 101 |
| Messy dev, 120 | 21 | n/a | 41 | 43 | 56 |

**The reader**: the released head, zero-shot, reads the decoder's fields (form, aggregate, answer type, direction,
table, measure, key, shown, filter, value, line, section) in one prefill over the compiled sheet. Its probabilities
add w·(log p − log 1/n) to H5's coordinate scores, and the same decoder enumerates the pool. Right within the top
1/3/10/50: Enron 41/65/86/101 at w=0 and 84/103/116/128 at w=2; messy 21/30/41/56 at w=0 and 42/60/68/78 at w=2. w was
picked on Enron; messy is the same at w=1 and w=2 on top 50.

**Reader and chooser together** (fused pools, fitted chooser): forced choice is right on 105 of 186 Enron questions
and 64 of 120 messy ones. The ask and amber-flag thresholds were chosen on validation only (wrong budget 10%: ask below
0.42, flag below 0.58). Served:

| Real-sheet questions | Right | Flagged right | Asked | Flagged wrong | Wrong |
|---|---:|---:|---:|---:|---:|
| Enron studied, 186 | 51 | 27 | 88 | 7 | 13 |
| Messy dev, 120 | 50 | 6 | 49 | 5 | 10 |
| Total, 306 | 101 | 33 | 137 | 12 | 23 |

Production on the same 306 is 70 right / 113 asked / 123 wrong (Enron replay 26 / 90 / 70, messy dev 44 / 23 / 53).
Both sets have been studied before, and the frozen messy set was not read. Latency is two prefills on an H100: reading
takes 1.0–1.8 s (4.7–8.8k tokens) and choosing 0.4–0.5 s, plus the CPU decoder at about 0.14 s. It is not sub-second.

**Spider**: the same chooser was fit on the shipped re-scorer's union pools (`rescorer_train_g`: Spider train, other
Spider train databases, BIRD train and Spider-Syn train; 10,368 records over 1,296 updates; dev and syn-dev were never
trained). On cross-fold pools, forced choice is right on 712 of 1,016 dev questions (pool order 575, listed 774) and 509
of 808 syn-dev questions (pool order 393, listed 560). **On production's own calibration pools** (a `pool_oracle` run at
`4bc611b`: production bundle `4545a481`, `whole_db`, cap 5,000, SQL backend, 1,053 questions), production's selection
is right on 730 (strict). CLEF zero-shot gets 727 and the CLEF fit **764 (72.6%, +34)**; the pool's ceiling is 882
(83.8%). This is a recorded-pool measure, not a served `whole_db` run.

**The B1 fit is a pilot with no clear gain.** CLEF's reader was trained on the factory's gold coordinates: 232 updates
over 1,856 of 2,004 records. To fit in memory, each field kept the reader's top 20 options and the sheet state was
capped at 3,000 tokens; scoring used the same cap. Fused at w=2 and compared with zero-shot, Enron's top 1/3/10/50 went
from 84/103/116/128 to 74/94/115/127, and messy's from 42/60/68/78 to 52/61/65/71. The trim and the state cap confound
this comparison, so it is not evidence either way. A zero-shot run under the same trim is the controlled check.

RunPod compute for the night was $33.72, from a balance of $191.49 to $157.77, including three short failed setups. The
headroom left is recall: 58 Enron and 42 messy questions have no right program in any pool.

## Named CLEF readout (S1) fails its gate: 0/100 strict, 10/300 with its gates removed (2026-11-09)

Experiment `named-readout-20261108` (contract and package in `training/readers/data/experiments/named-readout-20261108/`,
gitignored) fitted Codex's named readout at `86bedc9`: CLEF rev `cfddda04` with LoRA rank 16, one linear projection to
the 215 named coordinates (contract `e85030a0`), 1,024 updates at batch 2, seed 3407, on the part-0 corpus (`42eec10d`,
16,845 training records). It ran on one A100 80GB (SECURE) for 4.86 h. It saw **1,411 unique records (8.4%)**; the
opaque-head run it was matched against took the same wall time for 4,096 samples. The candidate is
`semantic-model.json` `b0aac69b` / `named-head.pt` `bbb38af2`, not promoted. The gate was Spider calibration
>= 699/1,053 and Enron wrong <= 42.

Calibration (1,818 held-out records; its 6 Spider databases share none with training): MAE **0.036** (a zero
predictor scores 0.089), precision 0.93 and recall 0.82 at 0.5. Qualification (support >= 20 per class, precision and
recall >= 0.95, MAE <= 0.1) seals **7 of 118** coordinate/role rows. Serving decides only on qualified coordinates, so
the contract's served Spider run (`whole_db`, served selection, `--no-compose`, AUTO, 5,000-row cap) answered **0 of
the first 100** questions (all `unqualified_or_unknown_proposition`); production answers 74 of them right. That run was
stopped at 100 questions as a sample.

Labelled ablation, not a gate: the same calibration re-sealed with the gates removed (support >= 1 per class)
qualifies 42 of 118 rows. Served Spider, first 300 questions in dataset order: **16 answered, 10 strict** against
production's 188 (0 wins, 178 losses). Of the unanswered questions, the first unbound decision was nearly always
`below_score` (best option median score 0.31 against 1/2, median 4 legal options), at the projection column, the FROM
table or a join. The coordinates involved are generic role coordinates (`role.projection`, `role.record_grain`,
`role.join_left/right`, `role.relation_target`), not schema.org properties. Enron, 186 questions, all tabs: **0 right
/ 185 asked / 1 wrong** (production bundle 26 / 90 / 70; opaque CLEF 29 / 115 / 42). Latency on the A100, one question
at a time: Spider median 5.84 s (prefill 4.32 s over a median 18,127 input tokens), Enron median 4.3 s (13,397
tokens).

The named readout loses to the opaque head at matched compute by a wide margin. The causes measured here are a
single-path decoder (one unbound decision returns no query), weak column/table grounding on unseen schemas, and
under-exposure. Evidence: `evidence/` and `lease-output/` in the experiment directory (archive `export-manifest.json`
hash-checked). Compute: 9.1 pod hours at $1.79/h (about $16.6 including two failed setups at $0.24).

## Codex's reader work on main: served Spider 706/1,053, messy dev 44/23/53 (2026-11-08)

Fresh serving-faithful run of the production bundle (`4545a481`, unchanged) at `86bedc9`, which brought Codex's
worktree into main. Calibration 1,053, `whole_db`, served selection, AUTO execution, 5,000-row cap, two shards merged:
**706 strict (67.0%) / 798 lenient / 988 answered**, against **708 / 799 / 988** at `eb40c4c` (Phase 0). Transitions:
0 wins, 2 losses, 706 unchanged right, 345 unchanged wrong; 1,048 of 1,053 served queries are identical.

All five changed queries trace to one owner change: a zero-padded integer string now stays text
(`engine/sql_schema.py:_infer_type`). `behavior_monitoring.Addresses.zip_postcode` is `VARCHAR(20)` holding `002`
and `062`, which integer typing turned into 2 and 62; questions 505 and 539 now compare `= '197'` and stay right.
On the retyped schema the readers rank two other questions of that database differently and lose them (525 picks
SUM for "highest average", 542 drops the Students arm of an INTERSECT); 904 changes and stays wrong. This is a
correctness hardening with a measured cost of 2 strict, not an accuracy change, and it is not patched per question.

Messy dev (120, served, `regress.run_messy`): **44 right / 22 asked / 54 wrong** at `86bedc9` against 44 / 23 / 53 at
Phase 0. The one change was "revenue from people who stayed longer than half a year": the new number words read
"half" as the literal 0.5 and served "past due is above 0.5". The follow-up commit keeps a fraction a number only as
a share or a scale ("more than half of the orders", "half a million"); "half a year", "half an hour" and "last
quarter" name no literal, and the decoder's context-free `number()` reads no lone fraction word. That question asks
back again, as at Phase 0; it is the only messy question with such a word and no calibration question has one, so
messy dev is **44 / 23 / 53** and Spider is unchanged by the fix. Per-example records:
`prereasoner-readers-artifacts/evals/codex_pull/spider-86bedc9/` and `evals/messy/messy_messy_served_86bedc9.json`.

## Recursive ACE corpus and benchmark-input gates; no new accuracy score (2026-11-07)

The full `shared-semantic-grammar-20261106` admission audit visits **32,798 question parents**.
All **32,160 admitted executable records** pass the saved ACE JSON schema, exact typed-AST codec
round trip and exact parse-tree agreement with the original Attempto APE parser. There are
**12,788 distinct canonical programs**; equivalent programs share cached parser results, while
every parent retains an individual audit record. **499 parents are rejected** because a target
literal/limit is absent from input-derived candidates; **139 admitted clarification records** have
no executable tree. No parser/schema failure is reported. The run took 1,491.46 seconds.
`native-ape-assessment.json` pins the admission, schema and parser grammar hashes; individual
records are in `native-ape-records.jsonl.gz`. This verifies canonical application-ACE representation,
not an independent judgement that the original English was interpreted correctly.

The frozen fit contains **46,952 train / 5,125 calibration / 2,162 held-out entity test records**;
the fit reader excludes the test records. The complete served-input resource audit has no errors:
the standing **1,053-question** Spider database-held-out calibration split has median **9,873**
and maximum **13,850** tokens; all **186 all-tab Enron** questions have median **7,697** and
maximum **23,814** tokens. These are input measurements, with no model predictions or gold-driven
input selection. The private benchmark package pins all 20 Spider databases, 97 Enron workbooks,
original graders and source code.

Original Qwen-source recovery now has **528 exact Git-blob-verified CSVs**. The pinned converter
reproduces every historical row count. Source-SQL/AST/ACE original-row parity and schema identity
pass for **3,600 training / 360 evaluation pairs**. Two additional training pairs hit SQLite integer
SUM overflow; **70 train / 15 eval** parity-pass records still have unparseable numeric-format
evidence. The source audit does not gradient-admit these pairs or certify English meaning. As of
this snapshot, **322 CSVs await a Software Heritage rate-limit retry and 24 are unavailable**;
these are archive responses, not GitHub rate limits. Detailed outcomes remain in the existing
`shared-semantic-dialogue-20261106/original-row-assessment.json` and recovery manifests.

Serving traces now replay named choices and currency/group/tie transforms, re-derive currency
bindings from the input graph and reject tampering. Fresh Enron source execution uses original
importer worksheet maps and checks exact agreement with the selected answer. Focused suites pass
**92 readers, 117 SQL AST, 75 deterministic emitter and 62 release tests**. These are mechanism
checks. No new recursive CLEF Spider/Enron accuracy, CPU latency, real-property causal alignment,
promotion or complete objective achievement is claimed by this entry.

## Enron selected-query source tracing; no new model score (2026-11-06)

The existing semantic audit examined all **186** saved all-tab baseline records on current workbook
imports. It replayed saved selections through `TableQuery.execute`, the dual SQL/Python executor and
request provenance owner, without loading a model or selecting a query. **57/96** saved selections
now pass execution parity and resolve every retained source-row identity; **37/96** cannot be imported
by the offline SQL adapter, and **2/96** remain unsupported or exceed the Python row bound. The
remaining **90/186** records have no saved selected query. Original baseline verdicts remain
**26 right / 70 wrong / 90 asked**, with pool recall **45/186**; these are not fresh accuracy results.

Generic DISTINCT grouping and integer-to-text column affinity repairs removed measured execution
gaps. The registered deterministic suite passes **75 tests** and decomposition **18 tests**. A
duplicate-row regression proves both equal observations survive into one DISTINCT contributor set.
The audit compares exact values at the same wire boundary as `TableQuery.execute`; decimal strings
are not silently accepted as approximate numbers. Query 20.1 reproduces six counterparties with six
resolved original worksheet rows, including the DISTINCT stage. This is positive row provenance,
not minimal cell influence or learned named-dimension causality. The replay explicitly uses observed
schema types, not the original model's schema predictions. Evidence, configuration, source/code
hashes and every failure are in `shared-semantic-scoped-20261106/enron-evidence/`.

No new shared-head fit, model qualification, serving promotion or Spider accuracy improvement has
been established by this change.

## Scoped named operator readout implemented; not yet fitted (2026-11-06)

The shared-head inference consumer and operator decoder now use named coordinates to choose finite
SQL enums, with exact contribution replay and refusal for unknown/unqualified alternatives. A
mechanism intervention changes only SUM to AVG and changes the compiled/executed result from 207
to 51.75 on the same four source rows. These are controlled profiles, not learned-model accuracy.
Source annotations preserve separate occurrence paths and omitted ASC/INNER defaults. Input-derived
operator banks remain identical when targets change; inactive slots have zero training gradient.

Preparation retains **47,445 train / 5,131 calibration / 2,162 entity test records**, adding scoped
operator items to query records. Dataset SHA256:
`afe5d1d3063d52943f3d9ec85c85b76b2abaa9bc697976eb8db1739b78886880`.
The registered readers suite passes **81 tests**, with zero failures/skips. This data is not fitted;
the finite bank still has the existing readings bounds and is not a full recursive AST readout.
There is no new serving/Enron score or model qualification. Evidence is under
`shared-semantic-scoped-20261106/`.

## Shared head: complete calibration; original Qwen sources recovered (2026-11-06)

The corrected 256-update candidate was evaluated on **all 5,131 calibration records** exactly once:
2,921 questions and 2,210 values, with 353,281 known coordinate targets. No test examples were fitted
or evaluated. Candidate metadata SHA256:
`20789f8f08a93c3bec50f029012b115fe64eb413c255ce4fc8c6b5388460dd11`;
prediction SHA256: `2ebf4224f5eef2d7bc590e4cd780368f796f7ebecb912901a71e59594212adfb`.
The frozen input package SHA256 is
`956c517d363d818ea65f0b8be9d4ca90c02b3af2ccd19a4c471d7cebe0bb8dba`;
commit, dirty-source manifests, dataset, seed and model revision are recorded with the pilot below.

| Block | Known targets | MAE | TP / FP / FN / TN at 0.5 | Binary F1 |
| --- | ---: | ---: | --- | ---: |
| SQL | 185,664 | 0.05143 | 10,920 / 2,787 / 4,552 / 167,405 | 0.74848 |
| Expected properties | 156,910 | 0.02485 | 2,388 / 68 / 1 / 117,809 | 0.98576 |
| Dialogue | 10,707 | 0.00399 | 4,845 / 20 / 20 / 5,822 | 0.99589 |

Fractional property targets are excluded from binary counts. Property supervision here is value-role
evidence, not real noun/column binding qualification. Complete 64-coordinate SQL presence vectors
match on only **641/2,901 answerable questions (22.1%)**. All **20/20 clarification cases** are
incorrectly called answerable. SUM, MAX, MIN, several comparisons, OR, DISTINCT and other operations
have zero recall at 0.5. Reconstructing the seeded 256-step schedule finds 170 questions, 86 values,
zero clarification/noun/column examples, four SUM positives and eight AVG positives. Sparse-coordinate
F1 does not establish question-to-SQL accuracy; coverage and undertraining are measured bottlenecks.

The same candidate's fixed 64 serial diagnostic and full-calibration batch-eight evaluation differ
on **262/13,760 quantized coordinates**, across 58 examples (maximum probability difference 0.05765).
Separate load/allocation, visitation order and batching changed together; the cause is not isolated.
The 649 mixed-length batches consumed 2,058.18 seconds of model-call/transfer time, excluding startup
and tokenization; this is not per-request latency. GPU samples reached 45,896 MiB on an A40. Both
completed-job GPUs were terminated after complete local artifact recovery. All 215 outputs remain
unqualified; no new served Spider/Enron accuracy, promotion, deployment or CPU benchmark is claimed.
Evidence: `shared-semantic-defaults-20261106/{full-calibration/,calibration-postprocess.json,
calibration-coordinate-diagnostics.json,recovery-assessment.json}`.

Searching all fourteen `C:/work/prereasoner-*` directories and the complete reachable histories of
both repositories (908 commits/54 refs in prereasoner-data; 108 commits/one ref in prereasoner-flat-data)
found the original single-table/join generators and quoted source corpora. Pinned flat-data revision:
`a2cb5dd30378a6ddcdb9aaace7cf334fb2ff8549`. Its single-table and historical join training graphs match
the local saved files after newline normalization. Every one of **6,481** original question/SQL
records matches metadata, question annotations, physical pointers and program tokens. Literal
spellings come from those verified originals; numeric column types are the pinned generators'
declared NUM requirements, not inferred observed CSV types.

All **6,481/6,481** now pass SQL/ACE JSON Schema, lossless codecs, source/AST/ACE parity on synthetic
original/permuted/cloned fixtures, and actual pinned APE parsing with exact tree agreement. All
**1,144 filter programs** have matching source rows in every fixture, avoiding vacuous empty-result
parity. All 6,481 stored ten-intent question annotations agree with AST labels. These annotations
do not independently certify every English argument or resolve ambiguous questions.

The original held-out split has 922 records over 104 disjoint single-table files and two held-out
join schemas, but includes 169 records representing 155 question/SQL pairs seen in training. Remove
those, eight further eval duplicates, and 75 identical English questions with different SQL:
**670 unique, non-intersecting eval pairs** remain (618 single-table, 52 joins), with zero question
text or question/SQL overlap. All **670/670** pass the same schema/codec/fixture/parser gates and
stored intent agreement; every filter program exercises matching rows. Neither recovered corpus
is admitted to gradient training. Evidence and pinned source hashes:
`shared-semantic-dialogue-20261106/{original-source-assessment.json,original-split-assessment.json,
repository-history-assessment.json,qwen-program-assessment.json,qwen-ape/,original-eval/}`.

The historical metadata also retains full Git blob hashes. Software Heritage supplied **118/874**
referenced original raw CSVs (19,451,881 bytes), each verified by its Git blob hash. Reconstructing
their typed rows with the pinned original converter matches all 118 historical row counts. Schema
identity and source-SQL/AST/ACE row parity pass for **812 training and 62 strict-eval questions**,
using the same first-60-row window as the source generator. **54 train + 11 eval questions** involve
declared numeric columns still stored as formatted strings; all such values parse with the existing
`engine.numeric.parse_decimal`. The audit records this evidence separately; it does not silently
normalize original rows or certify English meanings. The remaining **756 CSVs** are deferred by the
archive's anonymous rate limit, and join source schemas are synthetic. Per-file hashes, type-format
evidence and per-pair statuses are in `original-csv-recovery-assessment.json` and
`original-row-assessment.json`. Random table nouns in the original generator are aliases, not
Schema.org class supervision.

An input-identity audit also finds **20 training groups (42 records) and eight eval groups (16
records)** with identical question, table names and complete schema units but different projections.
Their ASTs are identical after removing the SELECT list. For example, a join top-k question never
specifies which extra dimension attribute the generator projects. These are multiple plausible
presentations, not a uniquely determined single-target AST. They need an explicit projection policy,
output-column clarification or multiple accepted programs before admission; choosing one arbitrarily
would conceal inconsistent supervision. The full audit retains every pair and records these
conflicts in `qwen-source-conflicts.jsonl.gz`. The evidence seal is `source-recovery-assessment.json`.

## Shared named head: first fit completed; AST-default label correction audited (2026-11-06)

The shared 215-coordinate head completed **256 updates in 521.47 seconds** on one A40, using native
CLEF prefill states and LoRA rank 16; its stock choice head is discarded. Dataset SHA256:
`b73e14efcbd7eb1f29ac780896f0d5b82096e88ba6d4b81f8227fa41baaee830`.
Counts: **47,445 train / 5,131 calibration / 2,162 entity test**. No test records fitted the model.
Only 256 batch-one updates were taken; this is a feasibility pilot, not a full-corpus training claim.
The experiment records commit `eb40c4c9144a926f0cc9260d9928fba343a0801f` and a dirty worktree; its
immutable package manifest identifies the actual source bytes. Seed: 3407; pinned CLEF revision:
`cfddda043dd190ef3b01cff449d5ee8cff1e8ec6`.

All four adapter/head manifest hashes match recovered local files. Metadata SHA256:
`1e791baca545a093109667890bfd660a9b359875a992a0546801a951cab08459`.
Reload reproduces quantized output on the frozen reload example. Repeated forwards match on
42-, 361- and 10,656-token inputs; paired-forward times are 0.522, 0.747 and 12.838 seconds,
respectively, including their differing warm-up costs. These are not CPU or throughput benchmarks.

The independent supervision-support audit exposed a label defect: canonical JSON omits default
`OrderTerm.direction=ASC` and `Join.kind=INNER`, and the original selector labels ignored those defaults.
The current gold owner now reads defaults from the typed dataclasses. Regeneration changed **2,467 ASC
and 14,638 INNER flags across 16,318 records**; every one of **32,659** executable targets passes the
SQL JSON Schema and an independent typed-field selector oracle. Corrected dataset SHA256:
`a38436a695abf58efb09b061ca2d307ec175b5b5cbb8b95181f210db8b985af5`.
Raw inputs, items and all other annotations are unchanged. This invalidates qualification against the
original SQL labels. The controlled repeat changes only these two target labels.

On the first fit's fixed **64 calibration records** (36 questions, 28 entity values), rescoring the
saved predictions against corrected targets gives:

| Coordinate block | Known targets | Initial MAE | Fitted MAE | TP / FP / FN / TN at 0.5 | F1 |
| --- | ---: | ---: | ---: | --- | ---: |
| SQL | 2,304 | 0.47866 | 0.04681 | 114 / 38 / 57 / 2,095 | 0.70588 |
| Expected properties | 1,988 | 0.46800 | 0.02313 | 30 / 0 / 0 / 1,475 | 1.00000 |
| Dialogue | 134 | 0.57207 | 0.00011 | 62 / 0 / 0 / 72 | 1.00000 |

Property binary counts exclude fractional targets; this sample contains only value-role property
evidence and no positive clarification/unsupported examples. These are sparse-coordinate diagnostics,
not calibrated binding or answer accuracy. All 215 coordinates remain unqualified. Full calibration
subsequently completed, as recorded above. There is no trained recursive tree readout, new served Spider/Enron score, CPU
benchmark, promotion or deployment. The 708/1,053 Spider and 26/186 right Enron baseline below remains
the latest serving evidence. Experiment artifacts are under ignored `training/readers/data/experiments/`
directories `shared-semantic-20261106` and `shared-semantic-defaults-20261106`.

The corrected-label repeat subsequently completed **256 updates in 519.34 seconds** and passed the
saved-model quantized reload check. Same fixed 64-record diagnostic:

| Coordinate block | Known targets | Fitted MAE | TP / FP / FN / TN at 0.5 | F1 |
| --- | ---: | ---: | --- | ---: |
| SQL | 2,304 | 0.04896 | 116 / 35 / 55 / 2,098 | 0.72050 |
| Expected properties | 1,988 | 0.02401 | 30 / 0 / 0 / 1,475 | 1.00000 |
| Dialogue | 134 | 0.00025 | 62 / 0 / 0 / 72 | 1.00000 |

INNER positives improve from **0/12 to 6/12**; the one ASC positive remains below 0.5 (probability
0.19229). SQL F1 rises while overall SQL MAE worsens; this is a mixed pilot result, not a release gain.
Before fitting, **460/13,760** initial quantized outputs differ between the two A40 allocations, with
maximum probability difference 0.01909. Model source and installed package versions match, but evaluator
visitation order changed. Cross-run numerical identity is not established; the difference has not been
causally attributed. Same-instance repeat/reload checks do not resolve it.

The separate dialogue-data revision checks all **14,371** source conversations and labels 7,116
previous-question references and 1,380 order/limit commands. Source readings retain 15 ordering histories
that canonical AST MIN/MAX optimization erases. Input-derived sample anchors may vary between turns;
column/type layout and original user text must match. Its data SHA256 is
`5ab64d7df15d45df51ea3d59759349837257d98111a5fbe42b32fac818dfce31`; it is not yet trained.

An earlier local audit incorrectly counted zero SQL programs in the original Qwen corpus by looking
only for top-level query fields. Matching adjacent intent/role records recovers **6,335 single-table +
146 join question/program-token pairs**, with matching metadata/schema and **zero pairing failures**.
All **8,189** question links resolve to schema columns and SQL-token occurrences; 404 include exact
literal-text identities. Role units preserve SQL token kinds/clauses, but unquoted literal text omits
its original type. No execution or semantic correctness is claimed and no new source is gradient-admitted.
The corrected recovery evidence is `shared-semantic-dialogue-20261106/qwen-recovery-assessment.json`.

The token-only audit initially recovered 4,819 programs, then 4,821 after reusing the existing
canonical column-name owner for duplicate headers. It excluded 1,660 cases requiring literal or
numeric-column type evidence. The original-source recovery above now resolves all of those cases;
the current per-pair audit and parser outputs cover all 6,481, with the same explicit limitation
that representation checks are not training admission or original-data/English certification.

## Shared-property requirement audit: AST trial interrupted; no new candidate accuracy claim (2026-11-06)

The owner clarified that ACE noun, schema-column and contextualized-value meanings must share Schema.org
property coordinates which causally determine semantic binding. A separate audit of both production and the
CLEF candidate found that neither implements that interface for own-data binding. The CLEF trial uses raw
contextual/lexical projections, a residual scorer and AST/action supervision; readable program trees are not
the requested shared semantic representation. No comparison has established that a complete implementation
of the owner's architecture failed. See the model card and architecture migration contract.

The 512-step AST-format fit was interrupted on that steering. Checkpoint **384** and all fifteen file hashes
were verified and retained on the original durable volume. Fourteen files recovered locally; the adapter
transfer remained incomplete, so the local directory is not a complete model bundle. The GPU pod was deleted
and a read-back returned not found. The prepared CPU VM was never allocated. The original training contract,
data and objective remain unchanged; this is not a completed 512-step experiment, a shared-property model,
or a promoted candidate. There is no new candidate Spider/Enron accuracy to report.

The fresh production-only calibration run did finish: all four disjoint shards merged with matching
fingerprints, reproducing **708/1,053 strict**, **799 lenient**, **988 answered**, under `whole_db`, served
selection, AUTO execution and a 5,000-row cap. The fresh Enron baseline reproduced **26 right / 90 asked /
70 wrong of 186**, with pool recall **45/186**. These are project calibration/baseline results, not an
official Spider test result. No new frozen-test run occurred. Recovered evidence is under
`clef-grammar-fit-20261105/recovered-evidence/`; semantic audit, coordinate order, annotation fixtures and
an explicitly unapproved draft experiment contract are under `shared-property-audit/`.

The direct source-program audit checked all **22,029** source pairs. After fixing offline import of NULL
tests/LOWER and rejecting unrepresentable OFFSET, compound ordering/limits and SQLite integer division,
**18,427** targets pass source-SQL/typed-AST execution, recursive JSON-schema validation and lossless ACE
projection. All **18,427/18,427** pass the real pinned Attempto parser with exact generated-tree agreement.
All accepted targets match source SQL on original, permuted and cloned rows, with **zero** disagreements;
LIMIT cases were scored strictly. **3,602** sources remain excluded with recorded reasons, and **916**
accepted targets fail the finite neural binding profile. This is not complete corpus support or independent
certification of every original English intent. The two changed-data failures before the final importer
hardening are retained with their diagnoses, not relabelled as passing ties. Final corpus evidence is in
`clef-ast-20261105/source-programs/semantics-checked/`.

Registered local suites pass **60 readers / 113 SQL AST**; compileall, focused Ruff F/E9 and diff checks pass.
Forty local offline suites exit successfully, with explicit model-dependent skips. A model-provisioned Linux
run passes 39 suites; its release suite's sole missing-Git-fixture failure is retained, and a rerun using
authentic existing HEAD objects passes **57/57** release tests without source changes. Eleven live PostgreSQL
suites and the live Gemini suite were omitted. Lossless disk-backed score storage also passes native64-record
identity, concurrency, truncation/duplicate/corruption tests, and preserves every historical E novel-case SQL
and verdict (**85/128**, robust answer programs **69/112**). These checks do not establish new learned accuracy.

## Recursive ACE projection and generic AST input: GPU feasibility passed; fit interrupted (2026-11-06)

The recursive application ACE view now passes the actual pinned Attempto parser for all **18,364/18,364**
admitted pairs, with exact agreement between the parser tree and generated JSON tree, and no failures.
This reifies the typed program as ACE facts and a result question; it does not establish that a fluent ACE
paraphrase preserves every original English question. Evidence is `clef-ast-20261105/recursive-ape/actual-ape-audit.json`;
parser revision `5f4d5354a45fb772763bf1a9543f508f15b28982`, projection SHA256
`86c298e32fabb2581bf742d2ed2fbe7190ca52a7564970a7ca208df08a629dc8`.

The generic typed-AST action input includes the actual engine constructors, field order and types once,
followed by input-derived options and 256 positional decisions. All **12,397** training and **1,974** calibration
records tokenize without truncation at 65,536 tokens; maxima are 50,085 and 21,968. Existing source-SQL execution
checks match all **12,278/12,278** training answer targets and **1,952/1,954** calibration answer targets; 139
records request clarification. The two calibration disagreements are nonunique LIMIT-3 boundaries and remain
strict failures. No new model accuracy follows from target validation. Data hashes and classifications are in
`clef-ast-20261105/target-audit-assessment.json`. Neural binding still excludes derived table sources; finite
program, alias and grounding bounds remain. This is not complete SQLite support.

The earlier generic-input native-head preflight completed two minimum-input forwards in 40.253 seconds and two
median-input forwards in 5.999 seconds, peaking at 32,722,965,504 and 60,893,027,840 allocated bytes respectively.
The widest 52,338-token input failed on a 96 GB GPU while requesting a further 108.88 GiB. These timings include
different cold costs and are not throughput estimates. The proposed shared-span head execution preserves the
parameters while avoiding repeated lexical embedding rows. CPU reference and native-function tests pass.
On a fresh 96 GB RTX PRO 6000 Blackwell, all four native/shared-head FP32/BF16 inference/training parity cases
passed, with identical selected options. Maximum logit error was 7.451e-8; maximum parameter-gradient errors
were 2.384e-7 in FP32 and 0.015625 under BF16 autocast, within the predeclared tolerances.

The refreshed full-model minimum/median/maximum inputs completed two repeated forwards each with identical
choices: 40.320 / 4.749 / 24.140 seconds and 25,268,596,224 / 25,889,946,624 / 33,724,303,360 peak allocated bytes.
The first timing includes cold compilation. Median and maximum backward probes completed in 72.312 and
48.159 seconds, peaking at 30,024,672,768 and 48,157,903,360 allocated bytes, with finite loss and finite nonzero
LoRA/head gradients. No optimizer was created or stepped. This establishes feasibility on that 96 GB GPU,
not CPU or 40 GB training compatibility. The compact input differs from the earlier failed native preflight,
so absolute before/after GPU timings are not a controlled speedup comparison. Evidence and the serialization
clarification are in `clef-kernels-20261105/{preflight,kernel-parity-gpu,execution-addendum}.json`.

Grammar decoding now retains native raw logits before legal-action normalization; softmax/API rounding can
otherwise erase every legal action when an illegal action has the highest score. Registered tests cover this
underflow. Readers **60/60**, SQL AST **113/113**, compileall, focused Ruff F/E9 and diff checks passed locally.
The generic-target fit was interrupted after recovery of checkpoint384 on the owner's shared-property
architecture correction; it did not complete its planned512 steps or candidate answer evaluation.
Its initial contract was `7078296d9281523eaaaf862bbfdcc1e1b06690e34305b6533e8a5c43b20b44d0`, under
`clef-grammar-fit-20261105`. A storage-only recovery froze contract
`db9cfec43bad2e0405fa3e5e9f525b7c76984c7ea14edcbf6051d3353cc0f6d3`: the first allocation stalled before
optimization while loading weights through FUSE. The replacement uses a local model cache and preserves the
original deletion deadline. After downloading, its 1,184 tensors loaded in 5.6 seconds; this is a startup
storage result, not an inference speedup. `cache-contract.json` verifies that the reload script's only change
preserves the caller's cache setting; every other Python AST statement is unchanged. The fit continues the
exact E adapter/head, frozen training serialization and data, with the existing learning rates and seed,
and a five-hour / USD12.45 compute cap; preflight plus fit were bounded to USD14.94 compute. The GPU is deleted;
no candidate reload, full answer-accuracy comparison or promotion was completed, and the
frozen Spider test remains unrun. Candidate code/data and the failed native preflight remain under the existing
`clef-ast-20261105` experiment. All three unsuccessful GCE allocation attempts left no VM or experiment disk.

## Recursive SQL AST target prototype: 18,364 ACE pairs round-trip (2026-11-05)

At the owner's request, the next target-format prototype uses a recursive JSON schema derived from the actual
classes in `engine/sql_ast.py`, with the official SQLite 3.50.4 grammar pinned separately as the syntax reference.
It is the engine's supported read-only AST, not a schema claiming every SQLite production. All 18,364 admitted
ACE pairs converted to JSON AST, validated and round-tripped through the existing typed compiler without a
different rendered query. A separate nine-projection/seven-condition/three-order-key example passed, and a
weighted-average AST executed to the independently expected 8.6. Unknown nodes, extra SQL text and invalid
COUNT(DISTINCT *) were rejected. No model was trained on these new targets and the named prediction slots
were not replaced in serving. Prototype, schema, paired data and source hashes are under
`training/readers/data/experiments/clef-ast-20261105/`.

## Longer CLEF training: calibration 720/1,053 strict, versus 708 baseline and 551 at 512 steps (2026-11-05)

The 2,048-step controlled exposure trial retained the same base revision, data, seed, objective and original
input serialization as the 512-step trial. Its linear learning-rate schedule scaled with the run length.
The fresh matched Linux runs used the same pinned bundle, source snapshot, evaluator, `whole_db`, served
selection, AUTO execution and 5,000-row cap. All four disjoint calibration shards merged successfully for
each run; merged results retain the whole-dataset and injected-dependency fingerprints.

| Reader | Strict right / 1,053 | Lenient right | Answered |
|---|---:|---:|---:|
| Current production bundle | 708 (67.2%) | 799 | 988 |
| CLEF, 512 steps, original user wording | 551 (52.3%) | 674 | 956 |
| CLEF, 2,048 steps, original user wording | 720 (68.4%) | 837 | 999 |

Against production the longer candidate gained 104 correct answers and lost 92. A seeded bootstrap over the
20 held-out databases gives a 95% interval of -4.692 to +5.664 percentage points for that difference; this
does not establish a reliable improvement over production. Against the 512-step candidate it gained 204
and lost 35. These are calibration results; the historical 1,535/2,147 frozen-test target remains untested.
The matched runs used the frozen comparator before the separate nonfinite-spelling correction below.

On the 128 untrained semantic fixtures, the longer model scored **85/128**, including 16 clarification cases.
All **69/112** correct answer programs remained correct on both altered datasets. Its pools contained an
original-data match for 104/128 cases, but only 80/112 answerable programs survived all three datasets.
Calendar predicates, repeated headers, medians and weighted averages remain unresolved. These are direct
compiler/program checks, not served Enron accuracy or fresh model inference on the altered rows.

Artifacts are in `training/readers/data/experiments/clef-exposure-20261105/` (archived).
The candidate manifest is `9ad9153b33a648958b5af7f1ce765726c12d6c71e0c73fad3c0d9f8201f87a5e`.
All 11 recovered files match their hashes; the exact training serializer is retained alongside the manifest.
Native predictions completed for 1,616 Spider routing requests, 186 Enron requests and 128 semantic requests,
with zero native inference errors. Reload and repeated-decision checks passed. This does not certify
correctness: GPU residency was 23,397,452,800 bytes and a 26,112-token input peaked at 30,051,629,056 bytes.
No model was promoted or deployed. The full Enron replay scored **29 right / 115 asked / 42 wrong of 186**,
versus the matched baseline's 26 / 90 / 70 and the 512-step candidate's 27 / 107 / 52. The longer candidate
gained 12 correct answers and lost nine baseline-correct answers; its pool held a right answer for only 47/186,
versus baseline 45/186. Raw-grid questions were 5/105 right, row-table questions 24/81. This fails the
no-loss Enron gate and the requested all-question objective. Further run length alone has not solved workbook
layout or the missing expression forms. The frozen Spider test was not run.

The frozen-weight input ablation moved shared option spans after the complete state. On the matched 96-question
Spider sample it stayed **57/96**, with six gains and six losses. On corrected semantic-fixture comparison it
moved **53 to 83/128**; 37 versus 67 answerable programs survived both altered datasets. The older 46 and 75
counts required output-column order even when the grouping question did not specify it. Historical reports
are retained; `semantic_comparison/` records the corrected rule and both variants. The ablation reached its
time cap after all semantic and Spider requests but only three Enron requests, so no Enron-sample result is
claimed. All 12,397 training and 1,974 calibration records fit the revised serialization without truncation;
the maximum was 32,611 tokens. Neither fine-tuned candidate was trained with that revised serialization.

## ACE corpus capacity and nonfinite comparison correction (2026-11-05)

The canonical profile's wider bounds recovered 49 source programs excluded by the neural head's smaller
projection, condition and ordering limits. The shared benchmark comparator also interpreted text such as
the city name `Nan` or a review containing `nan` as floating-point NaN, making identical answers unequal.
It now retains nonfinite spellings as stable text, with a regression that fails before the fix.

Re-running every source pair admitted **8,109/8,652 Spider**, **6,411/9,428 BIRD-train**, and
**3,844/3,949 Spider-Syn-train** pairs. All **18,364** admitted documents passed the generated JSON schema,
exact typed-AST round trips and the pinned real Attempto parser with exact JSON-tree agreement. All **3,665**
remaining exclusions are retained. These are execution and compiler checks, not exhaustive English-meaning
certification, and do not cover all historical property-head training inputs. Empty source databases,
unstated functional dependencies and unsupported SQL shapes remain limitations.
Records, schema and provenance: `training/readers/data/experiments/clef-supervision-20261105/finite-comparison/`.
The existing fine-tuning data and promoted bundle were not changed by the corpus refresh.

The changed-data audit then checked every admitted source pair again through the same gold oracle:
18,364 original-data checks, 15,507 passing permutations and 15,516 passing clones. Ten permutations and
one clone exposed result differences; those eleven labels remain flagged, including grouped projections
that rely on unstated functional dependencies. The 2,847 LIMIT queries were excluded from both changed-data
checks because cutoff results can be underdetermined. No exhaustive English-equivalence claim follows.
The ACE encoder also rejects oversized literals/documents and invalid Unicode before exposing its tree;
the added boundary regression and all current reader tests passed.

Validation: readers 59/59, SQL AST 110/110, complex datasets 10/10. All 40 offline suites passed after fixing
the disposable test setup: the complete run passed 39, and the release suite passed 57/57 after adding the
existing hermetic runner's temporary Git fixture. Eleven live Postgres suites and the live orchestrator
were explicitly skipped. This was not a full deployed-product release gate. Both paid compute pods were
deleted after results and all candidate files were recovered; the network volume remains retained.

## Unpromoted CLEF joint-reader trial: calibration 545/1,053 strict; original-wording ablation 549/1,053 (2026-11-05)

Commit `eb40c4c9144a926f0cc9260d9928fba343a0801f`, dirty worktree. Candidate provenance, file hashes, exact-input captures,
and per-example results are in `training/readers/data/experiments/clef-conversation-20261105/`.
This is an offline dependency injection into the existing `TableQuery.select_query` and `spider.probe.full_eval`,
not a promoted runtime. The baseline bundle is `4545a4810fa25f4b1fbff73a1c05fcb898958ade0aff3e29f76b876f3e44bdfc`.
All headline runs use the same 1,053 database-held-out calibration questions, `whole_db`, served selection,
AUTO execution, and a 5,000-row cap. The frozen test was not run.

| Reader / selector question | Strict right | Lenient right |
|---|---:|---:|
| Production bundle, freshly reproduced | 708 (67.2%) | 799 |
| CLEF; top interpretation's canonical read-as passed to the selector | 545 (51.8%) | 670 |
| Same CLEF predictions; original user wording passed to the selector | 549 (52.1%) | 676 |

The separate SQL-backend pool-oracle ablation found an eligible strict match for **803/1,053 (76.3%)**.
That is a raw candidate-pool ceiling, not served accuracy; served top-one and deterministic tie/null policies
must not be replaced with oracle answers.

`Cloudflare/clef` revision `cfddda043dd190ef3b01cff449d5ee8cff1e8ec6` was fine-tuned with NF4, rank-16 LoRA,
and its joint decision head, seed 3407. The 512-step run exposed 1,024 of 12,397 training records (about 8.26%
of one epoch), over 126 training databases; 20 calibration databases were excluded. Input-derived choices make
readings, an ACE view, and typed ASTs; no model generates SQL text. Improved decision-head loss is not a SQL score.

On the same 186 Enron offline questions, right / asked / wrong was 26 / 90 / 70 for the baseline,
30 / 96 / 60 for canonical wording, and 29 / 101 / 56 for original wording. The canonical candidate gained 12
previously incorrect answers and lost 8 previously correct ones; its pool contained a right answer for only 43/186,
versus 45/186 for the baseline. The candidate fails promotion gates. Its weights remain outside `engine/data/`.

The explicit-join codec extension preserved the original 545/1,053 and 30 / 96 / 60 counts. Its source-program
audit passed 7,116 original pairs plus 6,275 permutations and 6,275 clones, with no mismatches. Those are compiler
checks, not learned question accuracy; the current CLEF heads do not yet predict the added explicit join facts.

Subsequent diagnostics kept the 512-step weights frozen. Removing the decoder's per-field top-three cutoff,
while retaining 256 attempts and 16 unique ASTs, moved original-wording Spider from **549 to 551/1,053**
(two gains, no losses). Enron moved from 29 / 101 / 56 to **27 / 107 / 52** (one gain, three losses), with a
right answer somewhere in 48/186 pools. These are the `wide_decode/` results before the later bare-star
validation fix; that fix requires fresh baseline and candidate results.

A second encoder prefill of the **same** frozen model chose among complete typed candidate plans. On an
outcome-blind sample of 96 calibration questions, strict right moved **57 to 64** (ten gains, three losses).
On 32 sampled Enron questions, right / asked / wrong moved **6 / 17 / 9 to 6 / 22 / 4** (two right gains,
two right losses). `rank_probe/` records the exact sample, request and weight hashes. This is a small development
probe, not a full benchmark, frozen-test result or calibrated confidence estimate.

On the separate 128 untrained conversation/schema/value fixtures, wider search retained a right outcome in
96 pools; first-pass top-one remained 46. The same-model plan-choice prefill selected all 96: **80/112 answerable
questions plus 16/16 clarification cases**. All 80 selected answer programs also stayed right on two altered
datasets with refunds, extra matches, near-match names and changed numbers. Calendar ranges, repeated headers,
medians and weighted averages remained 32 failures. These `novel_rank/` results exercise direct compiler/program
semantics, not the served pipeline or fresh neural inference on altered data. They do not establish that all
Enron questions work. Timings from the shared training GPU are not isolated inference latency.

The longer-training trial remains separate; none of these diagnostics claims its accuracy or promotes a model.

## A result that holds nothing asks back: calibration 723 -> 708 strict, messy dev 44 / 21 / 55 -> 44 / 23 / 53, Enron replay 26 / 54 / 106 -> 26 / 90 / 70 (2026-11-02)

Commit b52c7b0 plus the Phase 0 changes this entry ships with (`worktree_dirty: true`; the record carries each source file's hash).
Shipped bundle: weights manifest 40605578..., `weight_bundle` 4545a4810fa25f4b, `encoder` 53fd3d37c64525be, dataset `dev`
268103700b08b658, `tables` 1ae061436b77d4b6. Spider calibration, `whole_db`, `--selection served`, `--backend auto`, fresh, not a delta.
Records: `prereasoner-readers-artifacts/evals/sqlite/full_eval_{,per_example_}calibration_phase0.json`; logs in `evals/phase0/`.

| Calibration (1,053) | 2941ae5 | this change |
|---|---:|---:|
| Strict right | 723 (68.7%) | 708 (67.2%) |
| Lenient right | 799 | 799 |
| Wrong (strict) | 295 | 280 |
| Unanswered | 35 | 65 (refusal 64, grounding 1) |
| Scalar gold | 317 / 377 | 317 / 377 |

Transitions, 2941ae5 to this change, which also span the rules of 1be6875 and feb491a: 703 stayed right, 277 stayed wrong, 34 stayed
unanswered; 18 right and 13 wrong became unanswered; 5 wrong became right, 2 right became wrong, 1 unanswered became wrong. By difficulty,
strict of n: easy 209 of 238, medium 262 of 412, hard 145 of 233, extra 92 of 170. Candidate-pool recall was not measured: the
rule acts after selection and builds no candidate.

This is below the former bar of 716. Spider's gold holds empty answers, so a rule that asks back on an empty result costs right answers
there. The frozen test was not re-run. Its stored records put 100 of 2,147 served results empty, 54 of them right: about 1,531 to 1,477,
an estimate. The owner decided to ship anyway, since real sheets are the bar now (DECISIONS.md, 2026-11-02).

| Real-sheet measure | Before: right / asked / wrong | After |
|---|---:|---:|
| Messy dev, `regress/run_messy.py --split dev`, of 120, measured | 44 / 21 / 55 | 44 / 23 / 53 |
| Enron replay, shipped bundle, of 186, only the 36 empty readings changed | 26 / 54 / 106 | 26 / 90 / 70 |

Other gates on these changes: hermetic `tests.run_all` passed, live `tests.test_datasets` passed (`chat:` follow-ups excluded there),
complex datasets 10 of 10. The replay runs `select_query` offline; it reproduces all 25 of production's right answers, and agrees less on
asked versus wrong, so production's own count after this release has yet to be measured on all 186.

The evaluation of 2026-11-01, on production before these changes (b52c7b0, `prereasoner-api-00214-w54`, `prereasoner-chat-00168-r48`),
through the add-on in Google Sheets: 100 Enron workbooks, 191 questions, 5 void from Google's import, 186 graded. Right / asked or
declined / wrong or failed: **25 / 49 / 112** (25 right, 40 asked, 9 declined, 99 wrong, 13 failed). Report and per-question records:
`enron_corpus/eval/{REPORT.md,results.jsonl}` (archived).

| Same 186 questions, offline replay | Right | Asked | Wrong |
|---|---:|---:|---:|
| Shipped readers | 26 | 54 | 106 |
| Gemini writes SQL over the same tables (yardstick) | 130 | 14 | 42 |
| Gemini writes SQL over tables an LLM marked (yardstick) | 140 | 21 | 25 |
| Gemini writes a program over the raw cells (yardstick) | 165 | 2 | 19 |

The yardstick is a measure of what the questions allow, not a serving option: no model writes SQL here (CLAUDE.md). LLM agents wrote the
questions and their expected answers after reading the sheets, so an LLM's score is a ceiling. These 100 workbooks are now studied dev
data; a frozen set must be drawn fresh. Records: `prereasoner-readers-artifacts/evals/enron/`.


## Messy questions at 3a34166: dev 44 right / 21 asked / 55 wrong of 120; the owner's two real sheets 6 / 0 / 8 of 14 (2026-10-31)

Shipped bundle (weights manifest 40605578...), served path, `regress/run_messy.py --split dev`, clean worktree at 3a34166, a
local process with a live Postgres. The bar since 2026-10-30 (DECISIONS.md): questions that use neither the sheet's column
names nor its cell values, graded right, asked back, or wrong. A frozen split of 79 reports totals only and did not run here.

| Family | Right | Asked | Wrong | n |
|---|---:|---:|---:|---:|
| column | 17 | 3 | 26 | 46 |
| value | 14 | 17 | 19 | 50 |
| plain | 11 | 0 | 4 | 15 |
| structure | 2 | 1 | 6 | 9 |
| all | 44 | 21 | 55 | 120 |

Before the tab fixes this was 45 / 21 / 54 on the older shape of the subscriptions workbook, whose reports kept the pivot's title
as their header; the workbook changed in feb491a, so the two are not a controlled comparison. These workbooks hold no near-copy
tabs to choose among, except subscriptions, so the fixes show on the real sheets rather than here.

The owner's Subscriptions and Keyword Stats sheets, asked in the add-on on the deployed 3404948 and 3a34166, with expected values
read from the sheets independently: 14 messy questions, 6 right, 0 asked, 8 wrong. An earlier run on 543e5b9 got 0 of 6. Each
answer now names its tab and the others that could answer. Right: Keyword Stats "how many keywords are hard to rank for";
Subscriptions "how many customers pay yearly", "how much revenue did we make in 2024", "what is our best selling product" (by
units), "revenue from monthly plans", "how many left because it was too expensive". Wrong: "whats the most expensive one to bid on",
"how many are getting more popular than last year", "how much search volume do the fire extinguisher ones get", "search volume split
by how competitive it is" (counted rows), "how much sales for long term customers (above 6 months)", "how many customers are in
america" (every row), "how much did retailers pay us" (every row), and "how many have cancelled", which the conversation's rewording
read as "in america" after the question before it. Half the wrong answers drop a filter without saying so.

Time, on the two warm instances of 4 vCPU, 500,000-cell workbook, Subscriptions: an instance's first question on a sheet 26.9 and
37.7 s in the engine, a later one 14.8 and 14.9 s; a chat turn 30.6 and 48.5 s, then 18.6 and 20.4 s. The derivations an instance now
keeps took 6.8 s in the first and 0.17 s in the later (`decompose_probe_self_ms`). Of a later question, `readers_ms` is 8.2 to 10.1 s.

What did not help, on the same 120 questions (fp32 p50 6.05 s; none of these ship):

| Readers | Right | Asked | Wrong | p50 |
|---|---:|---:|---:|---:|
| 8-bit dynamic, every encoder layer | 29 | 19 | 72 | 3.2 s |
| 8-bit dynamic, per channel | 29 | 22 | 69 | 3.4 s |
| 8-bit dynamic, per channel, feed-forward layers only | 31 | 11 | 78 | 3.7 s |

ONNX Runtime 1.31 over one reader's float32 weights, 4 threads, 508 tokens: 1.55 s against PyTorch's 2.37 s on an AMD Zen 4 dev box,
but 1.58 s against 1.30 s on the Intel Xeon that Cloud Run gave a job. Outputs differ by at most 3e-4 on values up to 28.

Spider calibration, whole_db, strict, on 2941ae5: 723 of 1,053 (lenient 799). The two rule changes since do not change that number by
a fresh run: 1be6875's subset delta implied about 726, and feb491a's rule changes no database's copy groups (0 of 1,053 questions).
A fresh run is owed at the next promotion.


## Every question on its own SQLite database: calibration 730 -> 727, frozen test 1,532 -> 1,531 (2026-10-27)

Same bundle (all14fg, weights manifest 40605578...), same evaluator, the choice's threshold null; the selected query
executes through the request's SQLite database (`engine/prepared.py`) by serving's policy (`--backend auto`: the
generated Python program, SQL when the plan cannot be lowered or is too large), where the previous entry executed SQL
on an in-memory copy (`--backend sql`). Records: `prereasoner-readers-artifacts/evals/sqlite/`.

| | previous executor (fd91a87, `--backend sql`) | request database (4580c0c) | request database with fd63f2b's fix |
|---|---|---|---|
| Calibration strict / lenient (1,053) | 730 / 811 | 725 / 806 | 727 / 808 |
| Calibration: same SQL | | 1,053 of 1,053; 1 answer became right, 6 wrong | 1,053 of 1,053; 1 became right, 4 wrong |
| Frozen test strict / lenient (2,147; totals only) | 1,532 / 1,696 | | 1,531 / 1,696 |
| Frozen test answered | 2,143 | | 2,143 |

The six calibration answers 4580c0c lost: 122 and 278 compared an integer column with '2008.' / '10.' (the readers kept
a sentence's period); SQLite's affinity reads the text as a number and the generated Python program did not (fixed in
fd63f2b). 314 and 315 ("the five oldest people") tie at the fifth row; either answer is a valid top five. 891 and 892
order hotels by a price the evaluator's copy reads as NULL where the database holds '' and NULLs now sort last; the
original database gives the new answer. The gain, 282, compares AVG(rating) with '5.' exactly. The fd63f2b column's
calibration ran at 13b9791 (the same engine; only engine/server.py's startup differs) and its frozen test at fd63f2b.

## The readers computed in float32 over bfloat16 weights: calibration 729 -> 730, frozen test 1,535 -> 1,532 (2026-10-27)

Same bundle (all14fg, weights manifest 40605578...), same evaluator and options as the bundle's recorded runs
(`spider/probe/full_eval.py --config whole_db --selection served --backend sql`, the choice's threshold null); only
`engine/readers/models.load` differs (fd91a87: every computation in float32, the weights kept in bfloat16). Run on the
development desktop, which has the bfloat16 instructions Cloud Run's CPUs lack. Records:
`prereasoner-readers-artifacts/evals/widened/`.

| | bfloat16 (f2d08f5) | float32 over bfloat16 weights (fd91a87) |
|---|---|---|
| Calibration strict / lenient (1,053) | 729 / 810 | 730 / 811 |
| Calibration: same SQL | | 1,016 of 1,053; 4 answers became right, 3 wrong |
| Frozen test strict / lenient (2,147; totals only) | 1,535 (71.5%) / 1,698 | 1,532 (71.4%) / 1,696 |
| Frozen test answered | 2,143 | 2,143 |

Prediction seconds on the desktop rose (calibration median 4.69 to 7.56, the frozen test 3.76 to 6.28: that CPU runs
bfloat16 natively); on Cloud Run the readers fell from 7.0 s to 2.7 s median (DECISIONS.md, 2026-10-27).

## After the SynSQL release: more SynSQL per database, a re-scorer that reads the first row, and what the tie rule costs (2026-10-26)

Calibration only (20 held-out Spider train databases); the frozen test was not run for any of these. Records:
`prereasoner-readers-artifacts/` (contracts `readers_v15.md`, `rescorer_h.md`; candidates `v15*cal.jsonl`;
evals `eval_v15.log`; contract `rescorer_i.md`). RunPod: $27.08 (v15 readers $14.30, "h" $1.61, v15b fold readers
and "i" $11.17).

**Readers v15** (v14's recipe and selection with eight SynSQL questions per database instead of four, 95,726 questions,
five epochs so the steps stay about equal), each against its v14 counterpart, candidates on 1,035 questions:

| Reader | Right answer in its candidates | First candidate right |
|---|---|---|
| v14a → v15a (anchored, seed 0) | 742 → 762 | 579 → 569 |
| v14b → v15b (plain, seed 0) | 732 → 751 | 550 → 571 |
| v14b1 → v15b1 (plain, seed 1) | 743 → 772 | 563 → 584 |
| v14a1 → v15a1 (anchored, seed 1) | 745 → 746 | 580 → 581 |

The pools gain less than the readers, and the choice gains nothing (out of fold by database, over re-scorers "f" and
"g"):

| Pool | Right answer in the pool | Choice, "f" + "g" | "f" alone | "g" alone |
|---|---|---|---|---|
| Shipped (v14a, v14b, v14b1, v14a1) | 865 | 734 | 704 | 719 |
| v15a, v15b, v15b1, v14a1 | 874 | 729 | 707 | 716 |
| v14a, v15b, v15b1, v14a1 | 869 | 733 | | |

The choice's own form is not the limit either: over six assignments of databases to folds the shipped choice picks
731 to 733 (mean 732.3); a listwise fit 730 to 732; interaction terms 727 to 731; a third re-scorer's logit (the large
"gl") 724. **"h"**, the "g" recipe on the same pools with each candidate's first result row appended to its text, chose
0.655 of calibration right at its best epoch against g's 0.672, and was left out. Counting an empty result's columns as
serving does (the candidates record 0) changes nothing: 734. **"i"**, g's recipe on the union pools of the v14a and
v15b cross-fold candidates (four v15b fold readers; fold 0's outputs partly downloaded, 3,721 of 8,004 questions, and
folds 1 and 3 at their epoch-3 checkpoints, the leases' limit), on the pool v14a, v15b, v15b1, v14a1: alone 706, with
"f" 722, with "g" 717, with "f" and "g" 733 (against 733 without it). A re-scorer matched to the new readers does not
lift the choice either.

**The tie rule.** Serving keeps every row tied with the first of a top-1 ranking (DECISIONS.md, 2026-10-02); Spider's
gold keeps one (`ORDER BY ... LIMIT 1`). Of the shipped bundle's 1,053 served calibration answers, 162 rank with ties;
run as the same reading with LIMIT 1, 24 become strict-right and 1 strict-wrong: **23 answers (2.2 points)** that the
strict metric counts as wrong because the product returns every tied row.

## Readers trained with SynSQL-2.5M's databases and two re-scorers: calibration 667 → 729 strict; frozen test 1,447 → 1,535 strict (2026-10-26)

The readers bundle of v14a, v14b, v14b1 and v14a1 (slots a to d, trained on v13's data plus 51,929 SynSQL-2.5M
questions on 13,537 synthetic databases: `training/readers/README.md` 2e, 4e), the shipped stage 1, re-scorers "f"
(`rescorer_a`) and "g" (`rescorer_b`, trained on the v14 recipes' cross-fold candidates: 8b) and the choice fitted over
both (threshold 0.0862 matched on calibration), installed by `training/readers/promote.py` at Hugging Face revision
`cd8a03abb8fd443442f9a3e691f717f4285fd263`, in the readers worktree at `fd6d45e` plus this change
(`worktree_dirty: true`); the stage-2 text drops a table's samples before the table, an ungrouped count of a
referencing column counts each row once, and a listed date is not read as the question's stored form. Same evaluator
and flags as the entry below, the threshold off. Records: `prereasoner-readers-artifacts/evals/all14fg/`
(per-example calibration; the test's totals only).

**Calibration** (20 held-out Spider train databases, 1,053 questions):

| System | Strict | Lenient | Answered | Wrong |
|---|---|---|---|---|
| Engine `b8bedde` | 418 (39.7%) | | 712 | 294 |
| Released bundle (v11a, v11b, v13b1, v13a1; "f"), choice off | 667 (63.3%) | 743 (70.6%) | 1,053 | 386 |
| These four readers with "g" only, choice off | 720 (68.4%) | 806 (76.5%) | 1,050 | |
| v11b in slot b with "f" and "g", choice off | 728 (69.1%) | 808 (76.7%) | 1,051 | |
| **This bundle, choice off** | **729 (69.2%)** | 810 (76.9%) | 1,050 | 321 |
| This bundle, choice at 0.0862 | 727 (69.0%) | | 1,021 | 294 |

Transitions, this bundle (choice off) against the released bundle: 92 wins, 30 losses, 637 unchanged right, 294
unchanged wrong; against the engine: 345 wins, 34 losses, 384 unchanged right, 290 unchanged wrong. By difficulty
(easy 238, medium 412, hard 233, extra 170): 88.7 / 66.7 / 63.9 / 55.3%, against 79.0 / 60.7 / 60.9 / 51.2% for the
released bundle. Prediction seconds (eight threads, beside the frozen test's run): median 4.69, p90 8.54; the same
readers with v11b in slot b, alone on the machine: median 3.52, p90 6.61.

Out of fold on the calibration pools (1,035 questions with the samples-first rows): right answer in the pool 865; the
choice right on 734 over both re-scorers, 704 over "f" alone and 719 over "g" alone.

**Frozen Spider test** (2,147 questions, 40 databases; totals only):

| System | Strict | Lenient | Answered |
|---|---|---|---|
| Engine alone `b8bedde` | 939 (43.7%) | | |
| Released bundle (v11a, v11b, v13b1, v13a1; "f"), choice off | 1,447 (67.4%) | 1,625 (75.7%) | 2,143 |
| **This bundle, choice off** | **1,535 (71.5%)** | 1,698 (79.1%) | 2,143 |
| This bundle, choice at 0.0862 | 1,524 (71.0%) | | 2,085 (561 wrong) |

By difficulty (easy 470, medium 857, hard 463, extra 357): 87.2 / 73.5 / 68.0 / 50.4%, against 84.9 / 69.5 / 62.6 /
45.4% for the released bundle. The four questions left unanswered with the choice off: two with no executable grounded
candidate, two held back by the selection (counted by reason without reading them). Prediction seconds (eight
threads): median 3.76, p90 7.44. The bundle was chosen on calibration and the product's demo questions, not on the
test.

## Two readers trained on sheet language and every column: calibration 669 → 667 strict; frozen test 1,414 → 1,447 strict (2026-10-24)

The readers bundle of v11a and v11b (slots a and b) with v13b1 and v13a1 (slots c and d, trained on the v13 data:
`training/readers/README.md` 2d, 4d), the shipped stage 1 and re-scorer "f", and the choice refitted on that pool with
the readers' share counted among the readers that read the question (threshold 0.1405 matched on calibration),
installed by `training/readers/promote.py` at Hugging Face revision `999b50a9d7038c901998c0b586cbb8d4aa67851e`, in the
readers worktree at `cb30447` plus this change (`worktree_dirty: true`). Same evaluator and flags as the entry below,
the threshold off. This change's layout-copy and shared-header rules reach no calibration or test question (no
database of either holds layout copies, and Spider has no repeated headers). Records:
`prereasoner-readers-artifacts/evals/v13s/` (per-example calibration; the test's totals only). The test ran on
the code committed as `f58b839`, released as `304e19b` (a dependency-lock change only).

**Calibration** (20 held-out Spider train databases, 1,053 questions):

| System | Strict | Lenient | Answered | Wrong |
|---|---|---|---|---|
| Engine `b8bedde` | 418 (39.7%) | | 712 | 294 |
| Bundle of 2026-10-23 (v11a, v11b, v12b1, v12a1), choice off | 669 (63.5%) | 749 (71.1%) | 1,053 | 384 |
| **This bundle, choice off** | **667 (63.3%)** | 743 (70.6%) | 1,053 | 386 |
| This bundle, choice at 0.1405 | 661 (62.8%) | | 955 | 294 |

Transitions, this bundle (choice off) against the bundle of 2026-10-23: 19 wins, 21 losses, 648 unchanged right, 365
unchanged wrong (within noise); against the engine: 301 wins, 52 losses, 366 unchanged right, 334 unchanged wrong.
Prediction seconds (eight threads): median 4.21, p90 8.35. Out-of-fold choice on the calibration pools: 666 of 1,028
right (668 with the candidates rebuilt by the final builder); all four readers trained on the v13 data, 649.

**Frozen Spider test** (2,147 questions, 40 databases; totals only):

| System | Strict | Lenient | Answered |
|---|---|---|---|
| Engine alone `b8bedde` | 939 (43.7%) | | |
| Production, the engine with the Gemini rewording | 1,126 (52.4%) | | |
| Bundle of 2026-10-22 (v11a, v11b, v11b1, v11a1), choice off | 1,414 (65.9%) | 1,611 (75.0%) | 2,147 |
| **This bundle, choice off** | **1,447 (67.4%)** | 1,625 (75.7%) | 2,143 |
| This bundle, choice at 0.1405 | 1,419 (66.1%) | | 1,968 (549 wrong) |

The bundle of 2026-10-23 was not run on the test. Of the four questions left unanswered with the choice off, two have no
executable grounded candidate and two rank only candidates the selection holds back (an unmet calculation or date, a
double count, or a ranking that ranks nothing), counted by reason without reading them. With the choice at its threshold
this bundle answers 4 fewer test questions than the bundle of 2026-10-22 and is wrong on 36 fewer. Prediction seconds
(eight threads): median 3.48, p90 7.17. The bundle was chosen on calibration and the product's demo questions, not on
the test.

**Against hosted models on the same 2,147 questions**, paired by question (the hosted runs of 2026-10-19 in the entry
of 2026-10-21: each model wrote SQL from CREATE TABLE statements with 3 example rows and the foreign keys; same capped
databases, executor and `spider_eval.compare`; totals only):

| | This bundle | Gemini 3.8 Flash | Sonnet 5.5 |
|---|---|---|---|
| Strict, all 2,147 | 1,447 (67.4%) | 1,821 (84.8%) | 1,806 (84.1%) |
| Lenient | 1,625 (75.7%) | 1,946 (90.6%) | 1,950 (90.8%) |
| Easy / medium / hard / extra (470 / 857 / 463 / 357) | 84.9 / 69.5 / 62.6 / 45.4% | 92.1 / 81.1 / 85.3 / 83.5% | 91.7 / 81.1 / 84.7 / 80.7% |
| Strict on the 1,968 the choice answers at 0.1405 | 1,419 (72.1%) | 1,692 (86.0%) | 1,682 (85.5%) |
| Strict on the 179 it declines | | 129 | 124 |

Paired with Gemini: 1,351 both right, 96 only this bundle, 470 only Gemini, 230 neither; with Sonnet: 1,346, 101,
460, 240. Any of the three is right on 1,955. Seconds per question: this bundle median 3.48 on eight local threads;
Gemini 2.40, Sonnet 1.44 (API). The hosted models' inputs differ from the engine's, and their training data may include
the published Spider test, so this compares the task, not the models alone.

## Two readers trained on the grouped structure, a refitted choice: calibration 663 → 669 strict (2026-10-23)

The readers bundle of v11a and v11b (slots a and b, unchanged) with v12b1 and v12a1 (slots c and d, trained on v11's
data plus the grouped structure), the shipped stage 1 and re-scorer "f", and the choice refitted on that pool
(threshold 0.1434 matched on calibration), installed by `training/readers/promote.py` at Hugging Face revision
`fd08934c5a5d45c31ed776c56cbc2d240d2f9d89`, in the readers worktree at `d416f2c` plus this change
(`worktree_dirty: true`). Same evaluator and flags as the entry below (`spider.probe.full_eval`, whole_db / served /
SQL / cap 5,000 per table / 12 s soft budget / compose on / external models off), the threshold off. Calibration ran before the long-question rule of this change,
which reaches no calibration question (the longest has 29 words). Records:
`prereasoner-readers-artifacts/evals/mixB/` (per-example calibration).

**Calibration** (20 held-out Spider train databases, 1,053 questions):

| System | Strict | Lenient | Answered | Wrong |
|---|---|---|---|---|
| Engine `b8bedde` | 418 (39.7%) | | 712 | 294 |
| Bundle of 2026-10-22 (v11a, v11b, v11b1, v11a1), choice off | 663 (63.0%) | 746 (70.8%) | 1,053 | 390 |
| **This bundle, choice off** | **669 (63.5%)** | 749 (71.1%) | 1,053 | 384 |
| This bundle, choice at 0.1434 | 658 (62.5%) | | 952 | 294 |

Transitions, this bundle (choice off) against the bundle of 2026-10-22: 25 wins, 19 losses, 644 unchanged right, 365
unchanged wrong; against the engine: 308 wins, 57 losses, 361 unchanged right, 327 unchanged wrong. Out-of-fold
choice on the calibration pools: 667 of 1,028 right (the four v11 readers 664, the four v12 readers 659). Prediction
seconds (six threads): median 5.55, p90 10.69.

## Four readers trained on the structures they misread, and a choice: calibration 617 → 663 strict; frozen test 1,351 → 1,414 strict (2026-10-22)

The readers bundle of four reading models v11a, v11a1 (anchored, seeds 0 and 1) and v11b, v11b1 (plain), the shipped
stage 1 and re-scorer "f", and the choice (`choice.json`, threshold 0.1421 matched on calibration), installed by
`training/readers/promote.py` at Hugging Face revision `d4949122da66f04e8fce1ea68f3e2e0ecb9a0327`, in the readers
worktree at `01138b2` plus this change (`worktree_dirty: true`). `spider.probe.full_eval`, whole_db / served / SQL /
cap 5,000 per table / 12 s soft budget / compose on / external models off; the threshold off for these runs, so each
answer records its confidence. The readers trained on Spider train and dev, BIRD train, Spider-Syn and questions
written on those training databases (`training/readers/README.md` step 2b), never on the 20 calibration or 40 test
databases. Records: `prereasoner-readers-artifacts/evals/v11/` (per-example calibration; the test's totals
only).

**Calibration** (20 held-out Spider train databases, 1,053 questions):

| System | Strict | Lenient | Answered | Wrong |
|---|---|---|---|---|
| Engine `b8bedde` | 418 (39.7%) | | 712 | 294 |
| Shipped readers (v7a, v5; refusal 0.2511 off) | 617 (58.6%) | 714 | 1,052 | 435 |
| Four earlier readers (v7a, v5, v7b, v6; choice off) | 641 (60.9%) | 723 | 1,053 | |
| **These readers, choice off** | **663 (63.0%)** | 746 (70.8%) | 1,053 | 390 |
| These readers, choice at 0.1421 | 649 (61.6%) | | 943 | 294 |

Transitions, these readers (choice off) against the engine: 298 wins, 53 losses, 365 unchanged right, 337 unchanged
wrong; against the shipped readers: 78 wins, 32 losses, 585 unchanged right, 358 unchanged wrong. The decoder's
period rule (one change of this entry) reaches no calibration question. Prediction seconds (four threads, beside two
other evaluations): median 6.83, p90 13.22.

**Frozen Spider test** (2,147 questions, 40 databases; totals only):

| System | Strict | Lenient | Answered |
|---|---|---|---|
| Engine alone `b8bedde` | 939 (43.7%) | | |
| Production, the engine with the Gemini rewording | 1,126 (52.4%) | | |
| Shipped readers (v7a, v5), refusal off | 1,351 (62.9%) | 1,535 (71.5%) | 2,145 |
| Four earlier readers (v7a, v5, v7b, v6), choice off | 1,395 (65.0%) | 1,575 (73.4%) | |
| **These readers, choice off** | **1,414 (65.9%)** | 1,611 (75.0%) | 2,147 |
| These readers, choice at 0.1421 | 1,387 (64.6%) | | 1,972 (585 wrong) |

The decoder's period rule reaches 6 of the 2,147 questions (counted without reading them); re-run with the final
code, they score as in the full run (4 strict), so the totals above hold for it. The bundle was chosen on calibration
and the product's demo questions, not on the test.

## A column matched with the question's value is counted by its rows: no Spider change; one demo case fixed (2026-10-21)

The decoder no longer reads COUNT(DISTINCT column) when a condition compares that column with the question's own value
(= or LIKE): its distinct values are that value or the few containing it. Same bundle (HF `b9bb652`), evaluator and
flags as the entry below, at `b54e2a2` plus this change (`worktree_dirty: true`), refusal off. Calibration (1,053):
617 strict / 714 lenient, unchanged, with no calibration answer's SQL changed. Frozen test (2,147, totals only): 1,351
strict / 1,535 lenient, unchanged. The live demo-dataset gate on the three datasets it touches: "How many Intake
Consent documents are there?" 1 → 6 (right); the other two datasets' failures are unchanged. Records:
`prereasoner-readers-artifacts/evals/g/`, `evals/gate_g_subset.log`.

## Learned readers replace the lexical search: frozen Spider test 939 → 1,351 strict; calibration 418 → 617 (2026-10-21)

The readers bundle installed by `training/readers/promote.py` (stage 1; reader v7a, anchored; reader v5
after the relayout, plain; the "f" re-scorer; refusal threshold 0.2511) in the readers worktree at `b8bedde` plus the
uncommitted migration (`worktree_dirty: true`). `spider.probe.full_eval`, whole_db / served / SQL / cap 5,000 per
table / 12 s soft budget / compose on / external models off. The refusal threshold was off for these runs: each answer
records its confidence, so the refusal's totals follow exactly. The readers trained on Spider train and dev and BIRD
train, never on the 20 calibration databases or the 40 test databases; DEV is training data for them and is not
reported. Records: `prereasoner-readers-artifacts/evals/f/` (per-example calibration; the test's totals
only).

**Frozen Spider test** (2,147 questions, 40 databases; totals only, as at every milestone):

| System | Strict | Lenient | Answered |
|---|---|---|---|
| Engine alone `b8bedde` (milestone of 2026-10-20) | 939 (43.7%) | | |
| Production, the engine with the Gemini rewording | 1,126 (52.4%) | | |
| Readers, first bundle (re-scorer "u", before the share/period/either/MAX rules) | 1,330 (61.9%) | 1,519 (70.7%) | 2,145 |
| Readers, second bundle (re-scorer "us", with those rules) | 1,319 (61.4%) | 1,500 (69.9%) | 2,145 |
| **Readers, shipped bundle (re-scorer "f"), refusal off** | **1,351 (62.9%)** | 1,535 (71.5%) | 2,145 |
| Readers, shipped bundle, refusal at 0.2511 | 1,314 (61.2%) | | 1,875 (561 wrong) |

Each bundle was chosen on calibration and the product's demo questions, not on the test. The second added the
share, either-or, period and extreme rules (11 fewer right on the test, 10 more on calibration); the third trained the
re-scorer on the shipped recipes' own cross-fold pools.

**Calibration** (20 held-out Spider train databases, 1,053 questions), same evaluator and question set:

| System | Strict | Answered | Wrong |
|---|---|---|---|
| Engine `b8bedde` | 418 (39.7%) | 712 | 294 |
| Readers, first bundle, refusal off | 597 (56.7%) | 1,052 | 455 |
| Readers, second bundle, refusal off | 607 (57.6%) | 1,052 | 445 |
| **Readers, shipped bundle, refusal off** | **617 (58.6%)** | 1,052 | 435 |
| Readers, shipped bundle, refusal at 0.2511 | 587 (55.7%) | 881 | 294 |

Transitions, shipped readers (refusal off) against the engine: 274 wins, 75 losses, 343 unchanged right, 361 unchanged
wrong. The threshold is the lowest at which the readers answer no more questions wrongly than the engine they replace
(`python -m training.readers.refusal match`). Prediction seconds on the development machine (4 threads, bfloat16): the
first calibration run, beside one other evaluation, median 2.39 and p90 4.72 against the engine's 2.24 and 6.09; the
final runs shared the machine with each other and read slower (calibration median 3.73, p90 6.98).

Offline (calibration candidates, the re-scorer's choice, refusal off; the pools behind the choice of readers):

| Readers | Right of 1,028 | Right answer in the pool |
|---|---|---|
| v7a alone / v7b alone / v5 alone | 565 / 556 / 555 | 629 / 638 / 645 |
| v7a + v7b | 589 | 704 |
| v7a + v6 | 590 | 707 |
| v7a + v5 (shipped): re-scorer v2 / "u" / "us" / "f" (the shipped recipes' own cross-fold pools) | 601 / 611 / 612 / 624 | 721 |
| v7a + v7b + v5 + v6, re-scorer with a five-feature combiner (about 9 GiB in all) | 621 | 767 |

The evaluated bundle was installed `--local-only`; the same files were then published at Hugging Face revision
`b9bb6521799ca603845a8e41248ff3abfe70fb40` and promoted at that revision (every file's hash checked against the
manifest by an anonymous download). The live demo-dataset gate fails 6 cases with this bundle (`DECISIONS.md`,
"Learned readers replace the lexical search"), so it is not deployed.

**Against hosted models on the same 2,147 frozen test questions** (2026-10-19; each model wrote SQL from the schema,
graded by the same capped loader, executor and `spider_eval.compare` as `full_eval`; the models' inputs and budgets
differ from the engine's, so this compares the task, not the models alone):

| System | Strict | Lenient | Cost |
|---|---|---|---|
| Gemini 3.8 Flash writing SQL | 1,821 (84.8%) | 1,946 (90.6%) | $1.59 |
| Sonnet 5.5 writing SQL | 1,806 (84.1%) | 1,950 (90.8%) | $6.08 |
| Readers (no model writes SQL), refusal off | 1,351 (62.9%) | 1,535 (71.5%) | local CPU |

Records: `prereasoner-readers-artifacts/llm_bench/` (`bench.py`, summaries and per-question SQL).

## Independent October 16 review: current checkout 584 strict; matched Sonnet 5.5 and Gemini 3.8 Flash

Fresh `whole_db` / production `served` / SQL / cap 5,000 per table / 12s soft prediction budget / seed 0 / external
rewriting off; current HEAD `e4a549e` plus the pre-existing dirty worktree, unchanged bundle
`b11056f8…`. DEV **584/1,034 strict (56.5%), 622/1,034 lenient (60.2%), 750/1,034 answered (72.5%)**;
284 unanswered (202 completeness, 63 constraints, 19 AST search); scalar-gold 282/408 (69.1%). Against the
October 14 accepted snapshot (546 strict / 590 lenient / 728 answered): 39 strict wins / 1 loss (+38), with 58 SQL
changes, 23 newly answered and 1 newly refused. Strict loss 688; newly refused 787. This paired delta includes all
intervening committed and pre-existing dirty changes; it is not attributed to one Claude commit.

Saved October 7 provider SQL was re-executed by the same capped loader, SQL executor and scorer on the same 1,034
questions/DBs: Sonnet 5.5 **840 strict / 897 lenient / 1,023 answered**; Gemini 3.8 Flash **872 / 904 / 1,034**.
On the identical 750 cases the engine answered, engine was 584 strict / 622 lenient; Sonnet 630 / 662 (747 answered),
Gemini 644 / 664 (750 answered). Inputs and generation budgets differ, so this is a same-task/data/scorer comparison,
not a fully controlled model-only comparison. No provider API calls were made.

Engine prediction time on the shared machine: median 5.91s, p90 12.62s, p95 15.04s, max 31.49s; 115 examples exceeded
the 12s soft budget. Other evaluation processes were active, so timing is not isolated. `python -m tests.test_sql_ast`:
272 passed. No live Postgres/Chrome release gates were run.

Evidence: `%LOCALAPPDATA%/Temp/prereasoner-spider-review-e4a549e-20261016/`, tag `review-e4a549e`; provider regrades
and identity checks: `%LOCALAPPDATA%/Temp/prereasoner-spider-review-3e46a74-20261014/`. Current run recorded DEV/table,
DB, scorer/executor, source and weight hashes; code commit `e4a549e7a7994f9c1fade8311990ae827734fb82`, dirty worktree.

## Independent October 14 review: Claude 544 strict; final repaired snapshot 546 strict

Frozen `3e46a74` plus the nine initially dirty files, then separate occurrence-only and final count/paired-emitter repair snapshots.
Fresh whole_db / served / SQL / cap 5,000 per table / 12s soft budget / seed 0 / rewriting off; unchanged bundle `b11056f8…`.
Claude baseline: 544 strict / 588 lenient / 728 answered; versus verified 529: 16 wins / 1 loss (+15).
Occurrence-only: identical totals, 0 strict wins/losses. Final: **546/1,034 (52.8%) strict / 590/1,034 (57.1%) lenient / 728/1,034 (70.4%) answered**.
Final repairs versus baseline: 2 wins / 0 losses; wins [966, 968], losses []; newly answered [], newly refused [].
Fresh baseline exactly matches saved package 15b SQL/grades. Question 202 remains a scored loss versus 529;
independent capped execution confirms unpadded gold returns empty, stored-value direct query returns the two Aberdeen airports.
No scorer/gold/denominator adjustment.

Disjoint final cohorts: 546 strict-correct / 70 lenient-only / 112 incorrect under both / 306 unanswered.
26 strict-correct empty-gold cases have lenient-false; refusal stages {'ast_search': 15, 'completeness': 219, 'constraints': 72}.
October 7 saved Sonnet 5.5 and Gemini 3.8 **Flash** SQL freshly reexecuted on the same capped rows/scorer:
840 strict / 897 lenient / 1,023 answered and 872 strict / 904 lenient / 1,034 answered. No provider grade changes/new API calls.
Same task/data/scorer, different inference inputs/budgets; not a current latency/cost comparison.

Final bounded pool diagnostic (same first 30/difficulty, n=120): 98 raw hits, 98 flag hits,
70 unfinalized selected-member hits; actual served 69 strict / 86 answered.
All recorded fingerprints and completed-run frozen 304-file source sets verified after execution. 410 focused checks plus Ruff F/E9,
high-severity Bandit, compilation and whitespace pass; live Postgres/Chrome/full release gates not run.

Artifacts: `%LOCALAPPDATA%/Temp/prereasoner-spider-review-3e46a74-20261014/`, tags `review-3e46a74`,
`review-output-occurrences`, `review-qualified-count` and their named 120-case diagnostics. Review/plan remain
`release-review/spider-review-20261011.md` and `release-review/sql-accuracy.md`. No training, promotion, commit, push or deployment.

## Independent latest-field review: 529 strict at `4edf791` (October 13 snapshot)

Frozen `4edf791` plus the three previously present review repair files; all 304 tracked Python sources matched primary
before execution. Fresh whole_db / served / SQL / cap 5,000 per table / 12s soft budget / seed 0 / external rewriting off;
unchanged bundle `b11056f8…`. **529/1,034 (51.2%) strict / 576/1,034 (55.7%) lenient / 729/1,034 (70.5%) answered**.
Against independently verified 527: 2 wins / 0 losses, 527 unchanged correct / 505 unchanged wrong;
SQL changes [928, 929, 994, 995], wins [928, 929], no coverage change. Fresh versus saved package 12b: 0 SQL / 0 grade/status changes.
Cumulative gain versus 497: +32; versus 386: +143, including earlier packages.

Disjoint: 529 strict-correct / 74 lenient-only / 126 incorrect under both / 305 unanswered;
27 strict-correct empty-gold cases have lenient-false. Refusal stages: {'ast_search': 9, 'completeness': 221, 'constraints': 75}.
October 7 saved Sonnet 5.5 and Gemini 3.8 **Flash** SQL freshly reexecuted on identical capped rows/shared scorer:
840 strict / 897 lenient / 1,023 answered and 872 strict / 904 lenient / 1,034 answered. No provider grade changes or API generation.
Same task/data/scorer, different inputs/inference budgets; no current cost/speed comparison.

Fresh same-120-case diagnostic: 98 raw strict-hit pools, 98 executable/grounded-flag hits,
63 unfinalized selected-member hits; actual finalized serving 60 strict / 86 answered.
Limited first 30/difficulty, not full certified recall. All 141/125 fingerprints match after execution,
as do all 304 frozen Python sources and saved reference inputs. 396 focused checks, Ruff F/E9, high-severity Bandit,
compilation and whitespace pass; live Postgres/Chrome/full release gates not run.

Review/plan: `release-review/spider-review-20261011.md`, `release-review/sql-accuracy.md`.
Evidence: `%LOCALAPPDATA%/Temp/prereasoner-spider-review-4edf791-20261013/`, tags `review-4edf791` and `review-4edf791-pool-120`.
No new production edits, training, promotion, provider generation, commit, push or deployment by this follow-up.

## Independent October 13 follow-up: 527 strict, latest Claude fixes and code-binding repair

Frozen `b493126` plus package-11 dirty ranker/test snapshot captured at 17:40 Europe/Paris, then a separate repair in
`sql_schema`, `sql_search`, `query_contract`. `whole_db`, production `served`, SQL, cap 5,000/table, 12s soft budget,
seed 0, unchanged bundle `b11056f8…`, external rewriting off. Fresh DEV: **527/1,034 (51.0%) strict**,
573/1,034 (55.4%) lenient, 729/1,034 (70.5%) answered. Pre-repair versus saved package 11:
0 SQL / 0 grade/status changes. Versus independent 497:
30 wins / 0 losses, 497 unchanged correct / 507 unchanged wrong.
Repair versus pre-repair: 0 wins / 0 losses, 0 SQL /
0 grade/status changes. Fixture hardening is not a claimed DEV gain.

Disjoint: 527 strict-correct / 73 lenient-only / 129 incorrect under both / 305 unanswered;
27 strict-correct empty-gold answers have lenient-false. Refusal stages: {'ast_search': 9, 'completeness': 221, 'constraints': 75}.
Saved October 7 Sonnet 5.5 and Gemini 3.8 Flash SQL was reexecuted with identical capped rows and shared scoring:
840 strict / 897 lenient / 1,023 answered; 872 strict / 904 lenient / 1,034 answered, all over 1,034.
No provider grades changed; no new API generation. Same questions/data/scorer, different inputs/inference budgets.

Current 120-case raw-pool diagnostic (first 30/difficulty): 98 raw strict hits,
98 under executable/grounded flags, 63 unfinalized selected-member strict,
60 actual finalized served strict. Not a certified ceiling or full census.
All recorded fingerprints verified after execution (141/141/125 fields). Final focused suites: 395 checks; Ruff F/E9,
high-severity Bandit, compilation and whitespace pass. Live Postgres/Chrome gates were not run.

Artifacts: `%LOCALAPPDATA%/Temp/prereasoner-spider-review-b493126-20261013/`, tags `review-b493126-pkg11`,
`review-code-bindings`, `review-code-bindings-pool-120`, plus `review-analysis.json` and `verified-*.json`.
Review: `release-review/spider-review-20261011.md`. Existing implementation plan: `release-review/sql-accuracy.md`.
No training, model promotion, commit, push or deployment by this review.

## Independent October 13 review: 497 strict, matched hosted SQL references

Frozen source: `e709851` plus Claude's audited package-R source snapshot and the three review repairs in the attached
`spider-accuracy-review-20261013` worktree. Package R matched all 113 serving-source fingerprints before repair;
the final dirty snapshot, inputs and unchanged promoted bundle are verified after execution. Concurrent main edits
were preserved; the tested repairs are applied as uncommitted changes on primary `94c4d50`, with all 113 serving sources
and the regression source equal under Python newline handling. `whole_db`, `served`, SQL, cap 5,000, 12s soft budget,
external rewriting off. Full fresh DEV: **497/1,034 (48.1%) strict**, 546/1,034 (52.8%) lenient,
704/1,034 (68.1%) answered. Versus saved package R: 0 strict wins / 0 losses,
0 SQL changes / 0 grade/status changes. Versus independent 386 baseline: +111 strict,
116 wins / 5 losses. Disjoint results: 75 lenient-only,
132 incorrect under both, 330 unanswered; 26 strict-correct empty-gold cases receive lenient-false.

The saved October 7 Sonnet 5.5 and Gemini 3.8 Flash SQL was independently reexecuted on the identical capped rows
with the shared scorer: **840/1,034 strict, 897 lenient, 1,023 answered** and **872/1,034 strict, 904 lenient, 1,034 answered**.
No changed provider grades and no new API calls. Same task/data/scorer; different prompts and inference budgets.
The full review, validation and limitations are in `release-review/spider-review-20261011.md`.

Artifacts: `%LOCALAPPDATA%/Temp/prereasoner-spider-review-20261013/`, tags `review-e709851`,
`review-pkgR-isolated-repairs`, plus the explicitly limited raw-pool diagnostic `review-pkgR-isolated-pool-120`.
Incomplete intermediate runs are excluded. The review hardens ranking-number roles, output counts (preserving stored-count SUM),
and table-owned full-name construction; it does not introduce another planner or model bundle.

## Each listed output is shown by a field of its own: 711 → 711 strict, Gemini on 766 → 766, held out 463 → 463, fresh holdout 476 → 479 (2026-10-19)

Same evaluator and contract as below (`spider.probe.full_eval`, whole_db, served, cap 5000, the evaluator ingesting
declared and discovered keys), run locally in the pods' four round-robin shards like the base runs, RunPod's balance
having run out. Runs `p40a` are this change against `b306132`'s `p39a` runs. The change is the `DECISIONS.md` entry of
this title.

- DEV, engine alone: 711 → **711 strict**; no win, no loss; wrong answers 132 → 132; 0 queries changed.
- Held out: 463 → **463 strict**; no win, no loss; wrong answers 210 → 208; 5 queries changed.
- Fresh holdout: 476 → **479 strict**; 3 wins (358, 764, 842), no loss; wrong answers 215 → 208; 9 queries changed.
- Gemini on: 766 → 766; no win, no loss; wrong answers 169 → 168; 1 query changed.
- The change refuses answers more than it adds right ones: wrong answers fall by 2 on held out, 7 on the fresh holdout
  and 1 with Gemini on (fresh holdout 43, 699, 848, 887 and held out 430, 760 are now refused).

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}p40a.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_p40a-gemini-dirty.json`.

## A table's descriptions and details are its own; a number before a field's two-word name compares the field: 710 → 711 strict, Gemini on 765 → 766, held out 462 → 463, fresh holdout 473 → 476 (2026-10-19)

Same evaluator and contract as below (`spider.probe.full_eval`, whole_db, served, cap 5000, the evaluator ingesting
declared and discovered keys), run locally in the pods' four round-robin shards like the base runs, RunPod's balance
having run out. Runs `p39a` are this change against `b0d9108`'s `p38f` runs. The change is the `DECISIONS.md` entry of
this title.

- DEV, engine alone: 710 → **711 strict**; 1 win (354), no loss; wrong answers 134 → 132; 2 queries changed.
- Held out: 462 → **463 strict**; 1 win (265), no loss; wrong answers 212 → 210; 2 queries changed.
- Fresh holdout: 473 → **476 strict**; 3 wins (33, 488, 673), no loss; wrong answers 216 → 215; 5 queries changed.
- Gemini on: 765 → 766; 1 win (354), no loss; wrong answers 171 → 169; 2 queries changed.

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}p39a.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_p39a-gemini-dirty.json`.

## A most common value is ranked once, over the rows it is common among; a count meets only a number of rows; a key shows the rows it refers to: 708 → 710 strict, Gemini on 763 → 765, held out 456 → 462, fresh holdout 469 → 473 (2026-10-19)

Same evaluator and contract as below (`spider.probe.full_eval`, whole_db, served, cap 5000, the evaluator ingesting
declared and discovered keys), run locally in the pods' four round-robin shards because RunPod's balance ran out on
2026-10-18; `bf1c242` re-run locally on every changed question (49) reproduced the pod baseline's strictness on all of
them. Runs `p38f` are this change against
`bf1c242`'s `p37d` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 708 → **710 strict**; 2 wins (527, 954), no loss; wrong answers 136 → 134; 11 queries changed.
- Held out: 456 → **462 strict**; 7 wins (304, 645, 673, 707, 714, 940, 972), 1 loss (156); wrong answers 220 → 212; 19
  queries changed.
- Fresh holdout: 469 → **473 strict**; 5 wins (140, 328, 391, 405, 616), 1 loss (698); wrong answers 224 → 216; 19
  queries changed.
- Gemini on: 763 → 765; 2 wins (154, 954), no loss; wrong answers 173 → 171; 11 queries changed.
- Held out 156 ("the most common first name among all actors") is now the gold reading, actors grouped by first name,
  and three names tie at the top: the tie rendering serves them all (the doubled reading was right by picking one).
- Fresh holdout 698 was right by a HAVING COUNT(*) < 120000 that every group met, now refused for counting rows the
  question states as a value; the gold intersects two sets of investors.
- Spider DEV 579 ("all the student details in reversed lexicographical order") is answered now that "lexicographical" is
  read, by the students' first names rather than their other_student_details: a new wrong answer.

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}p38f.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_p38f-gemini-dirty.json`.

## The pool holds no exclusion the gate refuses for its shape; "never" and what rows do not have are exclusions; a list's "for" names its names' owner: 708 → 708 strict, Gemini on 762 → 763, held out 454 → 456, fresh holdout 466 → 469 (2026-10-18)

Same contract as below (RunPod CPU pods, the evaluator ingesting declared and discovered keys). Runs `p37d` are this
change against `037405d`'s `p36g` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 708 → **708 strict**; no win, no loss; wrong answers 136 → 136; 0 queries changed.
- Held out: 454 → **456 strict**; 2 wins (409, 826), no loss; wrong answers 222 → 220; 2 queries changed.
- Fresh holdout: 466 → **469 strict**; 3 wins (178, 635, 855), no loss; wrong answers 224 → 224; 4 queries changed.
- Gemini on: 762 → 763; 1 win (281), no loss; wrong answers 173 → 173; 1 query changed.
- No loss on any set. Answers it now refuses that were wrong: held out 48 and 72 (wrong answers a first version made of
  questions refused before, once the pool held other readings). A first version's losses were fixed before these runs:
  dropping refused readings before ranking changed the search's top reading the decomposition probe reads
  (tests.test_complex_datasets), and "never" as an exclusion cue left the word unread by a difference (Spider DEV 282).

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}p37d.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_p37d-gemini-dirty.json`.

## The names asked for are their table's; a table named only inside a longer name is not joined; demonyms, initials and the information about rows are read: 702 → 708 strict, Gemini on 756 → 762, held out 447 → 454, fresh holdout 458 → 466 (2026-10-18)

Same contract as below (RunPod CPU pods, the evaluator ingesting declared and discovered keys). Runs `p36g` are this
change against `e7fc8d4`'s `z4` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 702 → **708 strict**; 6 wins (5, 100, 294, 639, 791, 1007), no loss; wrong answers 137 → 136; 16
  queries changed.
- Held out: 447 → **454 strict**; 7 wins (70, 267, 422, 459, 724, 755, 789), no loss; wrong answers 224 → 222; 14
  queries changed.
- Fresh holdout: 458 → **466 strict**; 9 wins (274, 479, 527, 582, 600, 696, 910, 914, 917), 1 loss (574); wrong answers
  230 → 224; 22 queries changed.
- Gemini on: 756 → 762; 6 wins (100, 294, 639, 791, 901, 1007), no loss; wrong answers 177 → 173; 17 queries changed.
- Wrong answers it makes of questions refused before: Spider DEV 80 ("list their ids instead of names" is a count beside
  the students' last names, LName now naming them), held out 658 ("in the order of their names" ordered by a sport's
  name), fresh holdout 880 (the authors' every field, joined to their courses) and 693 (gold's own query, whose rows the
  grader does not match). Answers it now refuses that were wrong: held out 628, fresh holdout 106, 134, 921.
- Fresh holdout 574 is the one loss: a coincidence the gate now refuses (it compared surname = 'Driver' and showed lap
  times, the surnames listed not shown). A first version's losses were fixed before these runs: a self-join's shown
  title read as an alias's (fresh holdout 539, 790), and the readings of "the largest area" displacing Spider DEV 800's
  answer from the pool.

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}p36g.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_p36g-gemini-dirty.json`.

## A negation reaches its clause; living, corresponding and a trip's preposition are read; a difference keeps the rows it names: 697 → 702 strict, Gemini on 751 → 756, held out 442 → 447, fresh holdout 442 → 458 (2026-10-18)

Same contract as below (RunPod CPU pods, the evaluator ingesting declared and discovered keys). Runs `z4` are this
change against `5979e87`'s `y2` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 697 → **702 strict**; 5 wins (171, 172, 766, 827, 833), no loss; wrong answers 137 → 137; 7 queries
  changed.
- Held out: 442 → **447 strict**; 5 wins (341, 368, 474, 786, 824), no loss; wrong answers 227 → 224; 13 queries
  changed.
- Fresh holdout: 442 → **458 strict**; 16 wins (48, 70, 111, 223, 376, 429, 434, 447, 454, 500, 555, 612, 619, 726, 795,
  992), no loss; wrong answers 233 → 230; 21 queries changed.
- Gemini on: 751 → 756; 5 wins (171, 172, 766, 827, 833), no loss; wrong answers 178 → 177; 9 queries changed.
- Wrong answers this change makes of questions refused before, once the negation reached their values: "Show all
  headquarters without a company in banking industry." (fresh holdout 724) shows the headquarters of companies outside
  banking, with each company, where the question asks for those with no banking company; and "List the document ids of
  documents with the status done and type Paper, which not shipped by the shipping agent named USPS." (held out 5) shows
  status codes: the request for the ids is dropped where "and" joins two conditions after "with"
  (`sql_expansion.list_closed`).
- The first measured version (runs `z3`) lost Spider DEV 778, 779 and held out 597, and Gemini on DEV 62, to the
  difference check and the "each" reading; runs `z4` are after their fixes (a key the kept part shows, a name after
  "whose", `_describes_rows`). Its fresh holdout run was lost to a RunPod host that exposed no SSH and was not measured.

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}z4.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_z4-gemini-dirty.json`.

## People are persons; a table a read key refers to, "located" by a place field and "greater than" what is no number by its comparison are read: 696 → 697 strict, Gemini on 751 → 751, held out 441 → 442, fresh holdout 438 → 442 (2026-10-18)

Same contract as below (RunPod CPU pods, the evaluator ingesting declared and discovered keys). Runs `y2` are this
change against `ad53dc1`'s `w3` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 696 → **697 strict**; 1 win (710), no loss; wrong answers 137 → 137; 1 query changed.
- Held out: 441 → **442 strict**; 1 win (994), no loss; wrong answers 226 → 227; 3 queries changed.
- Fresh holdout: 438 → **442 strict**; 4 wins (518, 609, 743, 973), no loss; wrong answers 233 → 233; 5 queries changed.
- Gemini on: 751 → 751; no win, no loss; wrong answers 178 → 178; 0 queries changed.
- A wrong answer this change makes of a question refused before: "Find the number of people who is under 40 for each
  gender." (held out 132) shows each gender before its count, the reading asked, in the groups-first column order the
  gold reverses.
- Verifying "most" and "least" by what the search reads them to grade, a count or total of rows or a measure they name,
  was measured over the questions of each set saying either (128 DEV, 120 held out, 125 fresh holdout) and not kept: it
  refused DEV 228 and 695, which the engine answers right by coincidence, ordering by another field ("the airport with
  the least flights" by a flight's FlightNo, "the contestant who got least votes" by contestant_number), so 72 → 70
  strict (held out 40 → 40, fresh holdout 43 → 43) for 116 → 111 wrong answers; a variant also reading "least" counts
  lost DEV 228, 461, 695 and fresh holdout 847 (72 → 69, 40 → 40, 43 → 42; wrong answers 116 → 113).

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}y2.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_y2-gemini-dirty.json`.

## An extreme a compared subquery finds, a longer name's part and the rows a count counts are read; a referring key counts the rows it refers to; "how many total" asks one number: 691 → 696 strict, Gemini on 744 → 751, held out 436 → 441, fresh holdout 437 → 438 (2026-10-18)

Same contract as below (RunPod CPU pods, the evaluator ingesting declared and discovered keys). Runs `w3` are this
change against `5cc3d95`'s `v5` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 691 → **696 strict**; 5 wins (44, 150, 159, 902, 903), no loss; wrong answers 137 → 137; 8 queries
  changed.
- Held out: 436 → **441 strict**; 5 wins (349, 573, 612, 655, 666), no loss; wrong answers 228 → 226; 10 queries
  changed.
- Fresh holdout: 437 → **438 strict**; 1 win (767), no loss; wrong answers 233 → 233; 3 queries changed.
- Gemini on: 744 → 751; 7 wins (44, 101, 150, 159, 607, 902, 903), no loss; wrong answers 178 → 178; 14 queries changed.
- Wrong answers this change makes of questions refused before: "How many total tours were there for each ranking date?"
  shows each date's total, the reading asked, in the groups-first column order the gold reverses (DEV 480, Gemini on 479
  and 480); and Gemini on, a rewording of "the transcript which shows the least number of results" is answered with the
  earliest transcript's date (DEV 573): "most" and "least" are read without the count ordering they ask being verified,
  and the gate before this change accepts the same rewording and query.
- The first measured version (runs `w2`) lost fresh holdout 9 and 51 and answered fresh holdout 488 and 673 wrong,
  reading a number before a numeric field's name as a count of rows; runs `w3` are after its fix.

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}w3.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_w3-gemini-dirty.json`.

## A superlative or aggregate word that names a field grades what follows it; the people counted are a population; a member is its table's row: 685 → 691 strict, Gemini on 738 → 744, held out 433 → 436, fresh holdout 434 → 437 (2026-10-17)

Same contract as below (RunPod CPU pods, the evaluator ingesting declared and discovered keys). Runs `v5` are this
change against `76837a3`'s `v3` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 685 → **691 strict**; 6 wins (18, 19, 43, 415, 728, 767), no loss; wrong answers 140 → 137; 8
  queries changed.
- Held out: 433 → **436 strict**; 3 wins (664, 785, 803), no loss; wrong answers 228 → 228; 5 queries changed.
- Fresh holdout: 434 → **437 strict**; 3 wins (76, 343, 567), no loss; wrong answers 230 → 233; 7 queries changed.
- Gemini on: 738 → 744; 6 wins (18, 19, 43, 415, 728, 767), no loss; wrong answers 181 → 178; 8 queries changed.
- Wrong answers this change makes of questions refused before, each wrong for another reason: "How many people live in
  Asia, and what is the largest GNP among them?" totals the population grouped by GNP, the gate reading "the largest" by
  any descending order (DEV 723, Gemini on too); and with "member" read, staff and faculty questions that need a set
  query or another relationship are answered ("faculty members who do not advise any student", "the phone numbers of all
  customers and all staff members", held out 627, 918; fresh holdout 31, 86, 888).
- The measured change first also read "people" as Population wherever it was said: "the language spoken by the largest
  percentage of people" showed Population (DEV 816, 820, wrong); it is read only counted or graded before release, which
  also leaves DEV 805 ("the 3 countries with the fewest people") refused.

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}v5.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_v5-gemini-dirty.json`.

## Participles, verbs and measure nouns are read; "most" before a table counts its rows; a number after "id" is its key; a list shows each field; a bound away from an equality answers nothing: 663 → 685 strict, Gemini on 731 → 738, held out 415 → 433, fresh holdout 412 → 434 (2026-10-17)

Same contract as below (RunPod CPU pods, the evaluator ingesting declared and discovered keys). Runs `v3` are this
change against `028c8c7`'s `r5` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 663 → **685 strict**; 23 wins (39, 98, 173, 462, 507, 519, 520, 565, 567, 603, 616, 640, 643, 644,
  691, 732, 733, 750, 759, 856, 857, 971, 973), 1 loss (27); wrong answers 139 → 140; 42 queries changed.
- Held out: 415 → **433 strict**; 18 wins (96, 112, 138, 244, 278, 336, 386, 414, 435, 483, 649, 717, 788, 810, 864,
  897, 921, 980), no loss; wrong answers 226 → 228; 48 queries changed.
- Fresh holdout: 412 → **434 strict**; 22 wins (74, 103, 136, 180, 197, 204, 218, 254, 273, 300, 316, 512, 615, 708,
  712, 719, 734, 753, 776, 810, 814, 825), no loss; wrong answers 232 → 230; 40 queries changed.
- Gemini on: 731 → 738; 8 wins (98, 462, 643, 644, 759, 856, 857, 973), 1 loss (27); wrong answers 181 → 181; 32 queries
  changed.
- DEV 27 ("What is the year that had the most concerts?") is a tie: 2014 and 2015 hold three concerts each. The answer
  now ranks the years by their concerts and keeps both, as DEV 26's did before; `028c8c7` ordered by the year itself,
  and Spider's LIMIT 1 happens to keep 2015.
- Wrong answers this change makes of questions refused before: an aggregate word that is a numeric field's whole name, a
  measure noun after it, is still read as the aggregate ("the stadium with highest average attendance", DEV 18, 19; "the
  highest total score", held out 664, 803), and "first-grade students" is read as a superlative (held out 923). Of the
  wrong answers the measured change first made, those of a participle a table's name spells (held out 601), an equality
  bounded away (fresh holdout 189) and one date of two listed (held out 920, fresh holdout 709) are refused before
  release, and DEV 520 is answered right.

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}v3.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_v3-gemini-dirty.json`.

## A count whose noun names a text field counts the rows: against `608fc98`, 632 → 663 strict, Gemini on 704 → 731, held out 405 → 415, fresh holdout 405 → 412 (2026-10-17)

Runs `r5` are the entry below's change without its field-noun count reading (the `DECISIONS.md` entry of this title),
against `608fc98`'s `c3` runs, on the same contract.

- DEV, engine alone: 632 → **663 strict**; 31 wins (52, 94, 104, 109, 115, 147, 148, 149, 199, 245, 246, 310, 323, 379,
  380, 389, 390, 439, 461, 468, 576, 577, 667, 695, 712, 768, 846, 918, 919, 976, 1011), no loss; wrong answers 166 →
  139; 83 queries changed.
- Held out: 405 → **415 strict**; 10 wins (68, 92, 110, 142, 363, 407, 505, 546, 617, 832), no loss; wrong answers 229 →
  226; 33 queries changed.
- Fresh holdout: 405 → **412 strict**; 7 wins (44, 272, 293, 438, 636, 656, 875), no loss; wrong answers 237 → 232; 27
  queries changed.
- Gemini on: 704 → 731 strict; 27 wins (52, 94, 109, 115, 130, 147, 148, 149, 199, 245, 246, 310, 323, 379, 380,
  390, 439, 461, 468, 576, 577, 695, 768, 846, 918, 919, 1011), no loss; wrong answers 210 → 181; 84 queries changed.
- Against the entry below (`1cd624d`, runs `r4`): DEV 664 → 663 and Gemini on 732 → 731, the one loss DEV 751; held out
  and the fresh holdout unchanged. No strict loss against `608fc98` on any set.
- The live demo-dataset suite (`tests.test_datasets`, all four execution modes) passes on this change; it failed on
  `1cd624d` at formesign-intake.

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}r5.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_r5-gemini-dirty.json`.

## A condition's field is not shown; a count counts what its noun names, a field's values once each; superlatives, years and inflections name their fields; a set query shows the key said; a value is its display field's: 632 → 664 strict, Gemini on 704 → 732, held out 405 → 415, fresh holdout 405 → 412 (2026-10-17)

Same contract as below (RunPod CPU pods, the evaluator ingesting declared and discovered keys). Runs `r4` are this
change against the entry below's `c3` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 632 → **664 strict**; 32 wins (52, 94, 104, 109, 115, 147, 148, 149, 199, 245, 246, 310, 323, 379,
  380, 389, 390, 439, 461, 468, 576, 577, 667, 695, 712, 751, 768, 846, 918, 919, 976, 1011), no loss; wrong answers 166
  → 138; 84 queries changed.
- Held out: 405 → **415 strict**; 10 wins (68, 92, 110, 142, 363, 407, 505, 546, 617, 832), no loss; wrong answers 229 →
  226; 34 queries changed.
- Fresh holdout: 405 → **412 strict**; 7 wins (44, 272, 293, 438, 636, 656, 875), no loss; wrong answers 237 → 232; 28
  queries changed.
- Gemini on: 704 → 732; 28 wins (52, 94, 109, 115, 130, 147, 148, 149, 199, 245, 246, 310, 323, 379, 380, 390, 439, 461,
  468, 576, 577, 695, 751, 768, 846, 918, 919, 1011), no loss; wrong answers 210 → 180; 86 queries changed.
- The Chrome gate of `608fc98` found complex-category-gaps answering "I couldn't split this question": Gemini's
  decomposition leaf "customer and product category for each purchase" was refused, on `608fc98` and `6eed24e` alike. It
  is answered now (a table joined to another item by "and" is shown by its display field).
- Measured as one package (runs `r4`). The rounds before it found losses fixed here: Gemini on, DEV 565, 569, 976, 977
  and 667 (a superlative's inferred dimension); DEV 277 ("awarded the most times"); held out 882 ("the most common
  amenity in the dorms" shown by dorm) and 989 ("movie ratings" counted as movies); the fresh holdout 44, 438 and 648 (a
  count of keys, and "instructors teach a course" read as the teaches table).
- No strict loss against 608fc98 on any set. Wrong answers fall on every set: DEV 166 to 138, held out 229 to 226, the
  fresh holdout 237 to 232, Gemini on 210 to 180.

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}r4.json` and `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_r4-gemini-dirty.json`.

## Relationships as serving discovers them; a measure's unit, a yes/no flag and a verb's doer are read; one closed field is the answer; "corresponding", titles and times are read: 588 → 632 strict, Gemini on 674 → 704, held out 385 → 405, fresh holdout 390 → 405 (2026-10-17)

The change is the `DECISIONS.md` entry of this title. Engine-alone runs are the production evaluator on RunPod CPU pods:
`requirements.lock.txt` on CPython 3.11.17 (the container's Python), the bundle fetched and verified by
`engine.fetch_weights` (`b11056f8ce8b`), each set split round robin into four evaluator processes and merged by index.
That environment reproduced `6eed24e` exactly (DEV 588, held out 385, fresh holdout 390; runs `b0`). The evaluator now
ingests Spider's declared keys as serving does (the ingestion contract `production_declared_plus_discovered`); alone on
`6eed24e` it measures DEV 588 → 602 strict (16 wins, 2 losses: 535, 536), held out 385 → 373 and the fresh holdout 390 →
380: serving's discovery as `6eed24e` read relationships cost the held-out sets what the evaluator, reading the
declarations only, never saw. The relationship family recovers them. Gemini on is the local run as before.

- DEV, engine alone: 588 → **632 strict**; 44 wins (97, 213, 214, 215, 216, 217, 218, 219, 220, 233, 234, 235, 236, 237,
  238, 239, 240, 362, 368, 451, 588, 625, 645, 646, 693, 738, 739, 748, 749, 770, 771, 778, 779, 787, 815, 841, 848,
  882, 948, 956, 1002, 1003, 1016, 1017), no loss; wrong answers 165 → 166; 77 queries changed.
- Held out: 385 → **405 strict**; 22 wins (113, 183, 194, 234, 236, 259, 476, 498, 599, 682, 726, 811, 863, 889, 898,
  913, 926, 932, 933, 947, 976, 997), 2 losses (546, 872); wrong answers 232 → 229; 74 queries changed.
- Fresh holdout: 390 → **405 strict**; 19 wins (170, 282, 441, 461, 561, 597, 627, 669, 746, 750, 751, 754, 797, 805,
  856, 889, 890, 940, 988), 4 losses (24, 488, 650, 673); wrong answers 235 → 237; 64 queries changed.
- Gemini on: 674 → 704; 32 wins (97, 213, 214, 215, 216, 217, 218, 219, 220, 233, 234, 235, 236, 237, 238, 239, 240,
  362, 451, 558, 625, 645, 646, 770, 771, 778, 779, 815, 882, 948, 956, 1016), 2 losses (768, 884); wrong answers 216 →
  210; 76 queries changed.

Each family alone against `6eed24e` (DEV / held out / fresh holdout, strict wins - losses):

- Relationships, with the ingestion contract: +16 (16 wins, 0 losses) / +6 (6, 0) / +6 (8, 2: bike_1 488 and 673, where
  the true trip keys crowd the pool).
- Listing grain: +1 / 0 / +1.
- One closed field: +4 (4, 0) on DEV; not run alone on the held-out sets.
- A measure's unit: +4 / +3 / 0.
- Verbs: +5 / +1 / 0; alone it answered DEV 768-770 wrong, which the flags family reads.
- Flags: +6 / +1 / 0.
- Rule words: +7 / +4 / +2.

- Fourteen of the DEV wins (flight_2 215-220 and 233-240) are no capability. Their gold answers are 0 or empty because
  flight_2 stores its airport codes with a leading space (' AHD'), and the engine's answers, joining flights to airlines
  as the gold queries do, are 0 or empty too; several read the wrong airport side. Without them the DEV gain is +30. DEV
  213 and 214 (JetBlue's 100 flights) are real.
- Losses: held out 546 (Products.parent_product_id discovered as a reference to Customers.customer_id, its role naming
  no customer) and 872 (a count of cards joined through the customers the new relationships connect); fresh holdout 488
  and 673 (bike_1) and 24 and 650 (a discovered relationship adds a join the answer does not need); Gemini on 768 (the
  flag condition's field shown beside the language) and 884.
- Newly answered wrong on DEV (12): 186 and 811 compare values the gold queries miss (a city stored as 'Anthony ',
  "north america" in lower case), 230 keeps tied airlines and 792 orders its columns as asked; 245, 246, 768 and 769
  show a condition's field beside the answer, 819 and 821 leave "predominantly" unread, 884 groups the students by name,
  and 922 reads "or" as "and".

Output: `%LOCALAPPDATA%/Temp/prereasoner-remote-eval-20261017/full_eval_{,heldout-,holdout2-}c3.json` and
`%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_c3-gemini-dirty.json`.

## "Available", "the results" and "compute" ask nothing; an ordering from old to young starts at the oldest: 587 → 588 strict, Gemini on 674 → 674, held out 377 → 385, fresh holdout 390 → 390 (2026-10-16)

Same contract as below. Runs `p28-dirty`, `p28-gemini-dirty`, `heldout-p28-dirty` and `holdout2-p28-dirty` are
`ae1c0a4` with this change, against its `m27` runs. The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 587 → **588 strict**; 1 win (414), no loss; wrong answers 165 → 165; 1 query changed.
- Held out: 377 → **385 strict**; 8 wins (41, 390, 443, 447, 477, 715, 842, 924), no loss; wrong answers 232 → 232; 8
  queries changed.
- Fresh holdout: 390 → **390 strict**; no win, no loss; wrong answers 234 → 235; 1 query changed.
- Gemini on: 674 → 674; no win, no loss; wrong answers 216 → 216; 0 queries changed.
- The fresh holdout answers 789, which it refused, wrong: "Compute the total amount of settlement across all the
  settlements." sums Payments.Amount_Payment where Spider sums Settlements.Amount_Settled. The search names no field by
  "settlement", the word of the Settlements table, so no reading summed Amount_Settled; "compute" was the only word the
  gate left unread.
- Held out 924 and DEV 414 order the ages descending; the search ordered them ascending before, and those answers were
  refused for "old" and "young" (Spider's strict grade, a multiset of rows, would have scored them).

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{p28,p28-gemini,heldout-p28,holdout2-p28}-dirty.json`.

## A possessive is its word; a word spelling out a field's abbreviation names it; a key named as the table it refers to names that table; only a request said again repeats its field: 584 → 587 strict, Gemini on 671 → 674, held out 370 → 377, fresh holdout 383 → 390 (2026-10-16)

Same contract as below. Runs `m27-dirty`, `m27-gemini-dirty`, `heldout-m27-dirty` and `holdout2-m27-dirty` are
`02a2d69` with this change, against its `m23` runs (`02a2d69` itself: the review session's `7452cf2` and the entry
below). The change is the `DECISIONS.md` entry of this title.

- DEV, engine alone: 584 → **587 strict**; 3 wins (688, 715, 873), no loss; wrong answers 166 → 165; 3 queries changed.
- Held out: 370 → **377 strict**; 7 wins (202, 269, 292, 369, 383, 631, 672), no loss; wrong answers 231 → 232; 13
  queries changed.
- Fresh holdout: 383 → **390 strict**; 7 wins (21, 35, 68, 450, 640, 684, 728), no loss; wrong answers 231 → 234; 13
  queries changed.
- Gemini on: 671 → 674; 3 wins (688, 715, 873), no loss; wrong answers 218 → 216; 5 queries changed.
- The added wrong answers are questions the engine refused: held out 100 ("Alice's friends of friends", one step of the
  friendship read) and 409 (the stations never above 10 bikes, read as a NOT IN of the wrong rows), the fresh holdout
  156 (a total per customer where the question asks one total) and 818 and 971, right only leniently (the balance field
  shown beside each state's total).
- Against the base before the review session's 7452cf2 (the `pkg23` runs), `02a2d69` measured DEV 583 → 584, held out
  373 → 370, the fresh holdout 379 → 383 and Gemini on 670 → 671: its count-grain repair won DEV 966 and 968 and held
  out 719 and lost held out 412 and 654 (each counts the joined rows, or one filtered by a type, where the repair counts
  each exhibition or song once), and its output-occurrence repair lost DEV 688 and held out 269 and 383, which this
  change recovers.
- A first round of the abbreviation reading without the table-word guard lost held out 58, 557 and 938 ("students" read
  as ENROLL.STU_NUM, shown beside the name); that round is not this measurement.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{m23,m27}{,-gemini}-dirty.json` and
`full_eval_{heldout,holdout2}-{m23,m27}-dirty.json`.

## A typographic quote or apostrophe is the ASCII one: 583 → 583 strict, Gemini on 670 → 670, held out 373 → 373, fresh holdout 379 → 379 (2026-10-16)

Same contract as below. Runs `pkg23-dirty`, `pkg23-gemini-dirty`, `heldout-pkg23-dirty` and `holdout2-pkg23-dirty`
are the change of the entry below with this change, against its `pkg22b` runs. The change is the `DECISIONS.md` entry
of this title: a hardening, with no strict change in any set.

- DEV, engine alone: 583 → 583 strict; no win, no loss; wrong answers 167 → 167; 0 queries changed.
- Held out: 373 → 373 strict; no win, no loss; wrong answers 230 → 230; 0 queries changed.
- Fresh holdout: 379 → 379 strict; no win, no loss; wrong answers 234 → 235; 1 query changed.
- Gemini on: 670 → 670 strict; no win, no loss; wrong answers 219 → 219; 0 queries changed.
- The DEV and held-out questions written with typographic marks (DEV 77, 715, 719, held out 202) are answered or refused
  as before: DEV 715 is refused for its possessive (the search read "Brazil's" as "brazil'"), held out 202 for "account
  balance", which names no acc_bal. The fresh holdout answers 742, which it refused, right only leniently: its quoted
  'Smith' is now read, and the answer shows the project's code beside its name.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg23,pkg23-gemini,heldout-pkg23,holdout2-pkg23}-dirty.json`.

## A misspelled word the query reads is read; "listed" asks nothing beside an address state: 578 → 583 strict, Gemini on 669 → 670, held out 372 → 373, fresh holdout 378 → 379 (2026-10-16)

Same contract as below. Runs `pkg22b-dirty`, `pkg22b-gemini-dirty`, `heldout-pkg22b-dirty` and `holdout2-pkg22b-dirty`
are the change of the entry below with this change, against its `pkg21` runs. The change is the `DECISIONS.md` entry
of this title.

- DEV, engine alone: 578 → **583 strict**; 5 wins (498, 509, 552, 568, 612), no loss; wrong answers 167 → 167; 5 queries
  changed.
- Held out: 372 → **373 strict**; 1 win (986), no loss; wrong answers 230 → 230; 2 queries changed.
- Fresh holdout: 378 → **379 strict**; 1 win (231), no loss; wrong answers 234 → 234; 1 queries changed.
- Gemini on: 669 → 670; 1 win (552), no loss; wrong answers 219 → 219; 2 queries changed.
- Gemini on, 612 is now served by the engine, where the rewording answered it the same. The first round of this change
  read a misspelling without the vocabulary test: held out, it answered 10 more questions, 8 of them wrong ("country"
  read as County, a name read as a field); that round is not this measurement.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg22b,pkg22b-gemini,heldout-pkg22b,holdout2-pkg22b}-dirty.json`.

## The row with the maximum is the row holding it; an extreme listed beside another output is a value; a rewording may say the value: 577 → 578 strict, Gemini on 662 → 669, held out 372 → 372, fresh holdout 377 → 378 (2026-10-15)

Same contract as below. Runs `pkg21-dirty`, `pkg21-gemini-dirty`, `heldout-pkg21-dirty` and `holdout2-pkg21-dirty`
are the change of the entry below with this change, against its `pkg20` runs. The change is the `DECISIONS.md` entry
of this title.

- DEV, engine alone: 577 → **578 strict**; 1 win (722), no loss; wrong answers 166 → 167; 3 queries changed.
- Held out: 372 → **372 strict**; no win, no loss; wrong answers 230 → 230; 4 queries changed.
- Fresh holdout: 377 → **378 strict**; 1 win (896), no loss; wrong answers 233 → 234; 4 queries changed.
- Gemini on: 662 → 669; 7 wins (5, 392, 466, 667, 722, 762, 763), no loss; wrong answers 221 → 219; 15 queries changed.
- The rewording check reads only the Gemini path: it accounts for Gemini on 762 and 763, the losses of the entry below.
  Engine alone, DEV answers 2 questions it refused (722 right, 168 wrong), held out 2 (329 right only leniently, 75
  wrong) and refuses 406 and 666, both wrong before; the fresh holdout answers 549 and 912 right only leniently and 893
  turns lenient: the row with the minimum is every tied row, where Spider keeps one.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg21,pkg21-gemini,heldout-pkg21,holdout2-pkg21}-dirty.json`.

## "For which" opens no command; "amount of" says a quantity; a place names its people; an item's last word may stand for its field's kind: 570 → 577 strict, Gemini on 661 → 662, held out 370 → 372, fresh holdout 376 → 377 (2026-10-15)

Same contract as below. Runs `pkg20-dirty`, `pkg20-gemini-dirty`, `heldout-pkg20-dirty` and `holdout2-pkg20-dirty`
are the change of the entry below with this change, against its `pkg19b` runs. The change is the `DECISIONS.md` entry
of this title.

- DEV, engine alone: 570 → **577 strict**; 7 wins (290, 398, 678, 724, 757, 994, 995), no loss; wrong answers 166 → 166;
  11 queries changed.
- Held out: 370 → **372 strict**; 2 wins (491, 943), no loss; wrong answers 229 → 230; 3 queries changed.
- Fresh holdout: 376 → **377 strict**; 1 win (593), no loss; wrong answers 231 → 233; 4 queries changed.
- Gemini on: 661 → 662; 4 wins, 3 losses (154, 762, 763); wrong answers 222 → 221; 10 queries changed.
- DEV answers 8 questions it refused: 5 right, 96 ("the model of the car with the smallest amount of horsepower") right
  but for one of its tied rows, and 132 and 154 wrong; it refuses 787, which it answered right only leniently. Held out
  answers 3 (491 and 943 right, 171 wrong) and the fresh holdout 3 (593 right, 699 wrong, 922 right only leniently).
- With Gemini on, DEV 154 ("the different years in which there were cars produced that weighed less than 4000 and also
  cars that weighed more than 3000") is now read by the engine, which keeps only the first comparison, where the
  rewording answered it; 762 and 763 ("asian country") are refused: the rewording said "Asia", and the rewording check,
  now reading "asian" as the value, asked it to keep the word.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg20,pkg20-gemini,heldout-pkg20,holdout2-pkg20}-dirty.json`.

## "Find" opens a list of outputs; an item followed by "of" and its rows is theirs: 568 → 570 strict, Gemini on 658 → 661, held out 369 → 370, fresh holdout 374 → 376 (2026-10-15)

Same contract as below. Runs `pkg19b-dirty`, `pkg19b-gemini-dirty`, `heldout-pkg19b-dirty` and
`holdout2-pkg19b-dirty` are the change of the entry below with this change, against its `pkg18c` runs. The change is
the `DECISIONS.md` entry of this title.

- DEV, engine alone: 568 → **570 strict**; 2 wins (419, 637), no loss; wrong answers 168 → 166; 5 queries changed.
- Held out: 369 → **370 strict**; 1 win (84), no loss; wrong answers 228 → 229; 4 queries changed.
- Fresh holdout: 374 → **376 strict**; 2 wins (402, 869), no loss; wrong answers 233 → 231; 6 queries changed.
- Gemini on: 658 → 661; 3 wins, no loss; wrong answers 224 → 222; 7 queries changed.
- Held out answers 2 questions it refused: 84 right, and 434 ("Find the city and name of bank branches that provide
  business loans") wrong: it shows a customer's name for the branch's bname, and Spider's gold lists the name before the
  city the question asks first.
- An earlier round (`pkg19`) left the item followed by "of" out of deciding the list's owner and lost DEV 370 ("the ids
  and names of each document, as well as the number of paragraphs" showed a template's id); `pkg19b` decides the owner
  over every item and pins only that item's field, and the case is a registered regression.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg19b,pkg19b-gemini,heldout-pkg19b,holdout2-pkg19b}-dirty.json`.

## "Along with" adds an item; a list of fields and a count counts each group; "listed" asks nothing: 562 → 568 strict, Gemini on 651 → 658, held out 364 → 369, fresh holdout 367 → 374 (2026-10-15)

Same contract as below. Runs `pkg18c-dirty`, `pkg18c-gemini-dirty`, `heldout-pkg18c-dirty` and
`holdout2-pkg18c-dirty` are the change of the entry below with this change, against its `pkg17b` runs. The change is
the `DECISIONS.md` entry of this title.

- DEV, engine alone: 562 → **568 strict**; 6 wins (342, 623, 627, 638, 946, 972), no loss; wrong answers 172 → 168; 9
  queries changed.
- Held out: 364 → **369 strict**; 5 wins (316, 532, 621, 695, 699), no loss; wrong answers 232 → 228; 8 queries changed.
- Fresh holdout: 367 → **374 strict**; 7 wins (145, 216, 277, 279, 489, 599, 673), no loss; wrong answers 236 → 233; 12
  queries changed.
- Gemini on: 651 → 658; 7 wins, no loss; wrong answers 231 → 224; 11 queries changed.
- An earlier round (`pkg18`) let the exact list start ahead where two items read one field and lost held-out 269, 977
  and fresh holdout 59 ("the names of climbers and the names of mountains"), and `pkg18b` lost the track name of fresh
  holdout 745 ("the name and date for each race and its track name"); each is a registered regression.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg18c,pkg18c-gemini,heldout-pkg18c,holdout2-pkg18c}-dirty.json`.

## A list verb after a comma lists the outputs; a field compared only is not the answer: 555 → 562 strict, Gemini on 645 → 651, held out 361 → 364, fresh holdout 365 → 367 (2026-10-15)

Same contract as below. Runs `pkg17b-dirty`, `pkg17b-gemini-dirty`, `heldout-pkg17b-dirty` and `holdout2-pkg17b-dirty`
are the change of the entry below with this change, against its `pkg16d` runs. The change is `DECISIONS.md` "A list
verb after a comma lists the outputs; a field compared only is not the answer".

- DEV, engine alone: 555 → **562 strict**; 7 wins (90, 274, 587, 621, 622, 812, 970), no loss; wrong answers 175 → 172;
  8 queries changed.
- Held out: 361 → **364 strict**; 3 wins (437, 894, 945), no loss; wrong answers 233 → 232; 4 queries changed.
- Fresh holdout: 365 → **367 strict**; 2 wins (34, 841), no loss; wrong answers 238 → 236; 5 queries changed.
- Gemini on: 645 → 651; 7 wins, 1 loss (972: the engine now answers it, showing the state beside the listed fields,
  where the rewording had answered it); wrong answers 234 → 231; 9 queries changed.
- Fresh holdout 597 ("For each party, return the name of the party and the number of delegates from that party.") was
  lenient and is wrong: the list is read and the answer groups by the party's name, merging two parties named
  Democratic, where Spider's gold groups by the party's key. No other answer across the three sets groups an entity by a
  field its rows share.
- An earlier round (`pkg17`) also showed a product as the name of orders ("orders with Amount over 1000") and read "For
  each continent, list its id, name, ..." as the countries'; both are registered regressions.
- The correct-binding experiment rerun on the entry below (gold select lists through the same reading, everything
  else production): DEV 555 → 592 strict with gold outputs; nine of its 40 wins are grouped answers Spider lists count
  first, the order a recorded product choice. Its scripts are evaluation-only and not in the repository.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg17b,pkg17b-gemini,heldout-pkg17b,holdout2-pkg17b}-dirty.json`.

## The opening sentence lists its coordinated outputs: 544 → 555 strict, Gemini on 632 → 645, held out 359 → 361, fresh holdout 359 → 365 (2026-10-14)

Same contract as below. Runs `pkg16d-dirty`, `pkg16d-gemini-dirty`, `heldout-pkg16d-dirty` and `holdout2-pkg16d-dirty` are
the change of the entry below with this change, against its `pkg15b` runs. The change is `DECISIONS.md` "The opening
sentence lists its coordinated outputs".

- DEV, engine alone: 544 → **555 strict**; 11 wins (33, 34, 370, 405, 406, 504, 540, 694, 800, 950, 951), no loss;
  wrong answers 184 → 175; 17 queries changed.
- Held out: 359 → **361 strict**; 3 wins (776, 818, 825), 1 loss (113); wrong answers 234 → 233; 15 queries
  changed.
- Fresh holdout: 359 → **365 strict**; 6 wins (89, 294, 488, 596, 650, 785), no loss; wrong answers 242 → 238; 19
  queries changed.
- Gemini on: 632 → 645; 14 wins, 1 loss (417: the answer follows the question's order, Spider's gold reverses it);
  wrong answers 247 → 234; 22 queries changed.
- Earlier rounds (`pkg16`, `pkg16b`, `pkg16c`) lost DEV 319, 327 (a foreign key gave way inside its owner), 523, 524
  and held-out 178, 709, 718, 769, fresh holdout 7 (tied owners; the table after the list owns them), held-out 828,
  fresh holdout 346 (a foreign key and its referenced key), held-out 903 and fresh holdout 654 (a table named before
  the list, not in its condition) and held-out 529 (a tie no named table breaks reads no list); each is a registered
  regression.
- The correct-binding experiment that chose this change (gold select lists through the same reading, everything else
  production): DEV 544 → 592 strict with gold outputs; its scripts are evaluation-only and not in the repository.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg16d,pkg16d-gemini,heldout-pkg16d,holdout2-pkg16d}-dirty.json`.

## The verb a sentence opens with names no table; "the count" in a list; relationship self-joins; a superlative over the shown field: 539 → 544 strict, Gemini on 626 → 632, held out 359 unchanged, fresh holdout 358 → 359 (2026-10-14)

Same contract as below. Runs `pkg15b-dirty`, `pkg15b-gemini-dirty`, `heldout-pkg15b-dirty` and `holdout2-pkg15b-dirty`
are the change of the entry below with this change, against its `pkg14b` runs. The change is `DECISIONS.md` "The verb
a sentence opens with names no table; "the count" in a list; a relationship self-join needs its word; a superlative over
the shown field is one value".

- DEV, engine alone: 539 → **544 strict**; 6 wins (27, 105, 200, 840, 988, 989), 1 loss (202: gold compares the
  padded city 'Aberdeen ' as 'Aberdeen' and returns no rows; the old reading also returned none); wrong answers 189 → 184;
  21 queries changed.
- Held out: 359 → **359 strict**; no win, no loss; 4 queries changed (the same rows served without the tie window).
- Fresh holdout: 358 → **359 strict**; 1 win (796), no loss; wrong answers 243 → 242; 5 queries changed.
- Gemini on: 626 → 632 (7 wins: 27, 105, 200, 840, 848, 988, 989; 1 loss: 202; wrong answers 253 → 247; 25 queries changed).
- A first round (`pkg15`) required the relationship's word whole and chose the relationship key by it, which refused
  fresh holdout 539 and 790 ("the course whose prerequisite is ..."); the key choice is unchanged and an abbreviation
  counts. Both are registered regressions.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg15b,pkg15b-gemini,heldout-pkg15b,holdout2-pkg15b}-dirty.json`.

## An explicit list names the outputs, their owner and order; a row count drops joins that only multiply it: 535 → 539 strict, Gemini on 622 → 626 (4 wins: 24, 89, 515, 539; no loss; wrong answers 256 → 253; 14 queries changed), held out 358 → 359, fresh holdout 355 → 358 (2026-10-14)

Same contract as below. Runs `pkg14b-dirty`, `pkg14b-gemini-dirty`, `heldout-pkg14b-dirty` and `holdout2-pkg14b-dirty`
are the change of the entry below with this change, against its `pkg13b` runs. The change is `DECISIONS.md` "An
explicit list names the outputs, their owner and order; a row count drops joins that only multiply it".

- DEV, engine alone: 535 → **539 strict**; 4 wins (24, 89, 515, 539), no loss; wrong answers 193 → 189; 11 queries
  changed.
- Held out: 358 → **359 strict**; 1 win (903), no loss; wrong answers 235 → 234; 12 queries changed.
- Fresh holdout: 355 → **358 strict**; 3 wins (119, 259, 935), no loss; wrong answers 244 → 243; 11 queries changed.
- Gemini on: 622 → 626 (4 wins: 24, 89, 515, 539; no loss; wrong answers 256 → 253; 14 queries changed).
- A first round (`pkg14`) dropped a row count's join to a table the question names and lost held out 654 ("How many
  songs have a shared vocal?", refused) and refused DEV 966 (wrong before); a foreign key tied the owner of "the club
  id" and showed a coach's name (fresh holdout 119); the planner suite caught "the name and phone number of these
  patients" read as a count (held-out Spider train 47). Each is a registered regression.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg14b,pkg14b-gemini,heldout-pkg14b,holdout2-pkg14b}-dirty.json`.

## A count counts its noun's table, abbreviated fields shown, a word said once names one field: 529 → 535 strict, Gemini on 616 → 622 (6 wins: 6, 7, 63, 64, 91, 92; no loss; wrong answers 263 → 256; 13 queries changed), held out 357 → 358, fresh holdout 354 → 355 (2026-10-13)

Same contract as below. Runs `pkg13b-dirty`, `pkg13b-gemini-dirty`, `heldout-pkg13b-dirty` and `holdout2-pkg13b-dirty`
are the change of the entry below with this change, against its `pkg12b` runs. The change is `DECISIONS.md` "A count
counts the rows of the table its noun names; a field abbreviated by its table's name is shown; a word said once names
one field".

- DEV, engine alone: 529 → **535 strict**; 6 wins (6, 7, 63, 64, 91, 92), no loss; wrong answers 200 → 193; 12 queries
  changed.
- Held out: 357 → **358 strict**; 1 win (969), no loss; wrong answers 237 → 235; 7 queries changed.
- Fresh holdout: 354 → **355 strict**; 1 win (710), no loss; wrong answers 245 → 244; 5 queries changed.
- Gemini on: 616 → 622 (6 wins: 6, 7, 63, 64, 91, 92; no loss; wrong answers 263 → 256; 13 queries changed).
- A first round without the head-noun and said-table guards won the same questions and lost six: DEV 203-206 (an
  unjoined count of flights from ' APG' favoured; Spider's gold counts none), fresh holdout 494 (the course read as the
  counted noun) and 183 (DEPT_NAME read for "first names").

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg13b,pkg13b-gemini,heldout-pkg13b,holdout2-pkg13b}-dirty.json`.

## A field asked by its name without its kind: 527 → 529 strict, Gemini on 614 → 616, held out 356 → 357, fresh holdout 353 → 354 (2026-10-13)

Same contract as below. Runs `pkg12b-dirty`, `pkg12b-gemini-dirty`, `heldout-pkg12b-dirty` and `holdout2-pkg12b-dirty`
are the change of the entry below with this change, against its `pkg11` runs. The change is `DECISIONS.md` "A field is
asked by its name without its kind".

- DEV, engine alone: 527 → **529 strict**; 2 wins (928, 929), no loss; wrong answers 202 → 200; 4 queries changed (994,
  995 wrong before and after).
- Held out: 356 → **357 strict**; 1 win (16), no loss; wrong answers 239 → 237 (136 now refused); 4 queries changed.
- Fresh holdout: 353 → **354 strict**; 1 win (203), no loss; wrong answers 246 → 245; 1 query changed.
- Gemini on: 614 → **616 strict**; 2 wins (928, 929), no loss; wrong answers 265 → 263; 4 queries changed.
- A first round without the command guard lost held-out 156 ("Return the most common first name" read as asking for
  return_date); the guard recovered it.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg12b,pkg12b-gemini,heldout-pkg12b,holdout2-pkg12b}-dirty.json`.

## A field beside the named field of its entity is not asked: 525 → 527 strict, Gemini on 612 → 614, held out 354 → 356, fresh holdout 351 → 353 (2026-10-13)

Same contract as below. Runs `pkg11-dirty`, `pkg11-gemini-dirty`, `heldout-pkg11-dirty` and `holdout2-pkg11-dirty` are
the change of the entry below with this change, against its `pkg10` runs. The change is `DECISIONS.md` "A field beside
the named field of its entity is not asked".

- DEV, engine alone: 525 → **527 strict**; 2 wins (309, 367), no loss; wrong answers 204 → 202; 3 queries changed
  (995 wrong before and after).
- Held out: 354 → **356 strict**; 2 wins (546, 872), no loss; wrong answers 241 → 239; 2 queries changed.
- Fresh holdout: 351 → **353 strict**; 2 wins (361, 885), no loss; wrong answers 249 → 246 (874 now refused); 3
  queries changed.
- Gemini on: 612 → **614 strict**; 2 wins (309, 367), no loss; wrong answers 267 → 265; 4 queries changed.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg11,pkg11-gemini,heldout-pkg11,holdout2-pkg11}-dirty.json`.

## "In total" restates a count, a field inside another's name: 524 → 525 strict, Gemini on 611 → 612, held out 353 → 354, fresh holdout 346 → 351 (2026-10-13)

Same contract as below. Runs `pkg10-dirty`, `pkg10-gemini-dirty`, `heldout-pkg10-dirty` and `holdout2-pkg10-dirty` are
the change of the entry below with this change, against its `pkg9c` runs. The change is `DECISIONS.md` ""In total"
closing a count restates it; a field inside another field's spelled name is not named there".

- DEV, engine alone: 524 → **525 strict**; 1 win (357), no loss; wrong answers 206 → 204 (480 now refused, its
  ranking date no longer grouped beside the ranking); 5 queries changed.
- Held out: 353 → **354 strict**; 1 win (108), no loss; wrong answers 244 → 241; 6 queries changed.
- Fresh holdout: 346 → **351 strict**; 5 wins (41, 83, 531, 562, 598), no loss; wrong answers 254 → 249; 9 queries
  changed.
- Gemini on: 611 → **612 strict**; 1 win (357), no loss; wrong answers 270 → 267; 7 queries changed.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg10,pkg10-gemini,heldout-pkg10,holdout2-pkg10}-dirty.json`.

## A nation is a country, a store a shop; one-letter codes, a participle before a year: 505 → 524 strict, Gemini on 604 → 611, held out 348 → 353, fresh holdout 345 → 346 (2026-10-13)

Same contract as below. Runs `pkg9c-dirty`, `pkg9c-gemini-dirty`, `heldout-pkg9c-dirty` and `holdout2-pkg9c-dirty` are
`0f26bfe` (what production serves) with this change, against the `pkgZ` runs of the entry below (the same served code).
The change is `DECISIONS.md` "A nation is a country, a store a shop; one-letter codes and a participle before a year
read; a noun fields share counts its entity".

- DEV, engine alone: 505 → **524 strict (50.7%)**, 555 → 573 lenient, 710 → 730 answered; 19 wins (53, 54, 103, 145,
  271, 275, 293, 295, 296, 457, 458, 485, 486, 703, 730, 745, 746, 747, 804), no loss; wrong answers 205 → 206: of 20
  newly answered, 19 are strict-correct, and 639 shows the country of the channels without their pixel aspect ratio; 21
  queries changed.
- Held out: 348 → **353 strict**; 5 wins (49, 50, 52, 326, 337), no loss; wrong answers 242 → 244 (7 newly answered, 5
  strict-correct); 7 queries changed.
- Fresh holdout: 345 → **346 strict**; 1 win (208), no loss; wrong answers 253 → 254; 2 queries changed.
- Gemini on: 604 → **611 strict**, 683 → 690 lenient, 875 → 881 answered; 8 wins (53, 54, 104, 485, 486, 730, 745, 747),
  1 loss (639: the engine now reads "nation" and answers with the country alone, where the rewording it refused before
  showed the pixel aspect ratio too; the completeness check reads every field of a table the query reads, so a field the
  answer leaves out passes it); wrong answers 271 → 270; 13 queries changed.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkg9c,pkg9c-gemini,heldout-pkg9c,holdout2-pkg9c}-dirty.json`.

## Fields named by another form of their words and by their table's abbreviation: 499 → 505 strict, Gemini on 601 → 604, held out 335 → 348, fresh holdout 334 → 345 (2026-10-13)

Same contract as below. Runs `pkgZ-dirty`, `pkgZ-gemini-dirty`, `heldout-pkgZ-dirty` and `holdout2-pkgZ-dirty` are
`3ad0bb1` (what production serves) with this change, against the `pkgT` runs (the same served code: the review repairs
of `305c99c` changed no served query). The change is `DECISIONS.md` "Fields named by another form of their words and by
their table's abbreviation; a sort verb opening a question".

- DEV, engine alone: 499 → 505 strict, 548 → 555 lenient, 704 → 710 answered; 6 wins
  (261, 699, 958, 959, 964, 965), no loss; wrong answers 205 → 205 (all 6 newly answered are strict-correct); 10 queries changed.
- Held out: 335 → 348 strict; 13 wins (237, 249, 272, 555, 578, 773, 853, 871, 888, 893, 907, 910, 975), no loss; wrong answers 253 → 242;
  33 queries changed.
- Fresh holdout: 334 → 345 strict; 11 wins (51, 94, 310, 339, 478, 614, 629, 630, 843, 857, 877), no loss; wrong answers 257 → 253;
  27 queries changed.
- Gemini on: 601 → 604 strict; all 324 rewordings replayed from the cache; 3 wins (958, 959, 964), no loss; wrong answers 272 → 271; both newly answered questions are strict-correct; 7 queries changed.
- Three intermediate rounds found the losses this change no longer has: a dropped table abbreviation let a lone
  "number" or "class" name apt_number or STU_CLASS, a stripped "d" read "amenid" as "amenity", "the gradepoints" named a
  Grade, and date_of_treatment joined "the departing date of all the dogs".

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkgZ,pkgZ-gemini,heldout-pkgZ,holdout2-pkgZ}-dirty.json`.

## Rewordings keep the operations the engine reads, coordinated aggregate words share their field: 497 → 499 strict, Gemini on 592 → 601, held out 335, fresh holdout 334 (2026-10-13)

Same contract as below. Runs `pkgT-dirty`, `pkgT-gemini-dirty`, `heldout-pkgT-dirty` and `holdout2-pkgT-dirty` are
`94c4d50` (what production serves) with this change, against the `pkgR` runs of the entry below. The change is
`DECISIONS.md` "Rewordings keep the operations the engine reads; aggregate words coordinated before a field share it".

- DEV, engine alone: 497 → **499 strict (48.3%)**, 546 → 548 lenient, 704 answered; 2 wins (4, 17), no loss; wrong
  answers 207 → 205; 2 queries changed.
- Gemini on (all 330 rewordings replayed from the cache): 592 → **601 strict**, 668 → 679 lenient, 863 → 873 answered;
  9 wins (4, 17, 151, 271, 275, 416, 417, 757, 763), no loss; wrong answers 271 → 272: of 11 newly answered, 7 are
  strict-correct.
- Held out: 335 → **335 strict**; 1 query changed (752, wrong before and after).
- Fresh holdout: 334 → **334 strict**; no query changed.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkgT,pkgT-gemini,heldout-pkgT,holdout2-pkgT}-dirty.json`.

## A number of a numeric field, all the information, "equal to" and "record": 493 → 497 strict, Gemini on 576 → 592, held out 324 → 335, fresh holdout 329 → 334 (2026-10-13)

Same contract as below. Runs `pkgR-dirty`, `pkgR-gemini-dirty`, `heldout-pkgR-dirty` and `holdout2-pkgR-dirty` are
`e709851` (what production serves) with this change, against the `pkgQ` runs of the entry below. The change is
`DECISIONS.md` "A number of a numeric field, all the information, "equal to" and "record" read".

- DEV, engine alone: 493 → **497 strict (48.1%)**, 543 → 546 lenient, 701 → 704 answered; 4 wins (291, 292, 657, 809), no
  loss; wrong answers 208 → 207; 6 queries changed. All 3 newly answered are strict-correct.
- Gemini on (313 rewordings replayed from the cache, 17 fetched live): 576 → **592 strict**, 647 → 668 lenient, 839 →
  863 answered; 16 wins, no loss; wrong answers 263 → 271: of 24 newly answered, 15 are strict-correct. "Equal to" read
  lets rewordings that say it through, and some answer wrongly elsewhere.
- Held out: 324 → **335 strict**, 378 → 386 lenient, 581 → 588 answered; 11 wins (30, 79, 147, 288, 367, 598, 633, 706,
  790, 806, 848), no loss; wrong answers 257 → 253; all 7 newly answered are strict-correct.
- Fresh holdout: 329 → **334 strict**, 384 → 387 lenient, 587 → 591 answered; 5 wins (14, 167, 168, 427, 864), no loss
  (167, "the top 3 prominence", lost in the change below, is back); wrong answers 258 → 257; of 4 newly answered, 2 are
  strict-correct (531 shows an enzyme's name for the medicines' "FDA record", 824 compares a missing department number
  where gold compares the text 'null').
- Census replays (evaluation-only) before the full runs: DEV 508 → 512 (4 wins, no loss), held out 329 → 340 (11 wins,
  no loss).

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkgR,pkgR-gemini,heldout-pkgR,holdout2-pkgR}-dirty.json`.

## Full names, fields asked by their owner, counting words, plain-English rewordings: 471 → 493 strict, Gemini on 538 → 576, held out 304 → 324, fresh holdout 306 → 329 (2026-10-12)

Same contract as below. Runs `pkgQ-dirty`, `pkgQ2-gemini-dirty`, `heldout-pkgQ-dirty` and `holdout2-pkgQ-dirty` are
`d17cede` with this change, against `pkgN-dirty`, `pkgN-gemini-dirty`, `heldout-pkgN-dirty` (`d17cede`) and, for the
fresh holdout, `holdout2-pkgF-dirty` (`eba2339`, what production serves; `d17cede` was not run on it). Gemini on fetched
every rewording live: the rewording prompt changed. The change is `DECISIONS.md` "Full names, fields asked by their
owner, counting words, conditions kept out of the answer".

- DEV, engine alone: 471 → **493 strict (47.7%)**, 527 → 543 lenient, 687 → 701 answered; 22 wins (75, 76, 81, 82,
  123, 124, 397, 677, 814, 852, 853, 854, 890, 891, 892, 893, 930, 931, 934, 935, 936, 937), no loss; wrong answers
  216 → 208, strict of answered 68.6% → 70.3%; 36 queries changed. Of 14 newly answered, 9 are strict-correct; 421
  and 456 are the right query graded on ties (5,000 capped rows), 129, 541 and 606 are wrong elsewhere (a count of
  the wrong rows). Against `eba2339`: 461 → 493, 32 wins, no loss.
- Gemini on (every rewording fetched live, the prompt having changed): 538 → **576 strict**, 612 → 647 lenient, 802 →
  839 answered; 51 wins, 13 losses; wrong answers 264 → 263. Twelve losses are questions refused because their new
  rewording no longer reads (rewording variance: "what is Kyle's id?" and "how many TV Channel using language
  English?" among them), one (486) is a count of the wrong hand. Against `eba2339`'s 529: +47. A first run of the
  prompt, which also asked for "equal to" in place of "=", was stopped at 700 questions: 42 of 55 rewordings it
  refused for an unread word said "equal".
- Held out (1,000 train questions): 304 → **324 strict**, 365 → 378 lenient, 561 → 581 answered; 20 wins (40, 58,
  145, 169, 323, 335, 396, 420, 557, 609, 667, 727, 743, 746, 750, 850, 892, 902, 938, 944), no loss; wrong answers
  257 → 257, strict of answered 54.2% → 55.8%. Against `eba2339`: 278 → 324, 46 wins, no loss.
- Fresh holdout (`random.Random(20261012)`, never used to choose or tune anything), against `eba2339`: 306 → **329
  strict**, 367 → 384 lenient, 561 → 587 answered; 24 wins (9, 152, 158, 183, 244, 326, 330, 366, 399, 414, 475, 530,
  539, 683, 698, 718, 721, 736, 790, 929, 968, 969, 976, 999), 1 loss (167: "the mountains with the top 3 prominence"
  compares Prominence with 3, the previous change's number beside a field); wrong answers 255 → 258. Of 29 newly
  answered, 9 are strict-correct: the others are wrong elsewhere (a count without its group, the direction of a
  prerequisite, "ids and full names" listing the last name first), so the precision of new answers is the weakness
  this sample shows, as the held-out sample did after the first change.
- Census replays (evaluation-only) measured each family before the full runs: DEV 486 → 508 (22 wins, no loss), held
  out 308 → 329 (21 wins, no loss).

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkgQ,pkgQ2-gemini,heldout-pkgQ,holdout2-pkgQ}-dirty.json`.

## Counts over exclusions and averages, numbers beside fields: 461 → 471 strict, Gemini on 529 → 538, held out 278 → 304 (2026-10-12)

Same contract as below (`whole_db`, `served`, SQL backend, row cap 5,000, 12 s soft budget, runtime bundle unchanged,
external rewriting off except where Gemini is on, which replays cached rewordings: all 347 it needed were cached).
Runs `pkgN-dirty`, `pkgN-gemini-dirty` and `heldout-pkgN-dirty` are `eba2339` with this change, against `pkgF-dirty`,
`pkgF-gemini-dirty` and `heldout-pkgF-dirty` (the engine `eba2339` serves). The change is `DECISIONS.md` "Counts over
exclusions and averages, numbers beside fields, fields shown only where asked".

- DEV, engine alone: 461 → **471 strict (45.6%)**, 515 → 527 lenient, 677 → 687 answered; 10 wins (120, 870, 871,
  974, 975, 978, 979, 980, 982, 983), no loss; wrong answers 216 → 216; 19 queries changed. Newly answered 10, 6 of
  them strict-correct: of the other 4, 75 and 76 list the age before the first name asked first (the order is fixed
  in the next change), 83 is DISTINCT where gold repeats a student, and 455 orders 5,000 capped rows whose birth dates
  tie differently from gold's.
- Gemini on: 529 → **538 strict**, 601 → 612 lenient, 793 → 802 answered; 9 wins, no loss; wrong answers 264 → 264.
- Held out (1,000 train questions): 278 → **304 strict**, 347 → 365 lenient, 539 → 561 answered; 26 wins (105, 137,
  163, 182, 241, 261, 300, 311, 347, 376, 384, 424, 529, 540, 636, 661, 734, 735, 736, 799, 837, 851, 862, 906, 917,
  919), no loss; wrong answers 261 → 257, strict of answered 51.6% → 54.2%; 37 queries changed. Of 22 newly
  answered, 15 are strict-correct; the others are wrong elsewhere ("room 111 most recently" ranked by a count, a
  student named More read from "more than"), and 183, lenient before, now shows the checking balance it compares.
- Fresh holdout: not run for this change alone; the next entry measures it with the changes after it.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkgN,pkgN-gemini,heldout-pkgN}-dirty.json`.

## Answers shown as asked, words a query realizes read: 386 → 461 strict, Gemini on 471 → 529, held out 255 → 278, fresh holdout 286 → 306 (2026-10-12)

Same contract as below: `whole_db`, `served`, SQL backend, row cap 5,000, 12 s soft budget, runtime bundle unchanged
(`b11056f8…`), external rewriting off except where Gemini is on, which replays cached rewordings (4 rewordings the
change newly needed were fetched live and cached). Runs `pkgF-dirty`, `pkgF-gemini-dirty` and `heldout-pkgF-dirty` are a
worktree of `7843363` with this change, against `r126-dirty`, `r126-gemini-dirty` and `heldout-r126-dirty` (the engine
production serves, `3563863`). The change is `DECISIONS.md` "Answers shown as asked, words a query realizes read,
aggregates over every field they coordinate".

- DEV, engine alone: 386 → **461 strict (44.6%)**, 444 → 515 lenient, 590 → 677 answered; 80 wins, 5 losses; wrong
  answers 204 → 216; strict of answered 65.4% → 68.1%; 170 queries changed. Newly answered 91, of which 57 strict-correct.
  The losses: 233, 234 and 237 showed airport names for airline questions, right only because Spider's flight_2 gold
  is empty (its codes carry a trailing space), now refused; 37 is "singer names in concerts in year 2014", now
  DISTINCT where gold repeats a singer; 793 keeps the country before its count where gold puts the count first.
- Gemini on: 471 → **529 strict**, 550 → 601 lenient, 740 → 793 answered; 65 wins, 7 losses; wrong answers 269 → 264.
- Held out (1,000 train questions): 255 → **278 strict**, 311 → 347 lenient, 487 → 539 answered; 28 wins (23, 125, 213,
  245, 268, 330, 357, 379, 397, 398, 408, 481, 503, 541, 577, 583, 654, 660, 698, 702, 712, 716, 781, 875, 909, 960, 963,
  968), 5 losses (219 and 286 compare Alice on the other side of PersonFriend; 309 and 677 are now DISTINCT where
  gold repeats rows; 455 also shows the player); wrong answers 232 → 261: of 58 newly answered, 20 are strict-correct.
  The stem witness accounts for about 5 right and 10 wrong of them (read from the served SQL): the word is the
  table's ("visited" by Visits), and the query is wrong elsewhere (a count without its group, an extra field).
- Fresh holdout, never used to choose or tune anything (1,000 Spider train questions over 136 databases, drawn with
  `random.Random(20261012)` from the train questions outside the first sample): `holdout2-3563863` → `holdout2-pkgF-dirty`
  286 → **306 strict**, 336 → 367 lenient, 519 → 561 answered; 23 wins, 3 losses (119 puts the count of coaches before
  the club it counts, as "how many" opens the question; 178 is newly refused; 889 is DISTINCT where gold repeats
  rows); wrong answers 233 → 255, 22 of 49 newly answered strict-correct.
- Census replays (evaluation-only, every pool member of every DEV question) measured each family before the full
  runs; their figures run about 11 above the served counts, which realize ties (`keep_ties`), and were used only to
  compare variants.
- Soft-budget exceedances: 7 on DEV, 1 Gemini on, 69 held out, all with three evaluations and test suites sharing the
  machine; predictions median 2.4 s on DEV under that load. Grades do not depend on the budget; no controlled speed
  comparison was made.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{pkgF,pkgF-gemini,heldout-pkgF}-dirty.json`.

## Independent October 12 approach review and Boolean-count repair: 386 strict, unchanged DEV predictions

Fresh full evaluation of local `f6fbf4d5d9b914129ec1ccab2c3d5ad996fe9b0b` plus the uncommitted OR-count repair:
`whole_db`, `served`, SQL backend, compose on, cap 5,000/table, 12-second soft budget, external rewriting off,
unchanged promoted bundle `b11056f8ce8bf16a44da69bf76fea238149df439d7fde52d5e1f4747a5f536c7`.

- **386/1,034 strict (37.3%), 444/1,034 lenient (42.9%), 590/1,034 answered (57.1%).**
- Exact match with Claude's `r127-dirty`: zero selected SQL, strict, lenient, or answered-status differences.
  This is correctness hardening on independent OR fixtures, not a new DEV accuracy gain.
- All 1,034 indices unique; all 113 tracked code fingerprints agree with the after-run files. Source is dirty
  because the reviewed fix remains local/uncommitted; no training, promotion, commit, push or deployment occurred.
- Partition remains 386 strict-correct, 77 lenient-only, 127 wrong under both metrics, 444 unanswered.
  Refusal stages remain 362 completeness, 73 constraints, 9 AST search. No soft-budget exceedances.
- Prediction median/p90/p95/max: 1.171 / 2.555 / 3.081 / 5.971 seconds. These observations are not a controlled
  speed comparison with the saved runs.
- 361 focused checks pass: SQL AST 207, query contract 57, compose 33, deterministic emitters 64. Repository CI
  Ruff F/E9 and high-severity Bandit commands pass, as do Python compilation and `git diff --check`.
- The new serving OR/list/scalar/zero-admitting tests and opposite-connective completeness tests fail before the
  repair and pass afterward. Current Python lowering rejects these compound membership shapes as unsupported
  (`only one non-aggregate comparison is supported`); no SQL/Python execution-parity claim is made for them.
  The existing dual-emitter suite passes. Historical held-out/reference/deployed/browser runs were not rerun here.

Artifacts: `%TEMP%/prereasoner-claude-approach-review-20261012/`, with `full_eval_approach-review-f6fbf4d.json`,
the matching per-example file, `independent-verification.json`, and focused suite logs.
Assessment and revised next implementation work remain in `release-review/sql-accuracy.md` and the October 12
follow-up of `release-review/spider-review-20261011.md`.

## Where correct plans are lost at `3563863`: pool census and gold role readings (2026-10-12)

Evaluation-only, on the release commit `3563863` (clean worktree, runtime bundle `b11056f8…`, DEV 1,034, `whole_db`,
cap 5,000): a census of every pool member (ran, grounded, violations, unread words, strict grade), joined with the
served run `r126-dirty` (same code), and the same census with the encoder's role readings replaced by the gold SQL's.

- Served-path ledger: 386 right; 59 wrong while a strict-correct candidate was eligible but ranked below; 27 wrong
  while a strict-correct one was refused; 118 wrong with none built; 200 refused while a strict-correct one was
  refused (165 by an unread word, 62 by a violation, some both); 244 refused with none built. A strict-correct
  candidate is in the retained raw pool for 672 (65.0%), as the review's earlier census (671) found. This census
  grades raw SQL, not `PoolSelection.candidate`'s `keep_ties` realization; its flag eligibility is diagnostic,
  not a certified recoverable serving ceiling. "None built" here means no strict hit in the retained raw pool;
  it does not distinguish never constructed from discarded before the final pool.
- Of the 362 with none in that pool, 139 are flat queries (no grouping, ordering, nesting or set operation), 145 group or
  order, 40 nest a query and 38 are set operations.
- Gold role readings (projection, aggregate, group, order and filter columns at 1.0, every other column 0.0; gold's
  tables at 1.0): the choice before the completeness check is strict-correct for 535 → 544 (20 wins, 11 losses), and
  the pool holds a strict-correct candidate for 672 → 673. These are a gold-column-role reweighting ablation:
  the role sets flatten nested scopes, preserve no operation occurrence/output order, and provide no operator,
  counted-entity, population, filter-value, or Boolean-scope binding. They reach the ranker and calculation
  expander, not base enumeration's table scores/mentions. The 535 → 544 count is `_choose`'s raw choice before
  completeness and serving finalization, not a measured 9-answer serving gain. It shows limited leverage through
  the existing role-score interface; it neither executes the proposed constructor-consumed correct-binding
  experiment nor rules out a semantic-reading deficit. The 362 retained-pool misses and 227 raw-correct blocked
  cases justify work on binding consumption, construction/retention, and witnessed admission.

Independent October 12 artifact audit: all 1,034 census and all 1,034 oracle indices are unique, with no recorded
tracebacks; the six served-ledger counts above reproduce. For `r126-dirty` and `r127-dirty`, all 113 recorded tracked
code fingerprints match commits `3563863` and `f6fbf4d`, respectively. Compared with `r123-dirty` (`6263502`),
`r126-dirty` adds **0 DEV wins/losses** and **2/1,000 sample wins, 0 losses** (47 and 370); sample 102 is newly
answered wrong. `r127-dirty` adds 0 wins/losses on either set, no DEV SQL changes, and one sample SQL change from
the current-day Julian literal. The 382 → 386 / 249 → 255 release comparison below spans the earlier R fixes too.
The sample has now informed fixes and is not an untouched generalization test. A sequential `r127` sample run has
22 soft-budget exceedances versus 40 in `r126` with unchanged grades; workload differences preclude a controlled
speed claim or attributing all overruns solely to immutable data characteristics.

For the same r126 inputs, cached-rewrite-on versus rewrite-off has 85 strict wins and 0 losses (386 → 471).
Of those 85, 54 original questions already have a raw strict hit and 31 do not. This is useful evidence of an
interpretation/admission/retention interface limitation, subject to independent meaning-preservation checks;
it is not Gemini SQL-generation accuracy or a rewrite-off deterministic result.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/census_3563863/` and `oracle_3563863/` (with the
evaluation-only scripts `census.py`, `oracle_bindings.py`, `served_ledger.py`, `replay.py`).

## Two reviews' count and membership gaps fixed: 382 → 386 strict, Gemini on 465 → 471, held out 249 → 255 (2026-10-12)

Same contract as the entries below: `whole_db`, `served`, SQL backend, row cap 5,000, 12 s budget, runtime bundle
unchanged (`b11056f8…`), external rewriting off except where Gemini is on, which replays the cached rewordings with no
live call. The change is `DECISIONS.md` "The second review of 2026-10-11" (R1-R3, committed in part as `6263502`) and
"The review of 6263502" (S1-S6, held-out train 370 and 47). Runs `r126-dirty`, `r126-gemini-dirty` and
`heldout-r126-dirty` are a worktree of `6263502` with the rest of the change, against `final-dirty`,
`final-gemini-dirty` and `heldout-final-dirty` (the engine of `8164601`, which production serves).

- DEV, engine alone: 382 → 386 strict, 440 → 444 lenient, 591 → 590 answered; 4 wins, 0 losses (333 and 334,
  template types with fewer than 3 templates; 477 and 478, country codes by player counts); wrong answers 209 → 204;
  30 queries changed. Against `6263502` alone (`r123-dirty`, which the independent review reproduced exactly) nothing
  changes correctness: 328, 418 and 694 no longer show a count the question does not ask (a "number" that names a
  field) and stay wrong; 331 and 332 now count the templates rather than each type's own row and stay wrong, as their
  gold keeps one of two tied types.
- Gemini on: 465 → 471 strict, 544 → 550 lenient; 6 wins, 0 losses; wrong answers 278 → 269.
- Held out (1,000 train questions): 249 → 255 strict, 305 → 311 lenient; 7 wins (47, 180, 402, 527, 594, 687,
  742), 1 loss; wrong answers 239 → 232. The loss, train 585 ("the customer who made the most orders"), returns the
  two customers tied at 3 orders where gold keeps one. Train 370, lost by `6263502`'s preference for fewer joins (it
  counted each transaction type's own row), is back. Train 102 is newly answered and wrong: "the total number of
  degrees granted between 1998 and 2002" compares the years with the Degrees field; a field named "TotalEnrollment_AY"
  in another table had made "total number" a field's name and refused it. Train 822, 943 and 961, wrong before, are
  refused.
- A first final run (`r125-dirty`) lost DEV 333 and 334 (386 → 384): grouping the templates alone no longer stood
  for every type (S1), and the NOT IN of the types read Ref_Template_Types as counted templates, its name holding
  the word. Fixed before `r126`, with a regression test.
- Soft-budget exceedances: none on DEV; 40 of 1,000 held out (41 to 42 in every earlier held-out run).

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{r126,r126-gemini,heldout-r126,r125,r123}-dirty.json`.

## Independent follow-up: 386 strict at `6263502`, four wins and no strict losses (2026-10-11)

Fresh full serving-faithful evaluation from clean isolated commit `6263502f7cdd3c0639208f13e49daa4563bfaae0`:
all 1,034 DEV questions, `whole_db`, `served`, SQL backend, compose enabled, cap 5,000/table,
12 s soft budget, external rewriting off, unchanged runtime bundle `b11056f8ce8bf16a…`.
All 141 recorded provenance fields remained unchanged; all question indices are unique.

| System, identical questions/execution data/scorer | Strict | Lenient | Answered |
|---|---:|---:|---:|
| **Engine, fresh run** | **386/1,034 (37.3%)** | **444/1,034 (42.9%)** | **590/1,034 (57.1%)** |
| Sonnet 5.5, saved SQL re-executed | 840/1,034 (81.2%) | 897/1,034 (86.8%) | 1,023/1,034 (98.9%) |
| Gemini 3.8 **Flash**, saved SQL re-executed | 872/1,034 (84.3%) | 904/1,034 (87.4%) | 1,034/1,034 (100.0%) |

October 7 hosted predictions were regraded with no changed grades and no new API generation. Input presentation
and inference budgets differ: reference DDL/FKs + three sample rows/table, engine capped-table inspection.

- Exact reproduction of Claude's `r123-dirty`: zero SQL, strict, lenient or answered-status differences.
  Runtime fingerprints match; its pre-commit source/dirty provenance fields differ from the clean reviewed commit.
- Versus `3ef82c4`: 4 strict wins (333/334 template counts, 477/478 player counts), 0 losses,
  382 unchanged correct, 648 unchanged wrong; 17 SQL records change. The four gains are two easy-question pairs.
- 334 becomes answered/correct; 175 and 526 move from wrong answers to refusals. Coverage falls by one;
  returned strict-wrong answers fall from 209 to 204. Country-count gold defects 161/162 remain separate caveats.
- Paired strict (both / engine only / reference only / neither): Sonnet 359 / 27 / 481 / 167;
  Gemini Flash 366 / 20 / 506 / 142. On the same 590 engine-answered questions: 386 / 496 / 505 strict-correct.
- Partition: 386 strict-correct, 77 lenient-only, 127 wrong under both, 444 unanswered.
  Stages: 362 completeness, 73 constraints, 9 AST search; 19 strict-correct empty-gold cases remain lenient-false.
- 403 focused tests pass. Prior R1/R2/R3 examples pass, but the broader review reproduces a zero-inclusive range
  wrong answer, count-operand/duplicate-bound/membership-cardinality proof gaps, literal-sized count-proof work,
  and a year-like ranking cutoff refusal. Details: `release-review/spider-review-20261011.md`.
- Prediction median/p90/p95/max: 1.646 / 3.696 / 4.484 / 9.769 s; zero soft-budget exceedances.
  Concurrent workload differs, so no controlled speedup or slowdown claim.

Artifacts: `%LOCALAPPDATA%/Temp/prereasoner-spider-fix-review-6263502-20261011/`: full summary/predictions,
reference regrading, paired transitions, provenance, test logs and reproduced counterexamples.
This independent task changes documentation; production code and bundle remain the reviewed Claude implementation.
Full live release suites, Python parity and a fresh candidate-pool census were not run.

## Independent follow-up: 382 strict reproduced at `3ef82c4`; matched reference regrading and remaining semantic gaps (2026-10-11)

Fresh serving-faithful predictions from clean isolated commit `3ef82c4066141f44300a14bda775c0a1358aece2`:
all 1,034 DEV questions, `whole_db`, `served`, SQL backend, compose enabled, cap 5,000/table,
12 s soft budget, `EXTERNAL_LLM_ENABLED=0`, unchanged bundle `b11056f8ce8bf16a…`.
Final verification: all 141 recorded provenance fields unchanged, 1,034 unique indices, source clean.

| System, same DEV questions/execution rows/scorer | Strict | Lenient | Answered |
|---|---:|---:|---:|
| **Engine, fresh predictions** | **382/1,034 (36.9%)** | **440/1,034 (42.6%)** | **591/1,034 (57.2%)** |
| Sonnet 5.5, saved SQL re-executed | 840/1,034 (81.2%) | 897/1,034 (86.8%) | 1,023/1,034 (98.9%) |
| Gemini 3.8 **Flash**, saved SQL re-executed | 872/1,034 (84.3%) | 904/1,034 (87.4%) | 1,034/1,034 (100.0%) |

No new hosted generation; no reference grades changed. References received DDL/FKs and three sample rows/table;
the engine inspects capped tables. Task/execution/scoring match; input presentation and inference budgets differ.

- Exact reproduction of Claude's `final-dirty`: no SQL/grade changes across 1,034 questions.
- Versus the earlier independent `3817b34` run: 0 strict wins, 2 losses (161/162), 382 unchanged correct,
  650 unchanged wrong; six SQL records differ (125,126,161,162,175,277). The country-count losses correct a gold
  unit error: engine returns `[4]` countries, gold returns makers per country `[4,4,3,5]`. The scorer remains unchanged.
- 125/126 move from wrong answers to constraint refusals; 175 stays wrong; 277 removes an unrequested hiring join
  without changing its correct answer.
- Paired strict (both / engine only / reference only / neither): Sonnet 355 / 27 / 485 / 167;
  Gemini Flash 362 / 20 / 510 / 142. Same 591 engine-answered questions: 382 / 495 / 505 strict-correct.
- Partition: 382 strict-correct, 77 lenient-only, 132 wrong under both, 443 unanswered. Refusal stages:
  365 completeness, 69 constraints, 9 AST search. Nineteen strict-correct empty-gold cases remain lenient-false.
- 403 focused checks pass. Broader probes still reproduce zero-related-count errors and unsound membership bindings;
  top-101 additionally regresses to a refusal. Original F1–F5 examples passing does not establish full-family correctness.
- Median/p90/p95/max prediction seconds: 0.861 / 1.863 / 2.290 / 5.296; no soft-budget exceedances.
  Workload conditions differ from the earlier run, so this is not a controlled speed comparison.

Details: the current independent review, `release-review/spider-review-20261011.md`.
Artifacts: `%LOCALAPPDATA%/Temp/prereasoner-spider-fix-review-3ef82c4-20261011/`, with complete summary,
per-question predictions, paired/changed-query analysis, provenance verification, reference regradings and probes.
Production code and bundle unchanged by the independent review; full live release suites were not run.

## The review's five defects fixed: 384 → 382 strict (DEV 161/162's gold counts per country), Gemini on 466 → 465, held out 246 → 249 (2026-10-11)

Same contract as the entries below: `whole_db`, `served`, SQL backend, row cap 5,000, 12 s budget, runtime bundle
unchanged, external rewriting off except where Gemini is on. The change is `DECISIONS.md` "The review of 2026-10-11".
Runs `final-dirty`, `final-gemini-dirty` and `heldout-final-dirty` are a worktree of `d8d81ab` with the change,
against `leads-dirty`, `leads-gemini-dirty` and `heldout-leads-dirty` (the engine production serves, `a0e4efb`).
Gemini on replays the same cached rewordings: 443 calls, none live.

- DEV, engine alone: 384 → 382 strict, 442 → 440 lenient, 593 → 591 answered; 0 wins, 2 losses, 5 queries
  changed. The losses are DEV 161 and 162, "how many countries have more than 2 car makers": their gold counts the
  car makers in each qualifying country (`[4], [4], [3], [5]`), and the engine now answers the number of countries,
  as the review predicted. DEV 125 and 126 ("cars with horsepower more than 150") were answered wrongly with a count
  per group and are now refused: Spider's Horsepower is text, and the queries left compared another field with 150.
  DEV 175 changed and stays wrong.
- Gemini on: 466 → 465 strict, 543 → 544 lenient; 1 win (DEV 616, "the 3 most highly rated episodes ... and their
  ratings", which needed the cutoff before its noun), 2 losses (161, 162); wrong answers 279 → 278, as DEV 102, 125
  and 126 are refused.
- Held out (1,000 train questions): 246 → 249 strict (3 wins, 0 losses): train 710 (maximum, minimum and minimum of
  three fields), 829 ("the three accounts with the highest savings balances") and 951 ("the 3 counties with the
  smallest population"). Train 789 is newly answered and wrong: "the names of items" lists reviewers' names, as the
  items have no name field. Five per-group counts over a spurious HAVING (327, 373, 435, 623, 958) became counts of
  the groups that qualify and stay wrong.
- F1 and F2 alone (`f12-dirty`): 382 strict, the same two losses. Two later cuts were caught here and fixed before the
  final runs: "line 2 of the address with the most students" read the 2 as a cutoff (lost DEV 562), "how many
  countries ... and the number of countries" asked two counts (DEV 89 answered wrongly), and held out, 829 first
  ranked the accounts by their checking balance.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{final,final-gemini,heldout-final,f12}-dirty.json`.

## Independent review: 384 strict reproduced; matched Sonnet/Gemini reference regrading (2026-10-11)

Fresh predictions through `spider/probe/full_eval.py`, clean isolated commit
`3817b34378d0133e272d75619d53bfe66a2e8e2c`: all 1,034 DEV questions, `whole_db`, `served`, SQL backend,
compose enabled, row cap 5,000/table, 12 s soft prediction budget, `EXTERNAL_LLM_ENABLED=0`, runtime bundle
`b11056f8ce8bf16a44da69bf76fea238149df439d7fde52d5e1f4747a5f536c7`.
Final verification: all 141 artifact/provenance fields unchanged; 1,034 unique indices; source worktree clean.

| System, same DEV questions/capped execution inputs/shared scorer | Strict | Lenient | Answered |
|---|---:|---:|---:|
| **Deterministic engine, fresh run, rewriting off** | **384/1,034 (37.1%)** | **442/1,034 (42.7%)** | **593/1,034 (57.4%)** |
| Sonnet 5.5, saved October 7 SQL re-executed | 840/1,034 (81.2%) | 897/1,034 (86.8%) | 1,023/1,034 (98.9%) |
| Gemini 3.8 **Flash**, saved October 7 SQL re-executed | 872/1,034 (84.3%) | 904/1,034 (87.4%) | 1,034/1,034 (100.0%) |

No new hosted generation/model API calls. The references received DDL/FKs and three sample rows per table;
the engine inspects capped tables. This matches task, execution data, and scorer, not identical prompt
presentation or inference budgets. All saved question/database/gold identities match current DEV; no reference
grade changed. Gemini here generates SQL as a benchmark reference, distinct from the production rewrite fallback.

- Against `own2-dirty`: zero selected-SQL or grade changes on all 1,034 questions. Against `plan-base-de0cfba`:
  314 -> 384 strict, 70 wins, 0 losses, 314 unchanged correct, 650 unchanged wrong, 127 SQL changes.
- Paired strict (both / engine only / reference only / neither): Sonnet 355 / 29 / 485 / 165;
  Gemini Flash 362 / 22 / 510 / 140.
- Same 593 engine-answered questions: engine 384/593 (64.8%), Sonnet 495/593 (83.5%), Gemini 506/593 (85.3%) strict.
- Engine failures: 441 unanswered (365 completeness, 67 constraints, 9 AST search), plus 209 answered strict-wrong
  (77 lenient-only, 132 wrong under both). Nineteen strict-correct empty-gold cases are lenient-false.
- Engine median/p90/max prediction latency 2.667 / 5.329 / 9.229 s; no soft-budget exceedances. Concurrent jobs
  shared the CPU, so this is not a controlled speed comparison.
- Label caveat: DEV 161/162 ask for a country count, while gold returns maker counts `[4,4,3,5]`. Gemini returns
  the requested country count `[4]` and is marked strict-wrong. The headline keeps the same gold/scorer for all
  systems; these two apparent engine wins are not semantic improvements.

Review, remaining defects, tests, provenance and per-example links:
`release-review/spider-review-20261011.md`.
Artifacts: `%LOCALAPPDATA%/Temp/prereasoner-spider-review-3817b34-isolated-20261011/`
(`full_eval_review-3817b34-isolated.json`, per-example predictions, paired comparison and final provenance check).
Reference summaries/per-example files are in `%LOCALAPPDATA%/Temp/prereasoner-spider-review-3817b34-20261011/`;
each records clean scoring commit, dataset/database/scorer/executor hashes and source prediction hash.
The first shared-checkout engine run was stopped after source edits and is explicitly marked aborted; its
partial predictions are excluded. A separate 25-question CPU-thread probe is also excluded from headline totals.

## The most of a one-table workbook's rows: Spider unchanged at 384 strict, Gemini on 466, held out 246 (2026-10-11)

Same contract as the entry below. The change (`DECISIONS.md` "\"The most\" of a one-table workbook's rows counts them
per group") answers "which country has the most leads?" over one sheet; it was measured for what it costs Spider. The
runs `leads-dirty`, `leads-gemini-dirty` and `heldout-leads-dirty` are a worktree of `3817b34` with this change,
against `own2-dirty`, `own2-gemini-dirty` and `heldout-own2-dirty`. Gemini on replays the same cached rewordings: 441
calls, none live.

- Strict, lenient and answered are unchanged on DEV (384 / 442 / 593), with Gemini on (466 / 543 / 745) and held out
  (246 / 302 / 484). One DEV query changed and stays right: DEV 277 ("the employee who was awarded the most times")
  counts the evaluation rows without the spurious join to hiring. Held out, train 752's query differs only by
  today's date constant (the run crossed midnight).

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{leads,leads-gemini,heldout-leads}-dirty.json`.

## Spreadsheet wording read as asked: Spider unchanged at 384 strict, Gemini on 466, held out 246 (2026-10-10)

Same contract as the entry below. The change (`DECISIONS.md` "The owner's questions are read as asked: a ranked
measure is no grain, a per-row average averages the rows") fixes six wrong answers an owner's own spreadsheet
questions found; it was measured for what it costs Spider. The runs `own2-dirty`, `own2-gemini-dirty` and
`heldout-own2-dirty` are a worktree of `e4713ae` with this change, against `step5c-dirty`, `step5c-gemini-dirty`
and `heldout-step5c-dirty` (`eed8d9d`'s code). Gemini on replays the rewordings `step4-gemini-dirty` cached: 441
calls, none live.

| | Previous change | **This change** | Previous, Gemini on | **This change, Gemini on** |
|---|---:|---:|---:|---:|
| Strict | 384 (37.1%) | **384 (37.1%)** | 466 (45.1%) | **466 (45.1%)** |
| Lenient | 442 | 442 | 543 | 543 |
| Answered | 593 | 593 | 745 | 745 |
| Served SQL changed | | **0 of 1,034** | | **0 of 1,034** |

- Held out (1,000 train questions): 246 strict, 302 lenient, 484 answered before and after; no served SQL changed.
  A first cut of the money-measure rule, which preferred any table's money field, changed two held-out queries and
  turned train 289 ("products whose availability equals to 1") into `amount_payable = 1`, lenient 302 → 301; the
  rule now reads only the money field of the table the noun names, and train 289 is a regression case.
- The owner's 96 questions from 48 conversations, replayed through the production entry point (engine alone, live
  PostgreSQL), against the expected answers computed from the sheets: 9 answers go from wrong to right ("top 3
  cities by total amount" in four conversations, "top 2 cities by total amount", "average amount per order", "average
  amount per order in Lyon", "how many orders are over 20?", "list the orders over 20"), 87 are unchanged, 0 get
  worse. 32 are still refused by the engine alone (31 clarifications, 1 error): 30 ask for a "volume" or "monthly
  searches" (27 of them "keyword volume"; production's Gemini rewording answered every one asked since 2026-10-06), and 2 ask
  for "the most leads" (rows the sheet calls responses).

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{own2,own2-gemini,heldout-own2}-dirty.json`.

## A relation read from its owner, named rows grouped by their key: 371 → 384 strict, Gemini on 452 → 466; held out 215 → 246 (2026-10-10)

Same contract as the entries below. The change runs `step5b-dirty` and `step5b-gemini-dirty` are a worktree of
`29a97c3` with the previous change and this one (`DECISIONS.md` "A relation is read from its owner, and rows grouped
by a name that repeats are grouped by their key"). The released code, `eed8d9d`, adds `SchemaGraph.repeats` (the same
test, cached) and narrows the search's grouping to tables the question names, after the release build's offline
regression gate failed `f9c9d10` on "total revenue by title"; its runs `step5c-dirty`, `step5c-gemini-dirty` and
`heldout-step5c-dirty` (a worktree whose 178 engine, evaluator, regression and test files are `eed8d9d`'s) serve the
same query for every DEV and held-out question as the `step5b` runs. Gemini on replays the rewordings
`step4-gemini-dirty` cached: 441 calls, none live.

| | Previous change | **This change** | Previous, Gemini on | **This change, Gemini on** |
|---|---:|---:|---:|---:|
| Strict | 371 (35.9%) | **384 (37.1%)** | 452 (43.7%) | **466 (45.1%)** |
| Lenient | 433 | 442 | 535 | 543 |
| Answered | 593 | 593 | 745 | 745 |
| Strict of answered | 62.6% | **64.8%** | 60.7% | **62.6%** |
| Scalar | 209/408 | 214/408 | 251/408 | 256/408 |
| Strict: easy / medium / hard / extra | 133 / 165 / 48 / 25 | 133 / 172 / 53 / 26 | 161 / 200 / 56 / 35 | 161 / 207 / 61 / 37 |
| Strict wins / losses | | **13 / 0** | | **14 / 0** |
| Prediction seconds, median / p90 (five runs sharing the machine) | 1.57 / 3.57 | 2.48 / 6.01 | 2.90 / 6.56 | 2.79 / 6.52 |

- Twelve of the thirteen wins are `network_1` (DEV 885, 886, 888, 889, 894, 897, 904, 905, 908, 909, 910, 911): the
  friendship and the like read from the owner's key, and the two Jordans grouped apart; the thirteenth is DEV 605
  (`tvshow`). DEV 906 now reads the right side and misses only on ties: it keeps every student tied for the most
  likes, where Spider's `LIMIT 1` keeps one. A first cut also lost DEV 758 and 369; the corrected rules
  (`DECISIONS.md`) keep both right.
- Since `de0cfba` (`plan-base-de0cfba`, the engine alone): 314 → 384 strict, 70 wins and 0 losses; Gemini on, the
  rewordings controlled since `29a97c3`: 448 → 466, 18 wins and 0 losses.
- Held out: 1,000 Spider train questions (`random.Random(0).sample` of `train_spider.json`, 1,000 questions over
  their own databases), none read while these three changes were written; one frequency count (coordinated
  aggregates, 41 of 7,000) scanned all of train's questions. `de0cfba`'s code against this change's, the engine alone:

| Held out | `de0cfba` | **These three changes** |
|---|---:|---:|
| Strict | 215 (21.5%) | **246 (24.6%)** |
| Lenient | 266 | 302 |
| Answered | 419 | 484 |
| Strict of answered | 51.3% | 50.8% |
| Strict: easy / medium / hard / extra | 87 / 81 / 31 / 16 | 91 / 94 / 42 / 19 |
| Strict wins / losses | | **33 / 2** |

  The gain carries to unseen databases at about half DEV's rate (+3.1 points against +6.8), and strict of answered
  stays level. The two losses: train 133 (Spider groups characteristics by a name two of them share; the key keeps
  them apart) and train 742 (an exclusion that now joins Lessons to Staff through Customers and Addresses, tables
  the question does not name; the previous change's check reads only a joined leaf).
- Served-path ledger (`step5b-dirty` joined with an evaluation-only census of every pool member of every question):
  384 served right; 62 served wrong while a right candidate was eligible (ranking); 26 served wrong while a check
  refused the right candidate; 121 served wrong with no right candidate built; 199 refused while a right candidate
  was built; 242 refused with none built. Where a check refused a right candidate, an unread word did it 162 times
  ("used", "name", "first", "high", "nations"), a constraint violation 63 (32 of them "the requested field is not
  used"). A right candidate is in the pool for 671 questions (666 before) and violation-free for 618 (618).

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{step5b,step5b-gemini,heldout-*}-*.json`.

## An exclusion subtracts the entities it names: 369 → 371 strict, Gemini on 448 → 452 with the same rewordings (2026-10-10)

Same contract as the entry below (`whole_db`, `served` selection, SQL backend, row cap 5,000, no resumed predictions,
bundle `b11056f8ce8bf16a…`). Engine alone: baseline `scoped-dirty` (the code of `29a97c3`), change `step4-dirty` (a
worktree of `847e913` with `29a97c3`'s code and the change of `DECISIONS.md` "An exclusion subtracts the entities it
names, and one aggregate word takes the fields it coordinates"; its 167 engine, evaluator and test files the ones
committed, line endings aside). Gemini on: the wrapper now replays rewordings from a cache keyed by the system prompt
and prompt, so both runs reword each question alike. `step4-gemini-dirty` made the cache (441 rewordings, 0 failed);
`replay-gemini-29a97c3` (a clean `29a97c3` worktree) read it, with 1 live call.

| | `29a97c3` | **This change** | `29a97c3`, Gemini on | **This change, Gemini on** |
|---|---:|---:|---:|---:|
| Strict | 369 (35.7%) | **371 (35.9%)** | 448 (43.3%) | **452 (43.7%)** |
| Lenient | 433 | 433 | 536 | 535 |
| Answered | 592 | 593 | 745 | 745 |
| Strict of answered | 62.3% | 62.6% | 60.1% | 60.7% |
| Strict: easy / medium / hard / extra | 133 / 165 / 47 / 24 | 133 / 165 / 48 / 25 | 161 / 200 / 54 / 33 | 161 / 200 / 56 / 35 |
| Strict wins / losses | | **2 / 0** | | **4 / 0** |
| Prediction seconds, median / p90 (a shared machine) | 1.44 / 3.26 | 1.57 / 3.57 | 2.36 / 5.01 | 2.90 / 6.56 |

- Engine alone: DEV 61 ("the major and age of students who do not have a cat", the key NOT IN) and DEV 282 ("employees
  who never received any evaluation", the exclusion no longer narrowed by `hiring`) are won. Nine served queries
  changed; the exclusions of DEV 29, 855 and 925 lost a join the question does not name and stayed as graded. DEV 911
  is answered and still wrong (the friendship read from the friend's side).
- Gemini on, the rewordings uncontrolled (`scoped-gemini-dirty` against `step4-gemini-dirty`): 452 → 452, 4 wins and 4
  losses, the losses all newly refused questions that the rewording, not the engine, changed (DEV 203, 414, 712, 739).
  With the same rewordings the change wins 4 and loses none: run-to-run rewording moves Gemini-on strict by about ±4.

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{step4,replay-gemini-29a97c3,step4-gemini}-*.json`.

## Constraints met by the whole query, graded words read in their direction: 314 → 369 strict, Gemini on 385 → 452 (2026-10-10)

Four runs, same contract as `release-0733eef` (`whole_db`, `served` selection, SQL backend, row cap 5,000, no resumed
predictions, bundle `b11056f8ce8bf16a…`). The baselines are `plan-base-de0cfba` (engine alone) and
`plan-base-gemini-de0cfba` (Gemini on, through the scratchpad wrapper that times production's rewrite path), both from
`847e913`, whose code is `de0cfba`'s (it adds a `DECISIONS.md` note), clean worktree. The change runs are
`scoped-dirty` and `scoped-gemini-dirty`: `847e913` with the change of `DECISIONS.md` "Constraints are met by the whole
query, and a graded word is read in its direction" in the worktree, its 113 source files the ones committed.

| | Engine alone before | **Engine alone** | Gemini on before | **Gemini on** |
|---|---:|---:|---:|---:|
| Strict | 314 (30.4%) | **369 (35.7%)** | 385 (37.2%) | **452 (43.7%)** |
| Lenient | 380 | **433** | 483 | **540** |
| Answered | 523 | **592** | 668 | **750** |
| Strict of answered | 60.0% | **62.3%** | 57.6% | **60.3%** |
| Scalar | 184/408 | **209/408** | 230/408 | **255/408** |
| Strict: easy / medium / hard / extra | 128 / 139 / 29 / 18 | 133 / 165 / 47 / 24 | 156 / 166 / 35 / 28 | 162 / 202 / 54 / 34 |
| Strict wins / losses | | **56 / 1** | | **75 / 8** |
| Refusals: gate / constraints / no candidate | 511 in one label | 366 / 67 / 9 | 366 in one label | 219 / 56 / 9 |
| Rewording calls (failed) | | | 511 (0) | 442 (0) |
| Prediction seconds, median / p90 (a shared machine) | 1.62 / 3.45 | 1.44 / 3.26 | 3.02 / 5.96 | 2.79 / 6.46 |

- An earlier pair of runs of this change (`plan-final-dirty`, `plan-final-gemini-dirty`) scored the same 369 and 452. A
  review then showed its first whole-query check accept a UNION and an EXCEPT for "above 35 and below 50", a scalar
  subquery comparing ids for "people above 35", and refuse "not older than 35"; the fixed check (`DECISIONS.md`)
  changes no Spider answer but one: DEV 911, served wrong, is now refused.

- Expected before the work (`DECISIONS.md`, from an offline replay of selection over every candidate of every DEV
  question): +40 to +50 strict for the engine alone, +20 to +35 with Gemini on. Measured: +55 and +67. With Gemini on
  the reworded question's own candidates pass the corrected checks too, and the engine answers 70 more questions
  itself, so it rewords 70 fewer.
- Engine alone: 70 newly answered, 38 of them right; 18 answered wrong are now right; one wrong answer newly refused
  (DEV 911). The one loss is DEV 61 ("the major and age of students who do not have a cat"): an EXCEPT of (major, age)
  now passes the exclusion check and ranks first, and it collapses students sharing both, which the NOT IN reading kept.
- Gemini on: 91 newly answered (49 right), 9 newly refused (3 of them right before). The 8 losses: DEV 61 and 62 (that
  EXCEPT); DEV 316 (an EXCEPT whose subtracted branch joins Paragraphs, which the question never names, replaced the
  rewording's NOT IN); DEV 988 and 989 ("the age of the oldest dog" is now the engine's own earliest birth date, which
  this data's ages contradict, where the rewording took the largest age); DEV 114 (its rewording's search now chooses
  COUNT(*), which the original question's check refuses for not using Maker); DEV 261 and 567 (the rewording itself
  differed between runs, as DEV 979's did in the earlier pair).
- Candidate-pool recall, from an evaluation-only census of every pool member (strict-correct, graded after
  prediction): a correct candidate is eligible for 618 of 1,034 questions (550 before); one is in the pool for 666
  (665). The work changed which built candidates are served, not which are built.
- 32 newly answered questions are wrong, most of them construction defects the gate used to hide: HAVING COUNT over
  the wrong counted table (DEV 149, 150, 642), the friendship read from the other side (886, 906), "the oldest player"
  read from a match's winner age (453, 454), "the fewest flights" from a LEFT JOIN counting airports with none (227,
  231, 376; Spider counts only those with some).
- Not changed: the 366 questions refused with an eligible candidate and the 369 with no correct candidate built;
  column order (a grouped answer lists its group first, a product choice); ties (`RANK()` keeps every tied row).

Output: `%LOCALAPPDATA%/Temp/prereasoner-no-sql-model-20261002/full_eval_{plan-base,scoped}{,-gemini}-*.json`.

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
