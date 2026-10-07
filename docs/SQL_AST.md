# Own-Data SQL Planner

This is the own-data SQL planner: the path from a question over uploaded tables to executed SQL.
Every query it can run is a validated typed abstract syntax tree (AST). A bounded deterministic
search builds and ranks the candidates; selection runs them and serves the best-ranked one that runs
and is grounded in the data. No local model writes or scores SQL, and no step samples: the search is
rule-based, and selection filters its ranking with a stable order. Only when no candidate is
eligible, and the operator enabled Gemini, does the labelled fallback run
([below](#labelled-gemini-fallback)).

## What it does

Given a question, tables, and foreign keys, the planner:

1. Builds a typed schema graph.
2. Links question roles to tables, columns, operators, and values.
3. Constructs, validates and ranks search candidates with named deterministic features.
4. Expands recursive queries, constraints, extrema, and set operations when applicable.
5. Runs up to 25 ranked candidates on an in-memory copy of the tables. A query that fails, compares
   a text column with a value it never holds while another column does, or joins columns the foreign
   keys keep apart is ineligible.
6. Serves the best-ranked eligible candidate. Named dates, a SUM or AVG that reads only rows its joins
   repeat (served only when every eligible candidate does), calculation intents, a named money total,
   and the decomposition leaf contract constrain that ranking; they never rescore it.
7. Only when no candidate is eligible and Gemini is enabled, runs the labelled fallback.
8. Renders only validated ASTs to SQL. A served top-1 ranking keeps every row tied with its first
   row (`sql_ast.keep_ties`, rendered with `RANK() OVER`); a larger `LIMIT` keeps exactly that many rows.

For every direct or named request in the supported dual subset, the winner lowers into
one immutable `AnalysisPlan`. SQL and readable SQLAlchemy/Python are emitted independently from that
plan; neither source is parsed to create the other. See
[DETERMINISTIC_EMITTERS.md](DETERMINISTIC_EMITTERS.md).

When a named request is compound — the search reads the question as a set operation — the engine
requests one bounded decomposition retry instead of running a query that answers a fragment.
Compound structure is the search's own reading, taken before any candidate runs: a named request
that is not compound is served by the best-ranked single query. A conversational model proposes
only natural-language leaf questions and a closed `cross`/`anti_join` topology. The same planner
selects every leaf, under the leaf contract; `engine/decomposition.py` fuses their typed outputs into
one DAG. No text of the decomposition proposal becomes SQL, Python, an identifier, a join key, or an
intermediate result. A leaf never takes the Gemini fallback (`select_query(..., allow_fallback=False)`):
a leaf with no runnable query rejects the decomposition, and the chat model restates the leaf.

AST validity is broader than dual-emitter coverage. The lowering adapter requires proven ORM row
identities and scalar join targets. Unsupported ASTs retain SQL execution under the default policy
or `use=sql`; `use=py` and `use=both` cannot accept those fallback results. Stage parity is a backend
equivalence test and does not replace tests against the independently expected meaning of a question.

Foreign keys may contain one or several ordered column pairs. A composite key remains one
logical graph edge and one `Join`; rendering produces an atomic conjunction such as
`ON child.country = parent.country AND child.postal = parent.postal`. The validator rejects
any component that does not connect the new table to the existing join graph.

With the fallback off, the same inputs produce the same selection. That removes sampling variance,
not natural-language ambiguity, schema-linking errors, missing search rules, or ranking errors. A
fallback answer also depends on Gemini's reply.

## Architecture

The runtime path (`TableQuery.select_query`, engine/tables.py) is:

```text
question + tables + foreign keys
          |
          v
      SchemaGraph
          |
          v
  typed bounded AST search + named deterministic ranking (CandidateRanker)
  -> up to 25 ranked candidates (SEARCH_CANDIDATES)
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
  serve the best-ranked eligible candidate          served_by: search
  (named dates, a total that counts no row twice,
   a calculation intent or a named money total may
   prefer a later eligible one)
          |
          |   none eligible, and the operator enabled Gemini
          +------------------------------------------------+
          |                                                v
          |                       one request-scoped wording rewrite
          |                       search again -> run/ground/serve
          |                       (original wording also coverage-checked)
          |                                                served_by: gemini-rewrite
          v                                                |
  validated AST -> SQL (or SQL + Python) <-----------------+
  a top-1 ranking keeps its ties (keep_ties)
```

The search's construction and ranking rules are hand-written; the frozen encoder supplies role and
schema similarities to them. Each candidate carries its named ranking features and evidence
(`ScoredQuery.features`, `ScoredQuery.evidence`), so its place in the order can be explained term by
term. Selection adds no score of its own: it walks the ranking and serves the first eligible member,
subject to the date, double-counting, calculation and money-total preferences
(`engine/sql_rank.py:select_ranked_candidate`).
`PoolSelection.record()` reports the decision:

| Selection field | Meaning |
|---|---|
| `pool_size` | Ranked search candidates pooled (at most 25) |
| `executable` | Members that ran within the step budget |
| `misgrounded` | Members that ran but bind a literal or a join the data or the foreign keys contradict |
| `eligible` | Members that ran and are grounded |
| `selected` | Pool index of the served member, or `null` when none is eligible |
| `rank` | The served member's place among the eligible members (0 is the best ranked) |
| `score` | The served member's search score |
| `calculation_satisfied`, `money_total` | Whether a calculation intent or a named money total chose it |
| `date_satisfied`, `double_counted` | Whether it keeps the dates the question names, and whether a SUM or AVG of it reads only rows its joins repeat |
| `served_by` | `search` or `gemini-rewrite` |
| `fallback` | Present when the rewriter ran: `kind`, `model`, the rewording, and why nothing was served |

When the fallback served the answer, the counts describe the deterministic search pool for the
reworded question.

### Labelled Gemini fallback

`engine/question_rewrite.py` runs when no candidate is eligible or the selected plan leaves request words
unresolved, and `engine/llm.py` reports Gemini available (`EXTERNAL_LLM_ENABLED` and a Vertex AI
project). It makes one request-scoped rewrite; the
deterministic search then builds, runs, and grounds candidates as usual:

1. `rewrite`: Gemini returns one rewording of the question in the tables' own words
   (`engine/sql_prompt.py:REWRITE_SYSTEM`). A rewording equal to the question, ignoring case and
   spacing, is discarded. The search runs on the rewording, so the SQL is still the search's.
The prompt carries the question and schema text from `engine/sql_prompt.py`: table and column names,
inferred types, and foreign keys. In the question, the phrases the search reads as cell values are quoted, and one
it reads as held inside a column's values ("all inspection checklist") is spelled as that column containing the
value (`sql_prompt.prompt_question`). It carries no other cell value, no conversation history, and no rows.
Replies are JSON objects of a fixed shape and bounded in length. Recognized source values, quoted
text, and numbers from the question must remain in the rewrite; the reply is not cached.
[ARCHITECTURE.md](ARCHITECTURE.md#labelled-gemini-fallback)
describes the data Gemini receives and the answer labels.

## Public API

### Serving entry point

Live serving goes through `engine/tables.py`. `TableQuery.serve(tables, question)` runs the
full own-data pipeline (ingest → schema → `_serve_ast` → guard → execute) and returns the
answer plus the winning candidate. `_serve_ast` calls `select_query`, the one own-data selection
also used by decomposition leaves, the Spider evaluator, and the offline regression gate. The compose
host's search probe (`engine/decomposition.search_probe`) reads only its first stage, `search_pool`: whether
the question is compound, and whether the upload reads it whole (`engine/routing.reads_upload_whole`):

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

engine = EncoderQuery()          # loads the runtime bundle: the encoder and its heads
result = engine.serve(tables, "list each customer name and total order amount")
print(result["sql"])
print(result["selection"])       # pool counts, the served member's rank and score, served_by
print(result["fallback"])        # None unless the labelled Gemini fallback ran
```

To see the whole decision, call `select_query` on normalized tables:

```python
norm, fks = engine.ingest(tables)
sch, colidx, tablemap = engine.schema(norm, fks)
selection = engine.select_query("list each customer name and total order amount",
                                norm, fks, sch, tablemap)
for index in selection.ranking:                 # eligible members, best ranked first
    print(selection.pool[index].score, selection.pool[index].sql)
print(selection.selected, selection.served_by)  # the served pool index and what built it
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
`planner` object with `ast`, `candidate_count`, `evidence`, `features`, and `selection` (the fields
above). A fallback answer also carries the top-level `fallback` record, and its `model` string names
the Gemini model.

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
- value exclusions: "not", "except", "excluding" or "without" excludes (`!=`) the first value named after it,
  within three words, and each value "and", "or" or "nor" joins to that one ("orders not Done or Cancelled");
  a value named after another value is kept ("orders not Done in France"), and a negation word inside a value
  is the value's ("Not Started"). The completeness check refuses a query that excludes a value the question
  keeps or keeps one it excludes (`sql_search.value_polarity`, read by `query_contract.constraint_violations`);
- substring filters: `LOWER(column) LIKE '%text%'` (`Lower`), for the texts a question asks values to hold
  (`sql_search.substring_requests`): a quoted text or the word after "substring", "letter" or "word"; a
  text right after "contain" or "include", whole value or not ("keywords containing 'inspection checklist'"),
  while a column's words between them keep a whole value one value ("contain the paragraph text 'Brazil'");
  and a whole value after "all" or "every" that one row holds while other values hold it ("all inspection
  checklist", not "all Paris orders"). The completeness check requires each such text compared with LIKE, in
  the column the question names right before "contain" or "include" when it names one ("a city containing",
  "the state whose name contains") or in the column whose value "all" quantified, and reads the words that
  asked for it once it is;
- aggregate operands: a text column is an aggregate's operand only where its name ends the phrase the
  aggregate word begins ("the total of the Amount"; "total keyword volume" totals a volume), and an average a
  spelled column name holds ("Avg. monthly searches") asks for none by itself (`sql_expansion.asked_cues`, which
  the search, the ranker and the constraint expansion's "X or Y" readings all read);
- rows a listing names: a listing of numbers alone shows first the text column its filter keeps several values
  of (a LIKE pattern, `NOT LIKE`, `!=`, `IN` over two or more values or `NOT IN`, or an `OR` of one column's
  values), so "the Avg. monthly searches for all Keyword containing 'inspection checklist'" lists each keyword
  beside its searches (`sql_search._rows_named`, applied after the ranking). A filter on one value, a listing
  that shows a text or key column, a total or other aggregate, a group, `DISTINCT` and a single row are
  unchanged;
- calendar phrases on a date column (`engine/sql_dates.py`): a month without a year compares
  `DatePart('month')`, and a dated phrase ("after August 10, 2026") compares the date itself;
- grouping by a column, or by the year-month of a date column (`DatePart('year_month')`, '2026-08'):
  "total amount by month", "monthly", "per month", "which month" (`sql_dates.period_grouping`);
- durations (`engine/sql_durations.py`): "more than 6 months", "at least 2 weeks", "over a year ago" compare
  `DateSpan`, the days from a row's start date to its end date, or to the question's date while it has none,
  over the unit's length (a month is 30.4375 days, a year 365.25);
- grouping, `HAVING`, ordering, and limits;
- direct and multi-hop foreign-key joins;
- aliases and self-joins;
- scalar subqueries, `IN`, `NOT IN`, `EXISTS`, and `NOT EXISTS`;
- derived tables and nested aggregation;
- `UNION`, `INTERSECT`, and `EXCEPT`;
- row extrema, frequency extrema, zero-inclusive counts, dual extrema, and top-N, with a served
  top 1 keeping its ties (`SelectQuery.with_ties`).

Grammar support does not imply perfect language coverage. A valid AST can still be absent
because the question was linked to the wrong role or no search rule proposed that shape.

Arithmetic is handled by `engine/calculations/`, not by SQL-string templates. A specification
detects explicit syntax, binds role-named numeric columns, and proposes a `BinaryExpr` tree. The
shared expander obtains complete join trees from `SchemaGraph`; composite keys remain one typed
foreign-key edge. The post-ranking verifier describes the selected AST and requires the expected
expression plus a complete registered-key path between its bound operands on every `UNION`,
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
| `engine/sql_ast.py` | Immutable AST, validation, and rendering. |
| `engine/sql_dates.py` | Calendar phrases of a question as typed date comparisons; the month words a query realized. |
| `engine/sql_durations.py` | Duration phrases of a question, the date columns a span runs between, and the duration words a query realized. |
| `engine/sql_grounding.py` | Pool eligibility: text literals that fit their column and joins the foreign keys allow; which SUM or AVG reads only rows its joins repeat (a selection preference). |
| `engine/sql_schema.py` | Typed schema and join-path search. |
| `engine/sql_search.py` | `SQLSearcher`: base beam, capability ordering, and candidate assembly. |
| `engine/sql_candidate.py` | Scored-candidate container and evidence. |
| `engine/sql_expansion.py` | Shared AST construction helpers. |
| `engine/sql_recursive.py` | Recursive queries, sets, aliases, and self-joins. |
| `engine/sql_constraints.py` | `HAVING`, disjunction, scalar, and membership rules. |
| `engine/sql_extrema.py` | Extrema, top-N, and set difference. |
| `engine/sql_parsimony.py` | Bounded projection/table variants of pooled candidates (minimal join, binding, drop/add column, operand swap, DISTINCT). |
| `engine/sql_rank.py` | Hand-written search ranking features (`CandidateRanker`), the pool contract (`SEARCH_CANDIDATES`, `execution_op_limit`), and the selection record (`PoolSelection`, `FallbackRecord`). |
| `engine/question_rewrite.py` | The labelled, request-scoped Gemini wording rewriter; the deterministic search still owns SQL. |
| `engine/sql_prompt.py` | The schema text and instructions Gemini reads in the fallback. |
| `regress/sql_import.py` | SQL text importer used by offline evaluation and migration tools; not used by serving fallback. |
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

A measured accuracy belongs to the whole served selection: search, pool execution and grounding,
loaded from the same runtime bundle the service loads, with the Gemini fallback off. The recorded
runs measured earlier designs, the search alone and then the search with a local SQL-writing model
and a fitted arbiter, and then the current selection (2026-10-02).

## Reproduction

Fetch Spider data:

```bash
python spider/probe/fetch_data.py --include-train
```

Run the serving-faithful evaluation. It calls `TableQuery.select_query`, the serving selection,
with the runtime bundle in `engine/data` (fetch it with `python -m engine.fetch_weights`); there is
no planner-mode flag and no model to pass. Leave `EXTERNAL_LLM_ENABLED` unset for the headline: the
evaluator records `fallback.enabled` and `fallback.model` in its contract, so a run with the
fallback on is a separate, labelled measurement.

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
expansion, deterministic ordering, pool execution and grounding, and the SQL import gate. They
stub the encoder, and their planner has no fallback attached, so they need no weights and make no
Gemini call.

Run the repository aggregate suite with:

```bash
python -m tests.run_all
```

## What remains

The planner is deterministic, and Spider is not solved. The next work should be measured against these bottlenecks (see RESULTS.md
for recorded numbers):

1. Coverage: add search rules for question families with no correct pool member. The fallback is
   not a substitute for a search rule, and the headline is measured without it.
2. Ranking: measure pool recall (`--selection pool_oracle`) separately from the served top-1. Where
   a correct candidate is pooled but outranked, fix the named ranking rule that put another first.
3. Latency: read the request's `[timing]` line (`pool_execute`, `pool_grounding`, `fallback`)
   before optimizing.
4. Treat a larger encoder as a controlled capacity experiment after objective and data changes.

Search controls and evidence use functional names (`recursive`, `constraint`, and `extrema`) rather
than historical implementation phases; these capabilities are one planner, not separate architectures.
