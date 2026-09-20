# Spider Results

This is the current, reproducible measurement for the deterministic typed-AST planner. Both summary
artifacts were generated with the serving-faithful selector (`serving_top1`, max 25 candidates) over the
Spider dev set: 1,034 examples and 20 databases. Each JSON records its exact source commit, code hashes,
model hashes, settings, and `worktree_dirty=false`.

Last reproduced: **2026-09-06** from clean source commit `93bc1b3`.

| Configuration | Evidence commit | Strict | Lenient | Scalar-gold |
|---|---|---:|---:|---:|
| `whole_db` — all tables, gold-blind (standard Spider comparison) | `93bc1b3` | **359/1,034 (34.7%)** | **453/1,034 (43.8%)** | **224/408 (54.9%)** |
| `gold_tables` — oracle table set (planner upper bound) | `93bc1b3` | **434/1,034 (42.0%)** | **551/1,034 (53.3%)** | **247/408 (60.5%)** |

The whole-db row is the standard comparison number: it includes table-set selection. The oracle row feeds
only tables referenced by the gold query and isolates AST reasoning and ranking. Relative to whole-db, the
oracle removes **75 strict misses (7.3 percentage points)**, **98 lenient misses (9.5 points)**, and **23
scalar misses (5.6 points)**. This is the measured table-selection opportunity, not a claim that oracle
tables are available in production.

## Difficulty

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
python -m spider.probe.full_eval \
  --dbs spider/data/dbs --config whole_db \
  --selection serving_top1 --max-candidates 25 \
  --tag current_release_clean
python -m spider.probe.full_eval \
  --dbs spider/data/dbs --config gold_tables \
  --selection serving_top1 --max-candidates 25 \
  --tag current_release_gold_clean
```

Run from a clean commit for release evidence. The evaluator intentionally records a dirty-worktree flag
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
