# Deterministic SQL Planner

This is the starting point for PreReasoner's typed SQL planner. The planner searches a
bounded space of valid SQL abstract syntax trees (ASTs). It does not sample SQL tokens
from a decoder.

## What it does

Given a question, tables, and foreign keys, the planner:

1. Builds a typed schema graph.
2. Links question roles to tables, columns, operators, and values.
3. Constructs and validates candidate ASTs.
4. Expands recursive queries, constraints, extrema, and set operations when applicable.
5. Optionally uses a frozen proposal model to add exact-profile AST variants.
6. Ranks candidates with deterministic features.
7. Optionally promotes a high-confidence generated candidate with a frozen gated ranker.
8. Renders only validated ASTs to SQL.

The same inputs, configuration, and artifacts always produce the same ordering.
Determinism removes sampling variance. It does not remove natural-language ambiguity,
schema-linking errors, missing search rules, or ranking errors.

## Architecture

The final runtime path is:

```text
question + tables + foreign keys
          |
          v
      SchemaGraph
          |
          v
  typed bounded AST search
          |
          +-------------------------------+
          | optional frozen proposer      |
          | profile + role predictions    |
          v                               |
  exact-profile AST expansion <-----------+
          |
          v
  deterministic semantic ranking
          |
          +-------------------------------+
          | optional frozen tree ranker   |
          | held-out promotion gate       |
          v                               |
  strict-scoped candidate promotion <-----+
          |
          v
      validated SQL
```

The proposal model never emits SQL. It predicts structural profiles and schema roles.
Every generated variant must still pass AST validation. The learned ranker never changes
an AST; it only reorders existing candidates.

## Public API

Use `engine.sql` for stable imports.

### Base planner

```python
from engine.sql import SQLSearcher

tables = [
    {
        "name": "orders",
        "columns": ["id", "customer_id", "amount"],
        "rows": [[1, 10, 25.0], [2, 10, 40.0], [3, 11, 12.5]],
    },
    {
        "name": "customers",
        "columns": ["id", "name"],
        "rows": [[10, "Ada"], [11, "Lin"]],
    },
]
foreign_keys = [{
    "from_table": "orders",
    "from_col": "customer_id",
    "to_table": "customers",
    "to_col": "id",
    "conf": 1.0,
}]

searcher = SQLSearcher.from_tables(tables, foreign_keys)
candidates = searcher.search("list each customer name and total order amount")

print(candidates[0].sql)
print(candidates[0].query)       # typed AST
print(candidates[0].evidence)    # generation and ranking trace
print(candidates[0].features)    # numeric ranking features
```

### Profile-aware strict planner

`DeterministicSQLPlanner` composes the final optional proposal and gated-ranker path:

```python
from engine.encoder_overlay import EncoderQuery
from engine.sql import (
    DeterministicSQLPlanner,
    ProfileSearchConfig,
    ProposalSignalProvider,
    SQLProposalModel,
    SQLSearcher,
    load_ranker_model,
)

searcher = SQLSearcher.from_tables(tables, foreign_keys, max_candidates=180)
encoder = EncoderQuery()
proposer = SQLProposalModel.load("engine/data/sql_proposer.json")
provider = ProposalSignalProvider(proposer, encoder)
ranker = load_ranker_model("engine/data/sql_profile_ranker.json")

planner = DeterministicSQLPlanner(
    searcher=searcher,
    signal_provider=provider,
    rank_model=ranker,
    profile_config=ProfileSearchConfig(),
)
candidates = planner.search("list each customer name and total order amount")
```

Proposer and ranker metadata are bound to the exact encoder adapter, proposer file,
candidate-pool size, and profile-generation settings. A mismatch is a hard error. The
current promotion gate is disabled because it did not pass full Spider dev; do not assume
a training-split gain improves strict, lenient, or scalar serving accuracy.

### Live serving

`TableQuery.serve` exposes two explicit rollout modes:

| `PREREASONER_SQL_PLANNER` | Runtime behavior |
|---|---|
| `legacy` | Existing slot-filling planner. This remains the default. |
| `ast` | Typed AST search with inspectable hand ranking. This is the production mode. |

The proposer, profile expansion, and learned ranker are explicit research options, not
hidden serving modes. Evaluation code must supply the proposer, an explicit
`ProfileSearchConfig`, and the ranker artifact. Candidate zero is the preserved baseline;
only a promotion gate that passes artifact contracts and accuracy gates may replace it.
The current committed ranker records a failed Spider dev gate and cannot promote.

The own-data `/api/knowledge` response keeps its existing SQL and result fields and adds a
`planner` object for AST modes with `mode`, `ast`, `candidate_count`, `evidence`, and
`features`. Research integrations bound the proposal descriptor-vector cache to 4,096
entries on a long-lived serving object.

## Search configuration

`ProfileSearchConfig` is the single source of truth for profile expansion:

| Setting | Default | Meaning |
|---|---:|---|
| `max_candidates` | 32 | Maximum generated profile candidates. |
| `per_profile` | 4 | Maximum retained bindings for one predicted profile. |
| `generation_penalty` | 5.0 | Prior penalty applied to generated variants. |
| `binding_quality_weight` | 2.0 | Weight for proposal role-binding quality. |
| `preserve_baseline_top` | `True` | Keep the hand-ranked winner unless gated promotion is used. |

The older keyword arguments on `SQLSearcher.search` remain for compatibility. New code
should pass `profile_config`.

## Supported SQL

The AST, validator, renderer, and search rules support:

- multiple projections and `DISTINCT`;
- `COUNT`, `SUM`, `AVG`, `MIN`, and `MAX`;
- typed comparisons, ranges, dates, categorical values, `AND`, and `OR`;
- grouping, `HAVING`, ordering, and limits;
- direct and multi-hop foreign-key joins;
- aliases and self-joins;
- scalar subqueries, `IN`, `NOT IN`, `EXISTS`, and `NOT EXISTS`;
- derived tables and nested aggregation;
- `UNION`, `INTERSECT`, and `EXCEPT`;
- row extrema, frequency extrema, zero-inclusive counts, dual extrema, and top-N.

Grammar support does not imply perfect language coverage. A valid AST can still be absent
because the question was linked to the wrong role or no search rule proposed that shape.

## Validation and safety

Before rendering, recursive validation checks:

- table and alias scope;
- join connectivity;
- scalar and set-query arity;
- operand and literal types;
- grouped projection and ordering rules;
- compound-query compatibility;
- invalid aggregate forms such as `COUNT(DISTINCT *)`.

The renderer quotes identifiers and literals. The planner emits query ASTs, not arbitrary
SQL statements. Serving also retains its SELECT-only execution guard.

## Module map

| Module | Responsibility |
|---|---|
| `engine/sql.py` | Stable public imports. |
| `engine/sql_planner.py` | Final high-level proposer/search/gate orchestration. |
| `engine/sql_ast.py` | Immutable AST, validation, and rendering. |
| `engine/sql_schema.py` | Typed schema and join-path search. |
| `engine/sql_search.py` | Base beam and capability ordering. |
| `engine/sql_expansion.py` | Shared AST construction helpers. |
| `engine/sql_recursive.py` | Recursive queries, sets, aliases, and self-joins. |
| `engine/sql_constraints.py` | `HAVING`, disjunction, scalar, and membership rules. |
| `engine/sql_extrema.py` | Extrema, top-N, and set difference. |
| `engine/sql_rank.py` | Hand semantic and execution features. |
| `engine/sql_profile.py` | Structural AST profiles. |
| `engine/sql_profile_expansion.py` | Profile configuration and exact-profile expansion. |
| `engine/sql_proposal.py` | Frozen proposal artifact inference. |
| `engine/sql_proposal_runtime.py` | Shared schema normalization and encoder caching. |
| `engine/sql_learned_rank.py` | Frozen ranker inference and promotion gate. |

`SQLSearcher.search` remains the low-level ablation boundary. The capability modules do
not depend on one another's private internals.

## Spider results

The current full-dev measurements use all 1,034 Spider dev examples, Spider foreign
keys, recursively referenced gold tables, pool 180, and denotation evaluation.

| Configuration | Strict top-1 | Strict top-10 | Strict pool oracle | Avg. candidates |
|---|---:|---:|---:|---:|
| Phase 5 typed search | 426 (41.2%) | 484 (46.8%) | 490 (47.4%) | 5.39 |
| Explicit profile expansion, baseline protected | 426 (41.2%) | 545 (52.7%) | 570 (55.1%) | 21.31 |

Profile expansion adds 80 strict-reachable answers without changing top-1. The current
projection linker distinguishes properties that share table-name words, qualifies
ambiguous properties by entity, and follows outbound owner foreign keys for display
names. Against the previous exact serving artifact this produces 18 strict wins and one
strict loss. The remaining loss is a known grouped distinct-count target ambiguity.

The learned ranker was trained on an older candidate distribution. Its calibrated
promotion candidate regressed full Spider dev, so the committed gate is disabled and no
ranker result is presented as a current accuracy claim.

The **serving-faithful** `full_eval.py --planner ast --selection serving_top1 --max-candidates 25`
path (NO proposer, NO ranker — byte-for-byte `engine/tables.py:_serve_ast`) scores **389/1034 strict
(37.6%)**, **509/1034 lenient (49.2%)**, and **235/408 scalar (57.6%)** on gold-tables. (Attaching the
proposer + profile expansion at pool 180 yields the *same* top-1 numbers — the proposer changed zero
top-1 selections in that run — so the accuracy is entirely the deterministic planner's; the proposer/
expansion only raise pool recall, ~47%→55%, which top-1 selection does not yet convert.) `ast_eval.py`
is an isolated AST-search benchmark; `full_eval.py --planner ast` is the serving-selector measurement.
Do not compare them as if they were the same pipeline.

### What the numbers mean

The corrected pool contains a strict-correct query for 570 examples, but the protected
top-1 is correct for only 426. This leaves two distinct problems:

1. **Selection:** 144 reachable answers are not selected first.
2. **Generation:** 464 examples still have no strict-correct AST in the pool.

Determinism makes both failures reproducible and inspectable; it does not solve either
one. A larger decoder is not the first remedy. The next useful data is targeted,
same-profile contrastive supervision for projection identity, aggregate shape, zero-
inclusive counts, and multi-table role binding.

Detailed records:

- `spider/results/ast_profile_projection_final.json`: current isolated search and pool recall;
- `spider/results/ast_profile_failure_analysis_final.json`: final failure-family counts;
- `spider/results/ast_profile_failure_details_final.json`: per-example failure diagnoses;
- `spider/results/ast_profile_nc90_ranked_v3.json`: rejected promotion experiment;
- `spider/results/full_eval_ast_projection_final/`: exact serving-selector
  summary, per-example records, and resumable checkpoint.

## Training and reproduction

Fetch Spider data:

```bash
python spider/probe/fetch_data.py --include-train
```

Build structured proposal supervision and train the proposer:

```bash
python spider/probe/build_ast_proposal_data.py
python spider/probe/train_ast_proposer.py \
  --out engine/data/sql_proposer.json \
  --report spider/results/ast_proposer.json
```

Train the profile-aware ranker. Question-vector checkpoints are resumable and candidate
caches are reusable; both are fingerprinted. Calibration databases must remain outside
the final fit:

```bash
python spider/probe/train_ast_ranker.py \
  --dbs spider/data/dbs \
  --proposer-model engine/data/sql_proposer.json \
  --pool 180 --negative-pool 48 --estimators 120 \
  --holdout-promotion-calibration \
  --cache spider/data/profile_ranker_nc90_v2_cache.jsonl \
  --out engine/data/sql_profile_ranker_candidate.json
```

Training writes a candidate artifact. `--holdout-promotion-calibration` divides the
database-disjoint holdout again and requires the same threshold to show positive gain
with zero losses on both schema cohorts. That is still not deployment approval: run the
full Spider dev serving gate and replace the committed artifact only when strict,
lenient, and scalar acceptance criteria pass.

The rejected whole-database ablation is reproduced with `--config whole_db`,
`--execution-timeout 1`, and `--execution-row-limit 10000`. Those training-only bounds
prevent pathological wrong joins from monopolizing hard-negative mining and are recorded
in cache and model provenance. Whole-database training reuses one schema graph and
in-memory SQLite database per Spider database.

Evaluate the final path:

```bash
python spider/probe/ast_eval.py \
  --dbs spider/data/dbs --pool 180 --top-k 10 \
  --proposer-model engine/data/sql_proposer.json \
  --profile-expansion \
  --out spider/results/ast_profile_projection_final.json

python spider/probe/full_eval.py \
  --dbs spider/data/dbs --planner ast --config gold_tables \
  --selection serving_top1 --max-candidates 180 \
  --proposer-model engine/data/sql_proposer.json \
  --profile-expansion --tag ast_projection_final \
  --out spider/results/full_eval_ast_projection_final
```

`train_ast_ranker.py` refuses Spider dev as training data unless the diagnostic-only
override is explicit. Frozen ranker inference does not require scikit-learn.

## Tests

```bash
python -m tests.test_sql_ast
```

The 93 hermetic tests execute generated SQL against in-memory SQLite and cover AST
typing, rendering, joins, recursion, constraints, extrema, profiles, artifacts, cache
provenance, promotion calibration, deterministic ordering, and all live AST modes.

Run the repository aggregate suite with:

```bash
python -m tests.run_all
```

## What remains

The planner is coherent and deterministic, but Spider is not solved. The next work should
be measured against the current bottlenecks:

1. Close the ranking gap between selected and strict-reachable candidates.
2. Add search rules or structured supervision for examples with no strict candidate.
3. Calibrate promotion for the product's answer metric instead of reusing a strict-only gate.
4. Treat larger encoders as controlled capacity experiments after objective and data changes.

Historical Phase 1-6 names remain in evaluator output for ablation compatibility. They are
not separate runtime architectures and should not drive new module boundaries.
