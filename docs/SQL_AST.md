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
There is no decoder entropy to average away: remaining errors come from schema linking,
missing candidate shapes, bounded search, and ranking the wrong valid interpretation.

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
| `spider/probe/ast_profile.py` | Shared structural and role-aware profiles for gold SQL and planner ASTs. |
| `spider/probe/mine_ast_failures.py` | Full-pool recall, linking, and composition failure analysis. |
| `spider/probe/build_ast_proposal_data.py` | Database-disjoint sketch/link/literal supervision builder. |

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
- row and frequency argmax/argmin, zero-inclusive relationship counts, dual extrema,
  and explicit top-N queries.

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
- aggregate operand and literal payload types;
- grouped projection and ordering rules;
- restrictions on compound-query operands, including indeterminate `SELECT *` arity;
- invalid aggregate forms such as `COUNT(DISTINCT *)`.

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

The current artifact is experimental and is not loaded automatically. On the corrected
Spider dev evaluation it moves scalar accuracy from 56.9% to 57.4%, but regresses
lenient accuracy from 49.8% to 48.2% and strict accuracy from 39.5% to 38.5%.
The promotion gate therefore keeps the hand-ranked planner as the default.

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

The fast evaluator uses Spider-declared foreign keys, recursively referenced gold table
selection, no encoder signals, and no compose route. Gold SQL and denotations are used
only for measurement. Gold and predicted SQL execute against the same capped in-memory
tables. Every example is accounted for as answered, no-candidate, candidate-execution
failure, or gold-execution failure.

On all 1,034 Spider dev examples with pool 180 and top-10 evaluation:

| Metric | P1 | P2 | P3 | P4 | P5 default | P6 experimental |
|---|---:|---:|---:|---:|---:|---:|
| Lenient | 34.1% | 38.0% | 40.7% | 44.8% | 49.8% | 48.2% |
| Strict | 17.8% | 22.1% | 25.3% | 31.7% | 39.5% | 38.5% |
| Scalar | 38.2% | 44.4% | 45.6% | 50.5% | 56.9% | 57.4% |
| Top-10 strict oracle | 24.4% | 24.6% | 28.0% | 35.7% | 44.5% | 44.6% |

The main unsolved limit is candidate recall and semantic binding, not randomness:
only 44.5% of examples have a strict-correct Phase 5 query in the top ten, and searching
the entire returned pool raises that oracle to just 45.0%. A perfect selector can recover
at most 5.5 strict points from the current pool. The rest requires better candidate recall
and schema binding. Whole-database table selection is also harder than the oracle
`gold_tables` configuration above.

The encoder-integrated `full_eval.py` path, which also applies live-style compose routing,
scores 46.6% lenient, 35.4% strict, and 56.6% scalar (`231/408`), with 95.5% of examples
answered. It currently trails the model-free hand-ranked planner by 3.2 lenient and 4.1
strict points. Encoder signals and routing therefore need calibration; they are not yet
the source of an accuracy gain.

### Failure-mining result

`mine_ast_failures.py` profiles the recursive Spider gold tree and every Phase 5 typed
AST without parsing rendered SQL. It then executes the full candidate pool and separates
ranking, sketch recall, table/column links, composition, and residual value errors.

The disjoint full-pool outcome on dev is:

| Outcome | n | % |
|---|---:|---:|
| Strict-correct at rank 1 | 408 | 39.5% |
| Strict-correct elsewhere in pool | 57 | 5.5% |
| Lenient-only, no strict candidate | 180 | 17.4% |
| Neither metric matches | 343 | 33.2% |
| No candidate | 46 | 4.4% |

Strict and lenient are not nested metrics here. Thirty-three examples have empty gold
results: strict equality can correctly match two empty multisets, while lenient
containment requires at least one gold value. Across the pool there are 465 strict hits,
612 lenient hits, 432 hits under both metrics, 33 strict-only hits, 180 lenient-only hits,
and 389 matching neither. Do not derive a lenient-only count by subtracting strict from
lenient.

Of the 569 examples with no strict-correct candidate, the profiler attributes 302 to no
matching structural sketch, 180 to lenient over-answers, 46 to an empty candidate pool,
28 to missing column-role links, 12 to value or residual semantic mismatches, and one to
failure to combine individually available choices. These labels compare counted profiles,
so they are diagnostic categories rather than formal SQL-equivalence proofs. The dominant
missing or extra features are projection width, filters, joins, `DISTINCT`, grouping, and
nested blocks. The pool cap is not binding: the planner returns only 5.6 candidates on
average and two at the median.

The compact report is `spider/results/ast_failure_analysis.json`. Per-example detail is
written to the gitignored `ast_failure_analysis_per_example.json` for local inspection.

### Training-data decision

The old Phase 6 data is ranker data, not proposal data. Its 6,997 generated Spider-train
groups contain a strict-positive candidate for only 3,135 examples (44.8%). There is no
positive for 3,862 examples (55.2%), and only 1,791 mixed positive/negative groups with
13,323 candidate rows can contribute to the ranking loss. Adding more rows in that format
cannot teach the grammar to create a missing AST.

`build_ast_proposal_data.py` now converts all 7,000 Spider-train examples into direct gold
supervision for:

- counted recursive SQL sketches and operators;
- referenced tables and role-specific projection, aggregate, filter, group, order, having,
  and join columns;
- typed predicate literals and limits bound to their operator and column;
- schema names, types, primary keys, and foreign keys, without copying database cell dumps.

The split is database-disjoint: 5,669 examples from 112 databases train, and 1,331 examples
from 28 unseen databases validate. There are 764 distinct train sketches; 148 validation
examples use a sketch not observed exactly in train, so compositional generalization remains
measurable. The manifest is `spider/results/ast_proposal_data.json`; generated JSONL files
live under gitignored `spider/data/`.

The next model should predict a small beam of sketch and schema-link proposals, then let the
existing typed AST builder, validator, renderer, and execution checks constrain them. Start
with the existing Qwen2.5-0.5B backbone under this correct objective. Then run an otherwise
identical Qwen2.5-1.5B ablation. A larger backbone is promoted only if it materially improves
database-disjoint sketch/link recall and final strict pool recall after accounting for memory
and latency. Swapping the current encoder before this baseline would confound model capacity
with the much larger effect of changing the supervision target.

## Reproducing results

Fetch the benchmark data:

```bash
python spider/probe/fetch_data.py --include-train
```

Run deterministic stage evaluation:

```bash
python spider/probe/ast_eval.py --dbs spider/data/dbs --pool 180 --top-k 10
```

Mine the current pool and build proposal supervision:

```bash
python spider/probe/mine_ast_failures.py --dbs spider/data/dbs --pool 180 --top-k 10
python spider/probe/build_ast_proposal_data.py
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

Its 62 cases cover typing and grouping rejection, projection and filter isolation,
multiple aggregates, direct and bridge joins, deterministic ordering, execution
reranking, subqueries, set operations, aliases, self-joins, nested aggregation,
`HAVING`, disjunction scope, inferred high-confidence joins, extrema, zero-inclusive
counts, top-N, set difference, ranker artifact round trips, evaluator accounting,
failure-profile alignment, database-disjoint proposal splits, literal targets, and
opt-in learned-ranker isolation.

## Current boundary

The implementation is coherent and tested, but Spider is not solved. Phase 1 through
Phase 5 are complete as engineering capabilities. Phase 6 is complete as a training,
serialization, and deterministic-inference experiment, but its current artifact is
not accurate enough to promote. Future accuracy work should target measured
candidate-recall and schema-binding failures before adding more ranking complexity. The
failure miner and proposal corpus are complete; training and integrating the proposal model
is the next implementation phase.
