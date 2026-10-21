# Own-Data SQL Planner

This is the own-data SQL planner: the path from a question over uploaded tables to executed SQL.
Every query it can run is a validated typed abstract syntax tree (AST). Learned encoder readers read
the question beside the schema as named readings, and a deterministic builder makes each reading a
typed AST. Selection runs the candidates, keeps the ones that are grounded in the data, and lets a
learned re-scorer order them and a learned refusal decide whether to serve the best one. No model
writes SQL, and no step samples.

## What it does

Given a question, tables, and foreign keys, the planner:

1. Builds a typed schema graph (`engine/sql_schema.py:SchemaGraph`).
2. Stage 1 (XLM-R base) scores each table's use and each column's; the six most probable tables are
   kept, and a wide table's 20 most probable columns.
3. Each stage-2 reader (XLM-R large) reads the question beside the kept tables and scores every
   reading the builder uses. Decoding keeps up to 16 of the most probable distinct readings per reader.
4. The builder (`engine/readers/readings.py:build`) makes each reading a typed AST, joining the tables
   a block uses along the foreign keys. The readers' candidates are pooled, each SQL once, most probable
   first, and the registered calculations add their expansions (`TableQuery.search_pool`).
5. Runs every pooled candidate on an in-memory copy of the tables. A query that fails, compares a
   text column with a value it never holds while another column does, or joins columns the foreign keys
   keep apart is ineligible.
6. The re-scorer (XLM-R base) orders the eligible candidates. Named dates, a SUM or AVG that reads only
   rows its joins repeat (served only when every eligible candidate does), calculation intents, and the
   decomposition leaf contract constrain that order; they never rescore it.
7. The refusal (`engine/data/readers/refusal.json`) serves the choice only when it is confident;
   otherwise the reply says how the best-ranked candidate read the question.
8. Renders only validated ASTs to SQL. A served top-1 ranking keeps every row tied with its first
   row (`sql_ast.keep_ties`, rendered with `RANK() OVER`); a larger `LIMIT` keeps exactly that many rows.

For every direct or named request in the supported dual subset, the winner lowers into
one immutable `AnalysisPlan`. SQL and readable SQLAlchemy/Python are emitted independently from that
plan; neither source is parsed to create the other. See
[DETERMINISTIC_EMITTERS.md](DETERMINISTIC_EMITTERS.md).

When a named request is compound — the readers' most probable reading is a set operation — the engine
requests one bounded decomposition retry instead of running a query that answers a fragment.
Compound structure is read from the pool before any candidate runs
(`engine/decomposition.py:compound_candidate`): a named request that is not compound is served by the
best-ranked single query. A conversational model proposes only natural-language leaf questions and a
closed `cross`/`anti_join` topology. The same selection chooses every leaf, under the leaf contract;
`engine/decomposition.py` fuses their typed outputs into one DAG. No text of the decomposition proposal
becomes SQL, Python, an identifier, a join key, or an intermediate result. A leaf with no reading the
contract accepts rejects the decomposition, and the chat model restates the leaf.

AST validity is broader than dual-emitter coverage. The lowering adapter requires proven ORM row
identities and scalar join targets. Unsupported ASTs retain SQL execution under the default policy
or `use=sql`; `use=py` and `use=both` cannot accept those fallback results. Stage parity is a backend
equivalence test and does not replace tests against the independently expected meaning of a question.

Foreign keys may contain one or several ordered column pairs. A composite key remains one
logical graph edge and one `Join`; rendering produces an atomic conjunction such as
`ON child.country = parent.country AND child.postal = parent.postal`. The validator rejects
any component that does not connect the new table to the existing join graph.

The same inputs and model files produce the same selection: the models run in fixed precision with no
sampling, ties keep pool order, and nothing depends on hash order. That removes sampling variance, not
natural-language ambiguity, schema-linking errors, or ranking errors.

## Architecture

The runtime path (`TableQuery.select_query`, engine/tables.py) is:

```text
question + tables + foreign keys
          |
          v
      SchemaGraph
          |
          v
  stage 1: schema items -> six tables, a wide table's 20 columns
          |
          v
  stage-2 readers -> up to 16 distinct readings each (decode.candidates)
  builder: each reading -> a typed AST (readings.build)
  pool: each SQL once, most probable first, + calculation expansions
          |
          v
  run each candidate on an in-memory SQLite copy
  (SELECT guard; 20 VM steps a cell of the tables it reads, at least
  10,000,000 and at most 100,000,000: execution_op_limit);
  failures are ineligible, and so are text literals bound to a column
  that never holds them while another column does, and joins that
  equate columns the foreign keys keep apart (sql_grounding.py)
          |
          v
  re-scorer orders the eligible candidates
  (named dates, a total that counts no row twice, or a
   calculation intent may prefer a later eligible one)
          |
          v
  refusal: confident -> serve        not confident -> "I read this as ..."
          |
          v
  validated AST -> SQL (or SQL + Python)
  a top-1 ranking keeps its ties (keep_ties)
```

Each candidate carries its read-as line as its evidence and its rank among its reader's readings as a
feature (`ScoredQuery.evidence`, `ScoredQuery.features`); its score is the decoder's log probability.
The pool and the re-scorer's scores depend only on the question and the data, so a request reads them
once (`ReadingSearch.pool`, `ReadingSearch.rescore`) although the decomposition probe, compose's routing
probe and the delegate each select. `PoolSelection.record()` reports the decision:

| Selection field | Meaning |
|---|---|
| `pool_size` | Candidates pooled |
| `executable` | Members that ran within the step budget |
| `misgrounded` | Members that ran but bind a literal or a join the data or the foreign keys contradict |
| `eligible` | Members that ran and are grounded |
| `selected` | Pool index of the served member, or `null` when none is eligible or the refusal declined |
| `rank` | The served member's place among the eligible members in the re-scorer's order (0 is first) |
| `score` | The served member's log probability from the decoder |
| `calculation_satisfied` | Whether a calculation intent chose it |
| `date_satisfied`, `double_counted` | Whether it keeps the dates the question names, and whether a SUM or AVG of it reads only rows its joins repeat |

### Readers and builder

| Module | Responsibility |
|---|---|
| `engine/readers/search.py` | `ReadingSearch`: schema items, pruning, the stage-2 readers' pooled candidates, re-scoring, and the refusal (`confident`) |
| `engine/readers/text.py` | The schema as each model reads it: tables, columns, types, sample values, and the question's values beside the columns that hold them (`Schema.anchors`), fitted to the token budget |
| `engine/readers/models.py` | The three model kinds (schema items, readings, re-scorer) and their inputs, shared by serving and training |
| `engine/readers/layout.py` | The readings' output layout |
| `engine/readers/decode.py` | Outputs to the most probable distinct readings, with generic semantics: a date the question names is read through `engine/sql_dates`, a column an equality pins is not shown again, and a pointed value drops the marks of the list it stood in |
| `engine/readers/readings.py` | The readings form, the builder (`build`), and its inverse (`readings_of`, used for training labels) |
| `engine/readers/read_as.py` | The plain-English "read as" line of a reading |

A reader whose `reader.json` says `{"anchored": true}` reads the question's values listed first beside
their columns; matching is on the data, not on a word list. The weights live in `engine/data/readers/`
(`stage1/`, `stage2_*/`, `rescorer/`, `refusal.json`). `training/readers/` trains them, and only
`training/readers/promote.py` installs them ([training/readers/README.md](../training/readers/README.md)).

### Refusal and the read-as line

The refusal is a logistic model over the re-scorer's probability, the decoder's log probability, the
margin to the next candidate, the rank, and whether the result is empty, with a threshold fitted on
held-out databases (`training/readers/refusal.py`). When it declines, nothing is selected and the reply
is a clarification: "I read this as “…”, but I'm not confident that is what you asked"
(`engine/tables.py:unsure_clarification`). A served answer carries `read_as`, the readers' plain reading
of the served query (`engine/readers/read_as.py`), which the thread shows as the turn's "read as" line.

## Public API

### Serving entry point

Live serving goes through `engine/tables.py`. `TableQuery.serve(tables, question)` runs the
full own-data pipeline (ingest → schema → `_serve_ast` → guard → execute) and returns the
answer plus the winning candidate. `_serve_ast` calls `select_query`, the one own-data selection
also used by decomposition leaves, the Spider evaluator, and the offline regression gate. The compose
host's probe (`engine/decomposition.search_probe`) reads its first stage, `search_pool`, to decide
whether the question is compound, and otherwise selects, so routing can ask whether the upload reads
the question whole (`engine/routing.reads_upload_whole`: the refusal accepted a reading and the
question names no world type or measure the upload lacks):

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

engine = EncoderQuery()          # the runtime bundle; the readers load on first use
result = engine.serve(tables, "list each customer name and total order amount")
print(result["sql"])
print(result["read_as"])         # how the readers read the question
print(result["selection"])       # pool counts, the served member's rank and log probability
```

To see the whole decision, call `select_query` on normalized tables:

```python
norm, fks = engine.ingest(tables)
sch, colidx, tablemap = engine.schema(norm, fks)
selection = engine.select_query("list each customer name and total order amount",
                                norm, fks, sch, tablemap)
for index in selection.ranking:                 # eligible members, in the re-scorer's order
    print(selection.pool[index].evidence[0], selection.pool[index].sql)
print(selection.selected)                       # the served pool index, or None
```

`search_pool(question, norm, fks, sch)` returns the pool alone, most probable first, before anything
runs.

Trusted internal callers may pass tuple edges to `ingest(tables, explicit_fks=...)` using
`from_cols` and `to_cols`. This parameter is an internal planner boundary; serving does not
read foreign keys from uploaded table dictionaries.

The own-data `/api/knowledge` response keeps its SQL and result fields and adds `read_as`, a
`planner` object with `ast`, `candidate_count`, `evidence`, `features`, and `selection` (the fields
above), and a `coverage` record certifying the reading. A declined question carries `clarify`,
`reason`, and the best-ranked candidate's `read_as` instead of a result.

## Supported SQL

The AST, validator, and renderer support:

- multiple projections and `DISTINCT`;
- `COUNT`, `SUM`, `AVG`, `MIN`, and `MAX`;
- typed `+`, `-`, `*`, and real-valued `/` expressions, including aggregates over expressions;
- typed comparisons, ranges, dates, categorical values, `AND`, and `OR`;
- substring filters, including `LOWER(column) LIKE '%text%'` (`Lower`);
- `DatePart('month')` and `DatePart('year_month')` of a date column, compared or grouped by;
- `DateSpan`, the days from a row's start date to its end date (or to an ISO date), over a day, week,
  month or year (`engine/sql_durations.py`, used by the world routes);
- grouping, `HAVING`, ordering, and limits;
- direct and multi-hop foreign-key joins;
- aliases and self-joins;
- scalar subqueries, `IN`, `NOT IN`, `EXISTS`, and `NOT EXISTS`;
- derived tables and nested aggregation;
- `UNION`, `INTERSECT`, and `EXCEPT`;
- a served top 1 keeping its ties (`SelectQuery.with_ties`).

The readers' form (`engine/readers/readings.py`) holds a subset: one block, or two joined by `UNION`,
`INTERSECT`, `EXCEPT`, or a nested condition (a scalar subquery, `IN` or `NOT IN`); per block the
selected columns with their aggregates, conditions under one `AND` or `OR` with `=`, `!=`, `>`, `<`,
`>=`, `<=`, `LIKE`, `NOT LIKE`, `IN`, or `NOT IN`, a date column's month or year-month, grouping, `HAVING`,
ordering in one direction, a limit, `DISTINCT`, and a share (an aggregate over the part's conditions
divided by the same aggregate over the whole). A plain column a grouped block shows or sorts by joins its
`GROUP BY`. `readings_of` names the reason a query falls outside the form (a self-join, a derived table,
`EXISTS`, a literal `IN` list, two columns compared, mixed `AND`/`OR`, three blocks), and the builder adds
nothing the readings do not say except the joins.

Grammar support does not imply perfect language coverage. A valid AST can still be absent
because the readers read the question wrongly or the form cannot hold its shape.

Arithmetic is handled by `engine/calculations/`, not by SQL-string templates. A specification
detects explicit syntax, binds role-named numeric columns, and expands the readers' candidates into a
`BinaryExpr` tree. The shared expander obtains complete join trees from `SchemaGraph`; composite keys
remain one typed foreign-key edge. The post-ranking verifier describes the selected AST and requires the
expected expression plus a complete registered-key path between its bound operands on every `UNION`,
`INTERSECT`, or `EXCEPT` branch before a number can be released.

Registered forms are `SUM(amount * rate_to_<target>)` for direct currency conversion (`AVG` when
the base query averages the amount),
`SUM(numerator) / SUM(denominator)` for ratios and per-capita measures, and
`SUM(amount * rate)` for flat tax/commission or explicit annual one-year simple interest. Percent
columns are divided by 100; fraction/decimal columns are not. A temporal rate table is eligible only
when the typed foreign key includes its temporal coordinate. Piecewise tax schedules, unspecified
interest periods, and latest-prior/as-of joins are not silently approximated; they clarify. Currency
also retains its filter, identity, and stated-unit realizations. ISO parsing and the canonical
`rate_to_<code>` convention live in `engine/currency_intent.py`.

## Validation and safety

Before rendering, recursive validation checks:

- table and alias scope;
- join connectivity;
- scalar and set-query arity;
- operand and literal types;
- grouped projection and ordering rules;
- compound-query compatibility;
- `DatePart` reads a `DATE` column, and its part is `month` or `year_month`;
- `DateSpan` reads `DATE` columns, its unit is a day, week, month or year, and it runs until an ISO date;
- a `GROUP BY` term is a column or a `DatePart`;
- `with_ties` needs `ORDER BY`, a `LIMIT`, and named projections (no `SELECT *`);
- invalid aggregate forms such as `COUNT(DISTINCT *)`.

The renderer quotes identifiers and literals. The planner emits query ASTs, not arbitrary
SQL statements. Serving also retains its SELECT-only execution guard.

## Module map

| Module | Responsibility |
|---|---|
| `engine/readers/` | The learned readers, the decoder, and the deterministic builder ([above](#readers-and-builder)). |
| `engine/sql_ast.py` | Immutable AST, validation, and rendering. |
| `engine/sql_dates.py` | Calendar phrases of a question as typed date comparisons; the month words a query realized. |
| `engine/sql_durations.py` | Duration phrases of a question, the date columns a span runs between, and the duration words a query realized. |
| `engine/sql_grounding.py` | Pool eligibility: text literals that fit their column and joins the foreign keys allow; which SUM or AVG reads only rows its joins repeat (a selection preference). |
| `engine/sql_schema.py` | Typed schema and join-path search. |
| `engine/sql_candidate.py` | Scored-candidate container and evidence. |
| `engine/sql_expansion.py` | Question-word helpers shared by the date and duration readers, the calculations, decomposition and the world routes; `plain_marks`. |
| `engine/sql_rank.py` | The pool contract (`execution_op_limit`), the selection record (`PoolSelection`) and its post-ranking rule (`select_ranked_candidate`); `analyze_question` for the calculations. |
| `engine/query_contract.py` | The unreadable-measure checks (`unreadable_operand`) and the `Coverage` record. |
| `regress/sql_import.py` | SQL text importer used by offline evaluation, training labels and tests; never used by serving. |
| `engine/calculations/core.py` | Typed plans and branch-preserving computation evidence. |
| `engine/calculations/specifications.py` | Registered currency, ratio, and rate-application semantics. |
| `engine/calculations/search.py` | Calculation-plan expansion of the readers' candidates into validated AST candidates. |
| `engine/calculations/registry.py` | Shared selection, verification, and clarify policy. |
| `engine/currency_intent.py` | Currency syntax and canonical rate-column rules used by the currency specification. |
| `engine/deterministic/plan.py` | Immutable topologically ordered plan shared by both source emitters. |
| `engine/deterministic/lower.py` | Strict lowering from the supported typed-AST subset; unsupported shapes remain on SQL. |
| `engine/deterministic/emitter/` | Deterministic SQL view-stack and readable SQLAlchemy/Python source emitters. |
| `engine/deterministic/runtime.py` | In-memory Python loading, execution policy, and SQL/Python parity checking. |
| `engine/decomposition.py` | Closed compound proposal validation and typed leaf-plan fusion. |

## Spider results

[`spider/results/RESULTS.md`](../spider/results/RESULTS.md) is the single authoritative benchmark
record, including exact configuration, source commit, artifact hashes, and dirty-worktree state.
Do not copy changing accuracy figures into architecture documentation. The standard comparison is
the serving-faithful `whole_db` run: all database tables, the served selection, and no gold table
hints. `gold_tables` is an oracle table-selection diagnostic, not a standard Spider result.

A measured accuracy belongs to the whole served selection: the readers, pool execution and grounding,
the re-scorer and the refusal, loaded from the same runtime bundle the service loads. The readers'
training sources include Spider's train and dev questions (all but 20 calibration databases), BIRD
and Spider-Syn, so a Spider DEV run reads databases the readers trained on; the Spider test set
appears in no training step and is scored only in totals
([training/readers/README.md](../training/readers/README.md)).

## Reproduction

Fetch Spider data:

```bash
python spider/probe/fetch_data.py --include-train
```

Run the serving-faithful evaluation. It calls `TableQuery.select_query`, the serving selection,
with the runtime bundle in `engine/data` and the readers in `engine/data/readers`; there is no
planner-mode flag and no model to pass.

Standard Spider (whole_db — the headline comparison number):

```bash
python -m spider.probe.full_eval \
  --dbs spider/data/dbs --config whole_db \
  --tag serving_whole_db
```

Oracle table selection (gold_tables — the product-analogue upper bound): rerun the same
command with `--config gold_tables`. `--selection pool_oracle` additionally executes the whole
pool and scores each question by its best member: the ceiling any selection change could reach.
Model training and promotion are covered in [`docs/TRAINING.md`](TRAINING.md) and, for the readers,
[`training/readers/README.md`](../training/readers/README.md).

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
python -m tests.test_readers
python -m tests.test_sql_ast
```

`tests.test_readers` covers the readers' deterministic half: the readings form and its builder, the
decoder's generic semantics, and the serialization, with outputs written as a model would write them.
`tests.test_sql_ast` executes SQL against in-memory SQLite and covers AST typing, rendering, joins,
grounding, the SQL import gate, and the plumbing of the one own-data selection over a pool written as
SQL and imported into the typed AST, with the re-scorer and refusal stubbed. Neither loads weights.

Run the repository aggregate suite with:

```bash
python -m tests.run_all
```

## What remains

Spider is not solved. The next work should be measured against these bottlenecks (see RESULTS.md
for recorded numbers):

1. Recall: measure pool recall (`--selection pool_oracle`) separately from the served top-1. A question
   with no correct pool member needs a reading the readers do not make or a shape the form cannot hold.
2. Ranking and refusal: where a correct candidate is pooled but not served, separate the re-scorer's
   order from the refusal's decision.
3. Latency: read the request's `[timing]` line (`pool_execute`, `pool_grounding`, `rescore`)
   before optimizing.
4. Treat a larger reader or a new training mix as a controlled experiment under
   [training/readers/README.md](../training/readers/README.md).
