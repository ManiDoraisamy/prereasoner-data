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
proposer = SQLProposalModel.load("spider/data/sql_proposer.json")
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

The promotion gate is trained for Spider strict denotation. Do not assume it improves
lenient containment or scalar-only metrics.

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

All figures below use Spider dev's 1,034 examples, Spider foreign keys, recursively
referenced gold tables, pool 180, and denotation evaluation.

| Configuration | Strict top-1 | Strict top-10 | Strict pool oracle | Avg. candidates |
|---|---:|---:|---:|---:|
| Phase 5 typed search | 408 (39.5%) | 460 (44.5%) | 465 (45.0%) | 5.60 |
| Compressed profile expansion | 408 (39.5%) | 529 (51.2%) | 553 (53.5%) | 22.02 |
| Safeguarded learned ordering | 408 (39.5%) | 541 (52.3%) | 553 (53.5%) | 22.02 |
| Held-out gated promotion | **417 (40.3%)** | **541 (52.3%)** | 553 (53.5%) | 22.02 |

The 1.5-point promotion threshold was selected on 28 databases excluded from the final
tree fit. Calibration observed 16 wins and zero losses over 29 promotions. On dev it adds
nine net strict answers. Raw unconstrained reranking is not safe: it scores 352 strict
answers (34.0%).

The strict gain is objective-specific. Gated promotion scores 48.4% lenient and 54.9%
scalar, below the protected baseline's 49.8% and 56.9%. Keep baseline-top preservation
for general use unless strict denotation is the explicit objective.

The current ceiling is candidate recall: 553 examples contain a strict-correct query,
but only 417 select it first. Another 481 examples still have no strict-correct candidate.
Determinism cannot recover an AST that search never constructs.

Detailed records:

- `spider/results/ast_profile_ranked.json`: final dev metrics;
- `spider/results/ast_proposer_ablation.json`: experiment and promotion decisions;
- `spider/results/ast_failure_analysis.json`: candidate-recall diagnosis;
- `spider/results/ast_proposer.json`: proposer validation metrics.

## Training and reproduction

Fetch Spider data:

```bash
python spider/probe/fetch_data.py --include-train
```

Build structured proposal supervision and train the proposer:

```bash
python spider/probe/build_ast_proposal_data.py
python spider/probe/train_ast_proposer.py \
  --out spider/data/sql_proposer.json \
  --report spider/results/ast_proposer.json
```

Train the profile-aware ranker. The question-vector and candidate caches are resumable
and fingerprinted. Calibration databases must remain outside the final fit:

```bash
python spider/probe/train_ast_ranker.py \
  --dbs spider/data/dbs \
  --proposer-model spider/data/sql_proposer.json \
  --pool 180 --negative-pool 48 --estimators 120 \
  --holdout-promotion-calibration \
  --cache spider/data/profile_ranker_cache.jsonl \
  --out engine/data/sql_profile_ranker.json
```

Evaluate the final path:

```bash
python spider/probe/ast_eval.py \
  --dbs spider/data/dbs --pool 180 --top-k 10 \
  --proposer-model spider/data/sql_proposer.json \
  --ranker-model engine/data/sql_profile_ranker.json \
  --out spider/results/ast_profile_ranked.json
```

`train_ast_ranker.py` refuses Spider dev as training data unless the diagnostic-only
override is explicit. Frozen ranker inference does not require scikit-learn.

## Tests

```bash
python -m tests.test_sql_ast
```

The 73 hermetic tests execute generated SQL against in-memory SQLite and cover AST
typing, rendering, joins, recursion, constraints, extrema, profiles, artifacts, cache
provenance, promotion calibration, and deterministic ordering.

Run the repository aggregate suite with:

```bash
python -m tests.run_all
```

## What remains

The planner is coherent and deterministic, but Spider is not solved. The next work should
be measured against the current bottlenecks:

1. Integrate the profile-aware planner into the live execution path and measure latency.
2. Close the gap between 417 selected and 553 strict-reachable examples.
3. Add search rules or structured supervision for the 481 examples with no strict candidate.
4. Improve whole-database table selection; the reported configuration uses gold tables.
5. Treat larger encoders as controlled capacity experiments after objective and data changes.

Historical Phase 1-6 names remain in evaluator output for ablation compatibility. They are
not separate runtime architectures and should not drive new module boundaries.
