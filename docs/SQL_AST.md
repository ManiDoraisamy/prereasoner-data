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

The same inputs always produce the same candidate **ordering** — the planner's selection is
deterministic, which removes sampling variance. It does not remove natural-language ambiguity,
schema-linking errors, missing search rules, or ranking errors.

> **Caveat — end-to-end byte-identity is not guaranteed.** Deterministic here means the AST
> planner's *candidate selection* is reproducible. It is **not** a claim that end-to-end serving
> emits byte-identical SQL across processes: the compose path shows cross-process SQL variance (an
> external review observed a few differing compose SQLs across two runs under identical model
> hashes; likely compose-path ordering that would need controlled torch/thread settings to pin).
> Treat byte-for-byte cross-process reproduction as a known caveat, not a guarantee.

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
variant still passes AST validation and is ranked by the same hand-written features. It is a
kept, purely deterministic knob; serving leaves it off. It is not tied to any reproducible
accuracy gain in the current tree — do not attribute a specific pool-recall number to it.

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

The reported numbers are the **serving-faithful** measurement: `full_eval.py` running the exact
serving selector (byte-for-byte `engine/tables.py:_serve_ast`) — top-1, `--max-candidates 25` — over
all 1,034 Spider dev examples with denotation evaluation. This accuracy is entirely the deterministic
planner's; no trained proposer or learned ranker is involved.

| Configuration | Strict | Lenient | Scalar-gold |
|---|---:|---:|---:|
| **whole_db** — gold-blind, all DB tables fed (standard Spider) | 313/1034 (30.3%) | 402/1034 (38.9%) | 204/408 (50.0%) |
| **gold_tables** — oracle table selection, only the gold-referenced tables fed | 389/1034 (37.6%) | 509/1034 (49.2%) | 235/408 (57.6%) |

The **whole_db** row is the number to compare against other Spider systems: it is gold-blind and
feeds every table in the database, so it also pays the cost of table selection. The **gold_tables**
row is an oracle-table-selection configuration that feeds only the tables the gold SQL references;
this is the closer analogue to the product, where a user uploads exactly the relevant sheets, and it
is an upper bound relative to standard Spider, not a standard-Spider result.

> **Historical note.** Earlier "profile-expansion / pool-recall" experiments (a "pool 180" candidate
> pool reaching ~55% strict pool-oracle) depended on a trained research **proposer** that has since
> been removed from the tree, along with `build_ast_proposal_data.py`. Those pool numbers were **not**
> produced by the deterministic serving path and **cannot be reproduced from HEAD**; they are recorded
> here only as history and are deliberately excluded from the table above.

## Reproduction

Fetch Spider data:

```bash
python spider/probe/fetch_data.py --include-train
```

Run the serving-faithful evaluation — the deterministic AST planner, byte-for-byte the
serving selector (`engine/tables.py:_serve_ast`). The planner is unconditional: there is no
planner-mode flag, no proposer, and no learned ranker to pass.

Standard Spider (whole_db — the headline comparison number):

```bash
python spider/probe/full_eval.py \
  --dbs spider/data/dbs --config whole_db \
  --selection serving_top1 --max-candidates 25 \
  --tag serving_whole_db \
  --out spider/results/full_eval_serving_whole_db
```

Oracle table selection (gold_tables — the product-analogue upper bound): rerun the same
command with `--config gold_tables`.

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
