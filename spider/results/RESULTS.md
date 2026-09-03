# Spider Results

This is the current, reproducible measurement for the deterministic typed-AST planner. Both summary
artifacts were generated with the serving-faithful selector (`serving_top1`, max 25 candidates) over the
Spider dev set: 1,034 examples and 20 databases. Each JSON records its exact source commit, code hashes,
model hashes, settings, and `worktree_dirty=false`.

| Configuration | Evidence commit | Strict | Lenient | Scalar-gold |
|---|---|---:|---:|---:|
| `whole_db` — all tables, gold-blind (standard Spider comparison) | `cf82141` | **359/1,034 (34.7%)** | **453/1,034 (43.8%)** | **224/408 (54.9%)** |
| `gold_tables` — oracle table set (planner upper bound) | `fb4aa80` | **434/1,034 (42.0%)** | **551/1,034 (53.3%)** | **247/408 (60.5%)** |

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

The 7.3-point strict gap shows that table-set retrieval is a major next lever, but the 42.0% oracle strict
score also shows that retrieval is not the whole problem. Remaining losses are concentrated in projection
identity, relationship direction, operator choice, multi-step nesting, and deterministic candidate ranking.
The next accuracy work should measure those error classes from the per-example traces before changing the
model or adding another competing planner.

Older benchmark rows remain available in git history. They are not repeated here because they used retired
planner or routing implementations and are not comparable to this report.
