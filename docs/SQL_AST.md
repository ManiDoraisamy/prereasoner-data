# Deterministic SQL AST Search

`engine/sql_ast.py`, `engine/sql_search.py`, `engine/sql_rank.py`, and the `engine/sql_phase*.py` expanders
implement the deterministic text-to-SQL planner. The planner
constructs typed SQL objects and renders SQL only after a complete candidate passes structural validation.
It does not decode SQL tokens.

## Phase 1 envelope

The AST and searcher currently support:

- multiple projected columns;
- `DISTINCT` projections;
- `COUNT`, `SUM`, `AVG`, `MIN`, and `MAX`, including several different aggregates in one query;
- equality, inequality, numeric range, categorical value, and year predicates combined with `AND`;
- `GROUP BY`, aggregate ordering, ordinary ordering, and `LIMIT`;
- deterministic search over direct and multi-hop foreign-key join trees;
- typed validation before SQL rendering;
- deterministic candidate scores and evidence records.

Phase 1 generation contains one `SELECT` block. The shared AST now also represents the recursive Phase 3
grammar described below; `phase3=False, phase4=False, phase5=False` keeps the original generation envelope
unchanged for evaluation.

## Phase 2 ranking

Phase 2 reranks the Phase 1 pool without changing its SQL grammar:

- question-role features distinguish projections, counted entities, aggregate operands, grouping keys,
  filters, and ordering columns;
- repeated aggregate operands are coordinated, so `AVG(age), MAX(age)` outranks `AVG(age), MAX(type)`;
- count questions penalize accidental grouping, while `each`/`per` questions require aligned grouping;
- `distinct` count and explicit `id instead of name` semantics are scored directly;
- role-specific question phrases and schema columns are compared in the existing encoder metric space;
- every score contribution is attached to the candidate trace as `rank:*` evidence;
- the five highest semantic candidates are executed, with errors, empty results, null-only results, and result
  shape contributing bounded `exec:*` evidence.

Fixed inputs and model weights produce the same candidate order. Execution is evidence for ranking, not a
search loop that mutates the query.

## Phase 3 recursive search

Phase 3 expands the ranked pool with recursive, scope-aware SQL structures:

- `SetQuery` models `UNION`, `INTERSECT`, and `EXCEPT` with equal-arity validation;
- `ScalarSubquery`, `InPredicate`, and `ExistsPredicate` model scalar, membership, and correlated subqueries;
- `SubquerySource` supports derived tables and nested aggregation;
- source and join aliases have independent SQL scopes, enabling deterministic self-joins;
- recursive validation checks visible qualifiers, connected aliased joins, scalar/subquery arity, aggregate
  types, grouping, set operands, and compound-query restrictions before rendering;
- high-confidence Spider expansions split contradictory same-column filters into set branches, construct
  anti-membership queries, rewrite local comparisons against aggregate subqueries, select entities through
  superlative scalar subqueries, build nested count aggregates, and expand repeated-FK route/relationship
  self-joins.

`EXISTS` is represented, validated, rendered, and covered by execution tests. Spider dev contains no gold
`EXISTS` query, so Phase 3 currently prefers the much more common `IN`/set forms during synthesis.

## Phase 4 constraint search

Phase 4 expands validated Phase 3 candidates with bounded rules for the highest-mass remaining Spider
structures:

- count and aggregate constraints become typed `GROUP BY ... HAVING` trees rather than row predicates;
- categorical and numeric alternatives become nested `OR` expressions while shared predicates remain
  conjunctive;
- relation-filtered entity projections use `DISTINCT` when the join can duplicate the entity;
- `AVG`, `MIN`, and `MAX` comparisons become scalar subqueries while explicit grouped aggregates stay grouped;
- scalar row and frequency selectors model comparisons against superlative rows and least/most-common values;
- positive, negative, and compound membership forms use typed `IN`/`NOT IN` subqueries;
- when Spider omits an entity FK, a Phase 4 fallback may add one join only when observed child values are a
  near-subset of a unique parent key and the relation role matches the entity name.

Every expansion is immutable, recursively validated, deterministically rendered, deduplicated by SQL, and
bounded by the configured candidate pool.

## Phase 5 extrema search

Phase 5 targets deterministic ordering and set operations that remain outside the earlier envelopes:

- row argmax/argmin maps `youngest`, `oldest`, `highest`, `lowest`, and related cues to typed order columns;
- explicit result cardinality produces bounded top-N candidates rather than an unbounded sort;
- filters and joins are preserved while scalar-extrema predicates are converted to `ORDER BY ... LIMIT` when
  the question asks for one row;
- dirty Spider measure columns can be ordered when their observed values are overwhelmingly numeric even if
  schema inference classified the column as text;
- frequency argmax/argmin emits `GROUP BY ... ORDER BY COUNT(*) ... LIMIT`, optionally returning the count;
- direct join-tree search and the Phase 4 high-confidence relation fallback cover cross-table frequency cases;
- negative relation questions can become typed, equal-arity `EXCEPT` trees when both schema sides are present;
- guards keep all-row sorting, grouped aggregates, multi-aggregate MIN/MAX, nested comparative counts, and
  scalar tie-preserving extrema in their earlier representations.

## Search pipeline

1. Build a `SchemaGraph` from typed columns, observed values, and foreign keys.
2. Link question spans to candidate tables, columns, values, aggregate functions, comparisons, grouping,
   ordering, and limits.
3. Expand ambiguous semantic bindings in a bounded beam.
4. Collect every table required by each semantic draft.
5. Search the undirected foreign-key graph for connected join trees. Bridge tables can be introduced even
   when they were not directly selected.
6. Construct immutable `SelectQuery` candidates and reject type or grouping violations.
7. Expand high-confidence recursive candidates from the validated base pool.
8. Expand aggregate constraints, disjunctions, scalar selectors, and membership candidates.
9. Expand row/frequency extrema, top-N, and set-difference candidates.
10. Validate the complete recursive tree, render it, and rank the surviving SQL strings.

The same inputs produce the same candidate order. A future learned ranker may replace or augment the current
scores without introducing sampling; fixed weights and deterministic operators preserve reproducibility.

## API

For raw table dictionaries:

```python
from engine.sql_search import SQLSearcher

searcher = SQLSearcher.from_tables(tables, foreign_keys)
candidates = searcher.search("show top 5 customers by total order amount")
sql = candidates[0].sql
ast = candidates[0].query
evidence = candidates[0].evidence
```

Phase controls are explicit:

```python
phase1 = searcher.search(question, phase2=False, phase3=False, phase4=False, phase5=False)
phase2 = searcher.search(question, phase2=True, phase3=False, phase4=False, phase5=False)
phase3 = searcher.search(question, phase2=True, phase3=True, phase4=False, phase5=False)
phase4 = searcher.search(question, phase2=True, phase3=True, phase4=True, phase5=False)
phase5 = searcher.search(question, phase2=True, phase3=True, phase4=True, phase5=True)  # defaults
```

The existing `TableQuery` schema path exposes the same planner through:

```python
candidates = table_query.search_ast(question, schema, tables, foreign_keys)
```

`TableQuery.search_ast` accepts the same `phase2`, `phase3`, `phase4`, and `phase5` flags for model-backed
baseline runs.

The Spider harness keeps the original planner as its default and enables this planner explicitly:

```bash
python spider/probe/full_eval.py --dbs spider/data/dbs --config gold_tables --planner ast --tag ast
```

Spider's declared foreign keys are converted to named graph edges for this mode. Uploaded CSVs continue to
use the engine's deterministic inclusion-dependency discovery.

The fast model-free evaluator reports all five phases independently:

```bash
python spider/probe/ast_eval.py --dbs spider/data/dbs --pool 180 --top-k 10
```

On the 1,034-example Spider dev split with `gold_tables`:

| Metric | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 5 |
|---|---:|---:|---:|---:|---:|
| Lenient | 33.1% | 36.8% | 38.2% | 42.5% | 47.2% |
| Strict | 19.9% | 24.4% | 26.4% | 32.9% | 40.2% |
| Scalar | 43.7% | 50.3% | 51.4% | 55.8% | 62.2% |
| Top-10 strict oracle | 27.5% | 27.9% | 30.0% | 38.2% | 45.8% |

Phase 2's nearly fixed oracle shows a ranking gain. Phase 3 expands recursive coverage, and Phase 4 adds
aggregate constraints and disjunctions. Phase 5 adds 7.3 strict top-1 points and 7.6 strict-oracle points over
Phase 4; its full-split audit records 76 strict gains and no regressions. This evaluation excludes encoder
signals and the compose route; `full_eval.py --planner ast` exercises those integrations.

## Verification

The hermetic suite executes generated SQL against in-memory SQLite:

```bash
python -m tests.test_sql_ast
```

It covers type rejection, multi-column projection, value/range predicates, multiple aggregates, bridge-table
joins, grouped counts, aggregate Top-N, count-distinct and aggregate-role failures from Spider, encoder signal
tie-breaking, execution reranking, scalar and correlated subqueries, compound/derived queries, anti-membership,
self-joins, nested count aggregation, and repeatability.
Phase 4 coverage additionally includes HAVING constraints, disjunction scope and deduplication, scalar
aggregate filters, inferred high-confidence joins, shared predicates, grouped-superlative guards, and phase
isolation.
Phase 5 coverage includes filtered row extrema, explicit top-N, grouped frequency extrema with optional count
output, direct set difference, multi-aggregate and nested-comparison guards, and Phase 5 isolation.
