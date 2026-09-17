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
| `whole_db` @25 + parsimony expander | **533/1,034 (51.5%)** | 612 (59.2%) | 365 (35.3%) | 168 (16.2 pts) |

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
