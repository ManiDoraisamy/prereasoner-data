# Deterministic SQL Planner

This is PreReasoner's own-data SQL planner — the single, deterministic path from a
question over your uploaded tables to executed SQL. The planner searches a bounded space
of valid SQL abstract syntax trees (ASTs) and ranks them with hand-written, fully
inspectable features. It does not sample SQL tokens from a decoder, and it uses no trained
proposer or learned ranker.

## What it does

Given a question, tables, and foreign keys, the planner:

1. Builds a typed schema graph.
2. Links question roles to tables, columns, operators, and values.
3. Constructs and validates candidate ASTs.
4. Expands recursive queries, constraints, extrema, and set operations when applicable.
5. Ranks candidates with hand-written, fully-inspectable deterministic features.
6. Renders only validated ASTs to SQL.

The same inputs always produce the same ordering. Determinism removes sampling variance.
It does not remove natural-language ambiguity, schema-linking errors, missing search
rules, or ranking errors.

## Architecture

The runtime path is:

```text
question + tables + foreign keys
          |
          v
      SchemaGraph
          |
          v
  typed bounded AST search
          |
          v
  deterministic semantic ranking
          |
          v
      validated SQL
```

Nothing here is trained. The ranking is hand-written and fully inspectable; there is no
trained proposer and no learned ranker. Every candidate must pass AST validation before it
can be rendered to SQL.

## Public API

### Serving entry point

Live serving goes through `engine/tables.py`. `TableQuery.serve(tables, question)` runs the
full own-data pipeline (ingest → schema → `_serve_ast` → guard → execute) and returns the
answer plus the winning candidate. `_serve_ast` calls `search_ast` and selects
`candidates[0]`:

```python
from engine.encoder_overlay import EncoderQuery

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

engine = EncoderQuery()                       # loads the one shared encoder
result = engine.serve(tables, "list each customer name and total order amount")
print(result["sql"])
```

### Direct AST search

To call the deterministic planner directly, build the typed schema and foreign keys and call
`search_ast`:

```python
from engine.encoder_overlay import EncoderQuery

engine = EncoderQuery()
norm, fks = engine.ingest(tables)
sch, colidx, tablemap = engine.schema(norm, fks)

candidates = engine.search_ast(
    "list each customer name and total order amount",
    sch, norm, fks,
    max_candidates=25,
)

print(candidates[0].sql)         # rendered SQL
print(candidates[0].query)       # typed AST
print(candidates[0].evidence)    # generation and ranking trace
print(candidates[0].features)    # numeric ranking features
```

`search_ast` builds a `SchemaGraph` (`engine/sql_search.py: SchemaGraph.from_planner`), runs
`SQLSearcher(graph, ...).search(...)`, and returns ranked, validated candidates. There is no
trained proposer and no learned ranker in this path.

The own-data `/api/knowledge` response keeps its existing SQL and result fields and adds a
`planner` object with `ast`, `candidate_count`, `evidence`, and `features`.

## Deterministic candidate expansion

`SQLSearcher.search` accepts an optional `profile_config` (`ProfileSearchConfig`,
`engine/sql_profile_expansion.py`) that turns on **deterministic** exact-profile candidate
expansion — an extra search knob that widens the candidate pool from structural AST profiles
(`engine/sql_profile.py`). It is not a proposer: no model is involved, and every expanded
variant still passes AST validation and is ranked by the same hand-written features. The
Spider eval uses it to raise pool recall; serving does not.

| Setting | Default | Meaning |
|---|---:|---|
| `max_candidates` | 32 | Maximum expanded profile candidates. |
| `per_profile` | 4 | Maximum retained bindings for one profile. |
| `generation_penalty` | 5.0 | Prior penalty applied to expanded variants. |
| `binding_quality_weight` | 2.0 | Weight for role-binding quality. |
| `preserve_baseline_top` | `True` | Keep the hand-ranked winner at the top. |

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
| `engine/sql_ast.py` | Immutable AST, validation, and rendering. |
| `engine/sql_schema.py` | Typed schema and join-path search. |
| `engine/sql_search.py` | `SQLSearcher`: base beam, capability ordering, and candidate assembly. |
| `engine/sql_candidate.py` | Scored-candidate container and evidence. |
| `engine/sql_expansion.py` | Shared AST construction helpers. |
| `engine/sql_recursive.py` | Recursive queries, sets, aliases, and self-joins. |
| `engine/sql_constraints.py` | `HAVING`, disjunction, scalar, and membership rules. |
| `engine/sql_extrema.py` | Extrema, top-N, and set difference. |
| `engine/sql_rank.py` | Hand-written semantic and execution features. |
| `engine/sql_profile.py` | Structural AST profiles. |
| `engine/sql_profile_expansion.py` | Deterministic exact-profile candidate expansion (`ProfileSearchConfig`). |

`SQLSearcher.search` remains the low-level ablation boundary. The capability modules do
not depend on one another's private internals.

## Spider results

The current full-dev measurements use all 1,034 Spider dev examples, Spider foreign
keys, recursively referenced gold tables, pool 180, and denotation evaluation.

| Configuration | Strict top-1 | Strict top-10 | Strict pool oracle | Avg. candidates |
|---|---:|---:|---:|---:|
| Phase 5 typed search | 426 (41.2%) | 484 (46.8%) | 490 (47.4%) | 5.39 |
| Explicit profile expansion, baseline protected | 426 (41.2%) | 545 (52.7%) | 570 (55.1%) | 21.31 |

The deterministic profile expansion adds 80 strict-reachable answers to the pool without
changing top-1. The current projection linker distinguishes properties that share
table-name words, qualifies ambiguous properties by entity, and follows outbound owner
foreign keys for display names. Against the previous exact serving artifact this produces
18 strict wins and one strict loss. The remaining loss is a known grouped distinct-count
target ambiguity.

The **serving-faithful** `full_eval.py --selection serving_top1 --max-candidates 25`
path (byte-for-byte `engine/tables.py:_serve_ast`) scores **389/1034 strict (37.6%)**,
**509/1034 lenient (49.2%)**, and **235/408 scalar (57.6%)** on gold-tables. This accuracy is
entirely the deterministic planner's. Turning on deterministic profile expansion at pool 180
yields the *same* top-1 numbers — it only raises pool recall (~47%→55%), which top-1 selection
does not yet convert.

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

- `spider/results/ast_profile_projection_final.json`: recorded isolated search and pool recall;
- `spider/results/ast_profile_failure_analysis_final.json`: failure-family counts;
- `spider/results/ast_profile_failure_details_final.json`: per-example failure diagnoses;
- `spider/results/full_eval_ast_projection_final/`: exact serving-selector
  summary, per-example records, and resumable checkpoint.

## Reproduction

Fetch Spider data:

```bash
python spider/probe/fetch_data.py --include-train
```

Run the serving-faithful evaluation — the deterministic AST planner, byte-for-byte the
serving selector (`engine/tables.py:_serve_ast`). There is no `--planner`, no
`--proposer-model`, no `--ranker-model`, and no `--profile-expansion`; the planner is
unconditional:

```bash
python spider/probe/full_eval.py \
  --dbs spider/data/dbs --config gold_tables \
  --selection serving_top1 --max-candidates 25 \
  --tag serving_final \
  --out spider/results/full_eval_serving_final
```

`--selection serving_top1` reproduces the live selector; `--selection execution_checks`
enables execution-based candidate checks for diagnosis. Encoder training is unchanged and
covered in [`docs/TRAINING.md`](TRAINING.md).

## Tests

```bash
python -m tests.test_sql_ast
```

The hermetic tests execute generated SQL against in-memory SQLite and cover AST typing,
rendering, joins, recursion, constraints, extrema, profiles, deterministic candidate
expansion, and deterministic ordering.

Run the repository aggregate suite with:

```bash
python -m tests.run_all
```

## What remains

The planner is coherent and deterministic, but Spider is not solved. The next work should
be measured against the current bottlenecks:

1. Close the ranking gap between selected and strict-reachable candidates — better
   hand-written ranking features that convert pool recall into top-1 selections.
2. Add search rules for examples with no strict-correct candidate in the pool.
3. Treat larger encoders as controlled capacity experiments after objective and data changes.

Historical Phase 1-6 names remain in evaluator output for ablation compatibility. They are
not separate runtime architectures and should not drive new module boundaries.
