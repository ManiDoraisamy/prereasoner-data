# World-model evaluation

This suite measures the part of PreReasoner that Spider cannot measure: deterministic
source grounding, world-table joins, world attributes, and calculations over the
joined rows. Spider is self-contained; this suite uses the live `knowledgebase`
tables and an oracle query for each case.

## Method

Each case contains uploaded rows, a question, and an independent SQL oracle. The
oracle joins the uploaded rows to `knowledgebase."Countries"`,
`knowledgebase."Cities"`, or `knowledgebase."Elements"` and computes the expected
denotation. PreReasoner runs its normal `KnowledgeReasoner` path against the same
database. `world_eval/run.py` compares row sets using the same exact and lenient
definitions used by the Spider reports.

The cases intentionally cover different execution shapes:

| Case | Capability |
|---|---|
| `sales_by_continent` | country -> continent, grouped sum |
| `top_continent` | grouped argmax over a world dimension |
| `total_in_asia` | world filter plus sum |
| `countries_in_europe` | world filter plus count |
| `amount_by_currency` | country -> ISO currency code, grouped sum |
| `avg_atomic_mass` | element -> world `mass`, average |
| `total_amount_france` | city -> country, two uploaded tables, sum |
| `count_customers_france` | city -> country, count |

## Last recorded result

The last isolated run is **8/8 exact (100%) and 8/8 lenient (100%)**. It was run
with the seeded Postgres world database and the same serving model used by the
world endpoint.

This result predates the provenance-bearing output format and is retained as regression history,
not as evidence for a new release. A release run must record the source commit, clean-worktree state,
weight repository and revision, weight-manifest fingerprint, and the maintained-table release state.

| Case | Result |
|---|---|
| `sales_by_continent` | PASS |
| `top_continent` | PASS (`Asia`) |
| `total_in_asia` | PASS (`310`) |
| `countries_in_europe` | PASS (`2`) |
| `amount_by_currency` | PASS (ISO codes: `BRL`, `CNY`, `EUR`, `INR`, `JPY`, `USD`) |
| `avg_atomic_mass` | PASS (`9.672666666666666`) |
| `total_amount_france` | PASS (`270`) |
| `count_customers_france` | PASS (`2`) |

The fixes behind this run are source-grounded element fallback routing, compound
world-attribute recognition for `atomic mass`, grouped argmax SQL, ISO-code currency
projection, and key-correct resolution trace rendering. These are deterministic
planner fixes; no retraining was needed.

## Reproduce

Load the repository environment, point `KB_PG_*` at the intended source-pinned
database snapshot, and run:

```bash
python -m world_eval.run
```

The evaluator writes `world_eval/results/world_eval_results.json`, including the exact runtime and
database provenance. Do not compare runs made against different source releases as if they were the
same benchmark.

## Limits

Eight cases are a smoke and regression corpus, not a market-wide accuracy estimate.
They do not cover every source family, alias variation, temporal join, missing-data
policy, or multi-hop calculation. Expand the corpus before making claims about
general Schema.org or domain coverage.
