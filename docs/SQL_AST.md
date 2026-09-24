# Own-Data SQL Planner

This is the own-data SQL planner: the path from a question over uploaded tables to executed SQL.
Every query it can run is a validated typed abstract syntax tree (AST). Candidates come from two
sources: a bounded deterministic search over typed ASTs, and a frozen SQL proposer model whose
decoded lines are accepted only if they import into the same typed AST and validate. An arbiter
(a fitted linear score over nine named features) chooses among the candidates that execute. No
step samples: the search is rule-based, the proposer uses deterministic beam search, and the
arbiter is arithmetic with a stable tie-break.

## What it does

Given a question, tables, and foreign keys, the planner:

1. Builds a typed schema graph.
2. Links question roles to tables, columns, operators, and values.
3. Constructs, validates and ranks search candidates with named deterministic features.
4. Expands recursive queries, constraints, extrema, and set operations when applicable.
5. Adds the proposer's beams that import into the typed AST and validate.
6. Runs every pooled query on an in-memory copy of the tables; a query that fails is ineligible.
7. Scores each runnable query with the arbiter and serves the best (calculation intents and the
   decomposition leaf contract constrain that ranking).
8. Renders only validated ASTs to SQL.

For every direct or named request in the supported dual subset, the winner lowers into
one immutable `AnalysisPlan`. SQL and readable SQLAlchemy/Python are emitted independently from that
plan; neither source is parsed to create the other. See
[DETERMINISTIC_EMITTERS.md](DETERMINISTIC_EMITTERS.md).

When a named request is compound — the search reads the question as a set operation — the engine
requests one bounded decomposition retry instead of running a query that answers a fragment. A
proposer beam never makes a question compound: a named request that is not compound is served by the
best-ranked single query. A conversational model proposes only natural-language leaf questions and a closed
`cross`/`anti_join` topology. The same planner selects every leaf, under the leaf contract;
`engine/decomposition.py` fuses their typed outputs into one DAG. No text from the conversational
model becomes SQL, Python, an identifier, a join key, or an intermediate result.

AST validity is broader than dual-emitter coverage. The lowering adapter requires proven ORM row
identities and scalar join targets. Unsupported ASTs retain SQL execution under the default policy
or `use=sql`; `use=py` and `use=both` cannot accept those fallback results. Stage parity is a backend
equivalence test and does not replace tests against the independently expected meaning of a question.

Foreign keys may contain one or several ordered column pairs. A composite key remains one
logical graph edge and one `Join`; rendering produces an atomic conjunction such as
`ON child.country = parent.country AND child.postal = parent.postal`. The validator rejects
any component that does not connect the new table to the existing join graph.

The same inputs produce the same selection. That removes sampling variance, not natural-language
ambiguity, schema-linking errors, missing search rules, or selection errors.

## Architecture

The runtime path (`TableQuery.select_query`, engine/tables.py) is:

```text
question + tables + foreign keys
          |
          v
      SchemaGraph ------------------------------+
          |                                     |
          v                                     v
  typed bounded AST search             SQL proposer (Qwen2.5-0.5B + LoRA)
  + named deterministic ranking        4 deterministic beams, first line each
          |                                     |
          |                            import -> validate -> re-render
          |                                     |
          +-------------> merged pool <---------+
                          (search order, then beams; SQL both found is one
                           member tagged proposer:endorsed)
                               |
                               v
             run each member on an in-memory SQLite copy
             (SELECT guard, fixed VM-step budget); failures are ineligible,
             and so are text literals bound to a column that never
             holds them while another column does (sql_grounding.py)
                               |
                               v
             proposer likelihood of each runnable member
                               |
                               v
             arbiter: linear score over 9 named features,
             best score wins, earlier pool position breaks ties
                               |
                               v
                  validated AST -> SQL (or SQL + Python)
```

The search's construction and ranking rules are hand-written; the frozen encoder supplies role and
schema similarities to them. The proposer is a small causal language model fine-tuned on Spider
TRAIN gold SQL that the importer could map into the typed AST; its raw text never reaches a database
(anything the importer cannot map, the validator rejects, or the renderer cannot reproduce is
dropped). The arbiter is a standardized logistic regression, so each selection is explainable as
feature contributions (`PoolSelection.record`).

| Arbiter feature | Meaning |
|---|---|
| `likelihood` | Teacher-forced log-probability of the SQL under the proposer's prompt |
| `likelihood_tokens` | SQL length in proposer tokens |
| `likelihood_per_token` | `likelihood / likelihood_tokens` |
| `pool_score` | Search score, or the proposal's penalized score (below every search candidate) |
| `pool_rank` | Position in the merged pool |
| `from_proposer` | The proposer produced this SQL (alone or with the search) |
| `from_search` | The deterministic search produced this SQL |
| `endorsed` | Both produced it |
| `pool_size` | Number of pooled candidates |

## Public API

### Serving entry point

Live serving goes through `engine/tables.py`. `TableQuery.serve(tables, question)` runs the
full own-data pipeline (ingest → schema → `_serve_ast` → guard → execute) and returns the
answer plus the winning candidate. `_serve_ast` calls `select_query`, the one own-data selection
also used by decomposition leaves, the Spider evaluator, the offline regression gate and arbiter
training. The decomposition probe reads only its first stage, `search_pool`:

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

engine = EncoderQuery()          # loads the runtime bundle: encoder, SQL proposer, arbiter
result = engine.serve(tables, "list each customer name and total order amount")
print(result["sql"])
print(result["selection"])       # origin, arbiter score and per-feature contributions
```

To see the whole decision, call `select_query` on normalized tables:

```python
norm, fks = engine.ingest(tables)
sch, colidx, tablemap = engine.schema(norm, fks)
selection = engine.select_query("list each customer name and total order amount",
                                norm, fks, sch, tablemap)
for index in selection.ranking:                 # runnable members, best first
    print(selection.scores[index], selection.origin(index), selection.pool[index].sql)
```

### Direct AST search

To call only the deterministic search, build the typed schema and foreign keys and call
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

Trusted internal callers may pass tuple edges to `ingest(tables, explicit_fks=...)` using
`from_cols` and `to_cols`. This parameter is an internal planner boundary; serving does not
read foreign keys from uploaded table dictionaries.

`search_ast` builds a `SchemaGraph` (`engine/sql_search.py: SchemaGraph.from_planner`), runs
`SQLSearcher(graph, ...).search(...)`, and returns ranked, validated candidates. The search uses
the encoder's similarities but no generative model.

The own-data `/api/knowledge` response keeps its existing SQL and result fields and adds a
`planner` object with `ast`, `candidate_count`, `evidence`, `features`, and `selection` (pool
size, how many ran, the winner's origin, score, feature values and contributions).

## Deterministic candidate expansion

`SQLSearcher.search` accepts an optional `profile_config` (`ProfileSearchConfig`,
`engine/sql_profile_expansion.py`) that turns on **deterministic** exact-profile candidate
expansion — an extra search knob that widens the candidate pool from structural AST profiles
(`engine/sql_profile.py`). The expansion algorithm is deterministic and may consume profiles
supplied through semantic signals; every variant still passes AST validation and is ranked by
the same named features. Serving leaves this optional knob off. It is not tied to any reproducible
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
- typed `+`, `-`, `*`, and real-valued `/` expressions, including aggregates over expressions;
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

Arithmetic is handled by `engine/calculations/`, not by SQL-string templates. A specification
detects explicit syntax, binds role-named numeric columns, and proposes a `BinaryExpr` tree. The
shared expander obtains complete join trees from `SchemaGraph`; composite keys remain one typed
foreign-key edge. The post-ranking verifier describes the selected AST and requires the expected
expression plus a complete registered-key path between its bound operands on every `UNION`,
`INTERSECT`, or `EXCEPT` branch before a number can be released.

Registered forms are `SUM(amount * rate_to_<target>)` for direct currency conversion,
`SUM(numerator) / SUM(denominator)` for ratios and per-capita measures, and
`SUM(amount * rate)` for flat tax/commission or explicit annual one-year simple interest. Percent
columns are divided by 100; fraction/decimal columns are not. A temporal rate table is eligible only
when the typed foreign key includes its temporal coordinate. Piecewise tax schedules, unspecified
interest periods, and latest-prior/as-of joins are not silently approximated; they clarify. Currency
also retains its filter, identity, and stated-unit realizations. ISO parsing and the canonical
`rate_to_<code>` convention live in `engine/currency_intent.py`.

Qwen similarities can order eligible operand bindings. They do not create intent by themselves,
change unit normalization, supply a missing key, or satisfy verification. This is why retraining can
improve recognition without moving arithmetic correctness into a stochastic decoder.

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
| `engine/sql_parsimony.py` | Bounded projection/table variants of pooled candidates (minimal join, binding, drop/add column, operand swap, DISTINCT). |
| `engine/sql_rank.py` | Hand-written search ranking features; the pool merge and the linear arbiter (`SQLArbiter`, `PoolSelection`). |
| `engine/sql_proposer.py` | The frozen SQL proposer: deterministic beams and teacher-forced likelihoods. |
| `engine/sql_prompt.py` | The one proposer prompt, shared with training. |
| `engine/sql_import.py` | SQL text to typed AST; the gate every proposal passes. |
| `engine/calculations/core.py` | Typed plans and branch-preserving computation evidence. |
| `engine/calculations/specifications.py` | Registered currency, ratio, and rate-application semantics. |
| `engine/calculations/search.py` | Calculation-plan expansion into validated AST candidates. |
| `engine/calculations/registry.py` | Shared selection, verification, ranking features, and clarify policy. |
| `engine/currency_intent.py` | Currency syntax and canonical rate-column rules used by the currency specification. |
| `engine/sql_profile.py` | Structural AST profiles. |
| `engine/sql_profile_expansion.py` | Deterministic exact-profile candidate expansion (`ProfileSearchConfig`). |
| `engine/deterministic/plan.py` | Immutable topologically ordered plan shared by both source emitters. |
| `engine/deterministic/lower.py` | Strict lowering from the supported typed-AST subset; unsupported shapes remain on SQL. |
| `engine/deterministic/emitter/` | Deterministic SQL view-stack and readable SQLAlchemy/Python source emitters. |
| `engine/deterministic/runtime.py` | In-memory Python loading, execution policy, and SQL/Python parity checking. |
| `engine/decomposition.py` | Closed compound proposal validation and typed leaf-plan fusion. |

`SQLSearcher.search` remains the low-level ablation boundary. The capability modules do
not depend on one another's private internals.

## Spider results

[`spider/results/RESULTS.md`](../spider/results/RESULTS.md) is the single authoritative benchmark
record, including exact configuration, source commit, artifact hashes, and dirty-worktree state.
Do not copy changing accuracy figures into architecture documentation. The standard comparison is
the serving-faithful `whole_db` run: all database tables, the served selection, and no gold table
hints. `gold_tables` is an oracle table-selection diagnostic, not a standard Spider result.

The measured accuracy belongs to the whole served pipeline: search, proposer, pool execution and
arbiter, loaded from the same runtime bundle the service loads.

## Reproduction

Fetch Spider data:

```bash
python spider/probe/fetch_data.py --include-train
```

Run the serving-faithful evaluation. It calls `TableQuery.select_query`, the serving selection,
with the runtime bundle in `engine/data` (fetch it with `python -m engine.fetch_weights`); there is
no planner-mode flag and no model to pass.

Standard Spider (whole_db — the headline comparison number):

```bash
python -m spider.probe.full_eval \
  --dbs spider/data/dbs --config whole_db \
  --tag serving_whole_db
```

Oracle table selection (gold_tables — the product-analogue upper bound): rerun the same
command with `--config gold_tables`. `--selection pool_oracle` additionally executes the whole
pool and scores each question by its best member: the ceiling any selection change could reach.
Model training and promotion are covered in [`docs/TRAINING.md`](TRAINING.md).

The same evaluator can execute a lowerable selected AST through the readable Python emitter
without changing candidate generation, ranking, gold execution, or comparison:

```bash
python -m spider.probe.full_eval \
  --dbs spider/data/dbs --config whole_db \
  --backend auto --python-row-limit 10000 --scalar-only \
  --tag serving_python_scalar
```

`auto` mirrors the production threshold and retains SQL fallback; the evaluator also records strict
selected-SQL/Python equality without changing the Python result. `python` fails unsupported candidates
so coverage is visible. `verify` requires strict SQL/Python denotation equality. All modes reuse `spider.probe.spider_eval.compare`; the
Python fields extend the existing scalar-gold report rather than defining another accuracy metric.

## Tests

```bash
python -m tests.test_sql_ast
```

The hermetic tests execute generated SQL against in-memory SQLite and cover AST typing,
rendering, joins, recursion, constraints, extrema, profiles, deterministic candidate
expansion, deterministic ordering, the proposal import gate, pool merging and arbitration. They
replace only the proposer's two model calls, so they need no weights.

Run the repository aggregate suite with:

```bash
python -m tests.run_all
```

## What remains

The planner is deterministic and measured, but Spider is not solved. The next work should be
measured against the current bottlenecks (see RESULTS.md for the numbers):

1. Selection: the served pool contains a correct query for many questions the arbiter misses;
   a better-calibrated arbiter converts that pool recall into answers.
2. Coverage: add search rules and importer mappings for questions with no correct pool member.
3. Latency: the proposer's beam search dominates request time on CPU.
4. Treat larger models as controlled capacity experiments after objective and data changes.

Search controls and evidence use functional names (`recursive`, `constraint`, and `extrema`) rather
than historical implementation phases; these capabilities are one planner, not separate architectures.
