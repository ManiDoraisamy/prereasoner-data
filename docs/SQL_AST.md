# Deterministic SQL Planner

This is the first document to read when working on PreReasoner's typed SQL planner.
The planner searches a bounded space of valid SQL abstract syntax trees (ASTs). It
does not generate SQL token by token, repair malformed SQL, or sample from a decoder.

## Mental model

Given a question, table data, and foreign keys, the planner:

1. Builds a typed schema graph from columns, observed values, and relationships.
2. Links question spans to tables, columns, literals, aggregates, and operators.
3. Expands ambiguous bindings in a bounded deterministic beam.
4. Connects required tables through direct or multi-hop foreign-key paths.
5. Adds recursive, constraint, extrema, and set-operation candidates when their
   question cues and schema preconditions are satisfied.
6. Rejects invalid trees with scope, type, grouping, arity, and join validation.
7. Renders valid ASTs to SQL and ranks them with inspectable features.
8. Optionally reranks a bounded prefix from execution results or a frozen model.

The same question, schema, values, settings, and model artifact always produce the
same ordered candidates. Determinism removes sampling variance; it does not remove
semantic ambiguity or guarantee that the correct AST exists in the searched pool.

## Code map

| Module | Responsibility |
|---|---|
| `engine/sql.py` | Stable public imports for planner users. |
| `engine/sql_ast.py` | Immutable AST nodes, recursive validation, and SQL rendering. |
| `engine/sql_schema.py` | Typed columns, observed values, foreign keys, and join-path search. |
| `engine/sql_candidate.py` | Shared immutable scored-candidate contract. |
| `engine/sql_search.py` | Base beam search and ordered capability-expansion pipeline. |
| `engine/sql_expansion.py` | Shared schema-linking and AST construction support for expanders. |
| `engine/sql_recursive.py` | Subqueries, membership, set operations, derived tables, and self-joins. |
| `engine/sql_constraints.py` | `HAVING`, disjunctions, scalar comparisons, and relation membership. |
| `engine/sql_extrema.py` | Row/frequency extrema, top-N, and set difference. |
| `engine/sql_rank.py` | Hand-written semantic features and bounded execution reranking. |
| `engine/sql_learned_rank.py` | Optional dependency-free inference for frozen ranker artifacts. |

The capability modules do not inherit from or import private details from one
another or from the search orchestrator. Shared contracts live in `sql_schema.py`
and `sql_candidate.py`, shared expansion behavior lives in `sql_expansion.py`, and
`SQLSearcher.search` is the only place that orders the stages.

## Quick start

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
foreign_keys = [
    {
        "from_table": "orders",
        "from_col": "customer_id",
        "to_table": "customers",
        "to_col": "id",
        "conf": 1.0,
    }
]

searcher = SQLSearcher.from_tables(tables, foreign_keys)
candidates = searcher.search("list each customer name and total order amount")

best = candidates[0]
print(best.sql)
print(best.query)       # typed AST
print(best.evidence)    # generation and ranking trace
print(best.features)    # numeric rank features
```

`TableQuery.search_ast(...)` exposes the same planner through the existing ingestion
and encoder path. It can supply role-specific semantic similarities from the frozen
encoder without changing the SQL grammar.

## Supported SQL

The AST and search pipeline support:

- one or many projected columns and `DISTINCT`;
- `COUNT`, `SUM`, `AVG`, `MIN`, and `MAX`, including multiple aggregates;
- typed comparisons, numeric ranges, categorical values, dates, `AND`, and `OR`;
- `GROUP BY`, aggregate constraints in `HAVING`, ordering, and limits;
- deterministic direct and multi-hop joins, including bridge tables;
- aliases and self-joins;
- scalar subqueries, `IN`, `NOT IN`, `EXISTS`, and `NOT EXISTS` ASTs;
- derived tables and nested aggregation;
- `UNION`, `INTERSECT`, and `EXCEPT` with equal-arity validation;
- row and frequency argmax/argmin and explicit top-N queries.

Support means the grammar, validator, renderer, and at least one search rule exist.
It does not mean every natural-language paraphrase is linked to that structure.
`EXISTS`, for example, is fully represented and tested, but Spider dev has no gold
`EXISTS` example and the searcher usually favors more common membership forms.

## Safety and validation

SQL is rendered only after the complete recursive tree passes validation. Validation
checks:

- visible table qualifiers and alias scope;
- connected joins and valid aliased self-joins;
- scalar and set-query arity;
- aggregate operand types;
- grouped projection and ordering rules;
- restrictions on compound-query operands.

Identifiers and literals are quoted by the renderer. The planner emits query ASTs,
not arbitrary statements. The serving path still applies its existing SELECT-only
guard before execution.

## Ranking

The default ranker is deterministic and inspectable. It scores role alignment for
projection, filtering, counting, aggregate operands, grouping, ordering, distinctness,
and requested identifiers. Each contribution is attached as `rank:*` evidence.

A caller may execute a bounded candidate prefix with `execute_and_rerank`. Execution
errors, empty results, null-only results, and result shape add bounded `exec:*`
features. Execution informs ordering; it never mutates or regenerates a query.

The optional frozen learned ranker is loaded explicitly:

```python
from engine.sql import SQLSearcher, load_ranker_model

model = load_ranker_model("engine/data/sql_ranker.json")
candidates = searcher.search(question, rank_model=model)
```

The current artifact is experimental and is not loaded automatically. It improved
database-disjoint training validation from 41.309% to 41.836% strict top-1, but
regressed untouched Spider dev from 40.2% to 39.5%. The promotion gate therefore
keeps the hand-ranked planner as the default.

## Evaluation stages

The old phase names remain only as stable ablation controls and evidence labels.
They are useful for measuring where accuracy comes from; they are not separate
runtime architectures.

| Stage | Adds |
|---|---|
| Phase 1 | Typed base AST, projections, filters, aggregates, joins, grouping, ordering, limits. |
| Phase 2 | Hand semantic ranking and execution checks. |
| Phase 3 | Recursive queries, set operations, aliases, self-joins, nested aggregation. |
| Phase 4 | Aggregate constraints, disjunctions, scalar selectors, membership rules. |
| Phase 5 | Row/frequency extrema, top-N, and set difference. This is the default. |
| Phase 6 | Optional frozen learned reranker. Experimental and not promoted. |

For controlled ablations:

```python
phase1 = searcher.search(q, phase2=False, phase3=False, phase4=False, phase5=False)
phase2 = searcher.search(q, phase2=True, phase3=False, phase4=False, phase5=False)
phase3 = searcher.search(q, phase2=True, phase3=True, phase4=False, phase5=False)
phase4 = searcher.search(q, phase2=True, phase3=True, phase4=True, phase5=False)
phase5 = searcher.search(q)  # all deterministic capability expanders enabled
```

## Spider status

The fast evaluator uses Spider-declared foreign keys, gold table selection, no encoder
signals, and no compose route. Gold SQL and denotations are used only for measurement.
On all 1,034 Spider dev examples with pool 180 and top-10 evaluation:

| Metric | P1 | P2 | P3 | P4 | P5 default | P6 experimental |
|---|---:|---:|---:|---:|---:|---:|
| Lenient | 33.1% | 36.8% | 38.2% | 42.5% | 47.2% | 45.9% |
| Strict | 19.9% | 24.4% | 26.4% | 32.9% | 40.2% | 39.5% |
| Scalar | 43.7% | 50.3% | 51.4% | 55.8% | 62.2% | 62.2% |
| Top-10 strict oracle | 27.5% | 27.9% | 30.0% | 38.2% | 45.8% | 45.6% |

The main unsolved limit is candidate recall and semantic binding, not randomness:
only 45.8% of examples have a strict-correct query in the top ten. Better ranking
cannot recover a query the bounded grammar rules did not generate. Whole-database
table selection is also harder than the oracle `gold_tables` configuration above.

## Reproducing results

Fetch the benchmark data:

```bash
python spider/probe/fetch_data.py --include-train
```

Run deterministic stage evaluation:

```bash
python spider/probe/ast_eval.py --dbs spider/data/dbs --pool 180 --top-k 10
```

Train on Spider train with database-disjoint validation, then evaluate on untouched
dev:

```bash
python spider/probe/train_ast_ranker.py --dbs spider/data/dbs \
  --pool 180 --negative-pool 32 \
  --cache spider/data/ranker_train_gold_180.jsonl \
  --out engine/data/sql_ranker.json

python spider/probe/ast_eval.py --dbs spider/data/dbs \
  --pool 180 --top-k 10 \
  --ranker-model engine/data/sql_ranker.json \
  --out spider/results/ast_eval_phase6.json
```

`train_ast_ranker.py` refuses `dev.json` unless the diagnostic-only override is
explicitly supplied. Training uses scikit-learn; frozen tree inference uses only the
Python standard library.

Run the integrated encoder and compose-route harness with:

```bash
python spider/probe/full_eval.py --dbs spider/data/dbs \
  --config gold_tables --planner ast --tag ast
```

## Tests

The hermetic AST suite executes generated SQL against in-memory SQLite:

```bash
python -m tests.test_sql_ast
```

It covers typing and grouping rejection, projection and filter isolation, multiple
aggregates, direct and bridge joins, deterministic ordering, execution reranking,
subqueries, set operations, aliases, self-joins, nested aggregation, `HAVING`,
disjunction scope, inferred high-confidence joins, extrema, top-N, set difference,
ranker artifact round trips, and opt-in learned-ranker isolation.

## Current boundary

The implementation is coherent and tested, but Spider is not solved. Phase 1 through
Phase 5 are complete as engineering capabilities. Phase 6 is complete as a training,
serialization, and deterministic-inference experiment, but its current artifact is
not accurate enough to promote. Future accuracy work should target measured
candidate-recall and schema-binding failures before adding more ranking complexity.
