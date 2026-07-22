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

### Live serving

`TableQuery.serve` and the Postgres own-data route use the same planner behind an
explicit rollout setting:

| `PREREASONER_SQL_PLANNER` | Runtime behavior |
|---|---|
| `legacy` | Existing slot-filling planner. This remains the default. |
| `ast` | Typed AST search with hand ranking. |
| `ast_profile` | Typed search plus the frozen structured proposer. Baseline top-1 is preserved. |
| `ast_strict` | Profile search plus the held-out strict-denotation promotion gate. |

The proposer and ranker default to `engine/data/sql_proposer.json` and
`engine/data/sql_profile_ranker.json`. Override them with
`PREREASONER_SQL_PROPOSER` and `PREREASONER_SQL_RANKER`. Artifacts are loaded lazily
once per serving object; an invalid mode, missing artifact, or ungated strict ranker is
reported in the normal serving error envelope.

The own-data `/api/knowledge` response keeps its existing SQL and result fields and adds a
`planner` object for AST modes with `mode`, `ast`, `candidate_count`, `evidence`, and
`features`. The proposal descriptor-vector cache is bounded to 4,096 entries on the
long-lived serving object.

For a conservative production rollout, start with `ast_profile`: it expands the
candidate pool but protects the deterministic hand-ranked winner. Use `ast_strict`
only when strict denotation is the product objective. The Spider-calibrated gate is not
a general guarantee for containment or scalar-only evaluation.

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

### Whole-database serving baseline

Gold tables are useful for isolating SQL construction, but live serving sees every table
in the uploaded database. The corresponding all-table Spider dev run is:

| Configuration | Strict top-1 | Strict top-10 | Strict pool oracle | Avg. candidates |
|---|---:|---:|---:|---:|
| Phase 5 typed search | 336 (32.5%) | 435 (42.1%) | 457 (44.2%) | 23.29 |
| Compressed profile expansion | 336 (32.5%) | 489 (47.3%) | 531 (51.4%) | 36.56 |
| Safeguarded learned ordering | 336 (32.5%) | 500 (48.4%) | 531 (51.4%) | 36.56 |
| Existing-ranker promotion | **344 (33.3%)** | **500 (48.4%)** | 531 (51.4%) | 36.56 |
| All-table-ranker safeguarded | 336 (32.5%) | 483 (46.7%) | 531 (51.4%) | 36.56 |
| All-table-ranker promotion | 316 (30.6%) | 483 (46.7%) | 531 (51.4%) | 36.56 |

The realistic pool loses only 22 reachable answers relative to the 553-example
gold-table pool, while selected top-1 loses 73 answers relative to 417. This localizes
the dominant live gap to ranking and role binding in the presence of distractor tables,
not merely to candidate construction.

Training directly on all-table candidate pools improves database-disjoint Spider-train
validation from 430 to 484 top-1 answers (32.4% to 36.4%), but does not generalize to
Spider dev. Its gated promotion drops dev strict top-1 from 336 to 316 and its safeguarded
top-10 drops from 500 with the existing ranker to 483. The artifact is retained as a
rejected ablation; live serving continues to use `sql_profile_ranker.json`. More data is
not enough when the learned objective exploits database-specific distractor patterns.

The current ceiling is candidate recall: 553 examples contain a strict-correct query,
but only 417 select it first. Another 481 examples still have no strict-correct candidate.
Determinism cannot recover an AST that search never constructs.

Detailed records:

- `spider/results/ast_profile_ranked.json`: final dev metrics;
- `spider/results/ast_profile_ranked_whole_db.json`: realistic all-table baseline;
- `spider/results/ast_profile_ranked_whole_db_ranker.json`: rejected all-table ranker;
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

Train the profile-aware ranker. Question-vector checkpoints are resumable and candidate
caches are reusable; both are fingerprinted. Calibration databases must remain outside
the final fit:

```bash
python spider/probe/train_ast_ranker.py \
  --dbs spider/data/dbs \
  --proposer-model spider/data/sql_proposer.json \
  --pool 180 --negative-pool 48 --estimators 120 \
  --holdout-promotion-calibration \
  --cache spider/data/profile_ranker_cache.jsonl \
  --out engine/data/sql_profile_ranker.json
```

The rejected whole-database ablation is reproduced with `--config whole_db`,
`--execution-timeout 1`, and `--execution-row-limit 10000`. Those training-only bounds
prevent pathological wrong joins from monopolizing hard-negative mining and are recorded
in cache and model provenance. Whole-database training reuses one schema graph and
in-memory SQLite database per Spider database.

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

The 79 hermetic tests execute generated SQL against in-memory SQLite and cover AST
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
