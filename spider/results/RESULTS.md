# Spider Results

Dated entries come newest first. Each records a run at the commit it names; the scripts, options, and models an older
entry names (the 7B SQL proposer, the arbiter, `training/rank/`, `--selection arbiter`) may since have
been removed, and that commit holds the code that ran.


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
