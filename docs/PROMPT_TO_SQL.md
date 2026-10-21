# From question to SQL

This walkthrough follows one request through Prereasoner. The important boundary is simple:
every query that runs is a typed AST the engine validated and rendered itself. Learned encoders read
the question beside the schema and say what it asks for as named readings: which tables and columns,
which aggregates, conditions, groups and orderings. A deterministic builder makes each reading a typed
query, and the engine serves the one a learned re-scorer ranks first among those that run and fit the
data, when a learned refusal is confident of it. No model writes SQL.

We trace **`"total amount in France"`** through the stack. This happens to need public world data,
but the own-data planning steps are the same for an ordinary table question.

This world-data example follows the SQL serving path. Grounded world bindings and supported own-data winners also lower into
one shared plan that emits a SQL view stack and readable ORM/Python stages. The backend is selected
by request context, after planning. See [DETERMINISTIC_EMITTERS.md](DETERMINISTIC_EMITTERS.md) for
that path and its limits; the world example here does not demonstrate Python world execution.

---

## Stage 1 - the schema is read beside the question

[`engine/readers/text.py`](../engine/readers/text.py) writes the schema as the readers read it: each
table's name and column names split into words, each column's inferred type, and a few of its values.
The stage-1 model (XLM-R base, [`engine/readers/search.py:ReadingSearch.schema_items`](../engine/readers/search.py))
reads the question beside one table at a time and scores the table's use and each column's
(values illustrative):

```
orders : order id [number] (101, 102, 103) | country [text] (France, Spain, Italy) | amount [number] (50, 80, 30) |
```

The six most probable tables are kept, and for a table with more than 20 columns its 20 most probable
columns (`ReadingSearch.prune`).

## Stage 2 - the readers read the question as named readings

Each stage-2 reader (XLM-R large) reads the question beside the kept tables and scores every fact the
builder uses ([`engine/readers/layout.py`](../engine/readers/layout.py)): the structure (one block, or two
joined by `UNION`, `INTERSECT`, `EXCEPT` or a nested condition), and per block each column's select
aggregate and position, condition operator, grouping, `HAVING`, ordering, each table's use, `DISTINCT`,
`AND`/`OR`, direction and limit, plus pointers to the spans of the question that hold a value or a limit.
A reader marked anchored also sees the question's own values listed first beside the columns that hold
them, matched on the data ("players from college UCLA" lists UCLA beside the college).

[`engine/readers/decode.py:candidates`](../engine/readers/decode.py) turns the scores into up to 16 of
the most probable distinct readings, each with the log probability of the decisions it took:

```
structure: single
block 0:   tables [orders]   select [SUM orders.amount]   where [orders.country = "France"]
```

Generic semantics shape them: a date the question names is read as the engine reads dates
([`engine/sql_dates.py`](../engine/sql_dates.py)), a column an equality pins to one value is not shown
again, and a pointed value drops the marks of the list it stood in.

## Stage 3 - the builder makes each reading a typed AST

[`engine/readers/readings.py:build`](../engine/readers/readings.py) assembles each reading out of the
frozen dataclass nodes in [`engine/sql_ast.py`](../engine/sql_ast.py), joining the tables a block uses
along the schema's foreign keys:

```
SelectQuery(
  select   = ( SelectItem( Aggregate("SUM", ColumnRef("orders","amount")) ), ),
  from_table = "orders",
  where    = Comparison( ColumnRef("orders","country"), "=", Literal("France") ),
)
```

The node types are a real grammar: `SelectQuery`, `SelectItem`, `Aggregate`, `ColumnRef`, `Comparison`,
`BooleanExpr`, `OrderTerm`, `Join`, `ScalarSubquery`, `InPredicate`, `DatePart` (the month of a date column,
for "signed in August", or its year-month, for "total amount by month"), … The builder adds nothing the
reading does not say except the joins. Each candidate is wrapped as a
[`ScoredQuery(query, sql, score, evidence, features)`](../engine/sql_candidate.py): the score is the
decoder's log probability, and the evidence is the reading's plain-English "read as" line
([`engine/readers/read_as.py`](../engine/readers/read_as.py)). The readers' candidates are pooled, each SQL
once, most probable first, and the registered calculations (`engine/calculations`) add their expansions.

## Stage 4 - run each candidate and check it against the data

[`engine/tables.py:select_query`](../engine/tables.py) runs every candidate on an in-memory copy of the
tables, under the SELECT guard and a fixed budget of SQLite VM steps. A query that fails is out. So is one
that compares a text column with a literal the column never holds while another column does
(`customer_name = 'Lyon'` when `Lyon` is in the city column), and one that joins two columns the foreign
keys keep apart ([`engine/sql_grounding.py`](../engine/sql_grounding.py)). The candidates that are
left are *eligible*. Running is a validity check, not proof of meaning: an eligible query can still answer
a different question.

## Stage 5 - re-score, then serve or decline

The re-scorer (XLM-R base, `ReadingSearch.rescore`) reads the question beside each eligible candidate's
read-as line and the shape of its result, with the decoder's log probability and rank, and orders them.
Preferences can pick a later eligible candidate: a date the question names keeps the choice to the
candidates that realize it; a candidate whose SUM or AVG reads only rows its joins repeat, such as a
report's Total Amount summed once per subscription the report row matched, is served only when every
eligible candidate does ([`engine/sql_grounding.py`](../engine/sql_grounding.py)); and a calculation intent
takes the best-ranked candidate that realizes it (`engine/calculations`).

The refusal (`ReadingSearch.confident`, a logistic model fitted on held-out databases) then decides.
When it is confident, the choice is served and the response's `read_as` is shown as the turn's "read as"
line. When it is not, nothing is served and the reply says how the best-ranked candidate read the
question: "I read this as “…”, but I'm not confident that is what you asked"
([`engine/tables.py:unsure_clarification`](../engine/tables.py)). The response's `planner.selection`
records the pool counts (how many ran, how many were grounded), the served member's rank, and its log
probability.

## Stage 6 - render the tree to a SQL string

The winning AST is rendered to `SELECT SUM("amount") FROM "orders" WHERE "country" = 'France'`, guarded
(SELECT-only), and executed. A served top-1 ranking (`ORDER BY ... LIMIT 1`) is rendered with
`RANK() OVER` so it keeps every row tied with the first (`sql_ast.keep_ties`).

---

## Why this shape (the payoff)

Because the models only **read the question**, and the engine **builds, checks and renders the tree**:

- **Interpretable** — the reading behind the answer is shown as its "read as" line, and the selection
  record shows how many candidates ran, were grounded, and where the served one ranked.
- **Deterministic** — the same input and model files yield byte-identical SQL: the models run in fixed
  precision with no sampling, and ties keep pool order. This is enforced by a cross-process repeatability
  test in [`tests/test_routing.py`](../tests/test_routing.py).
- **Valid by construction** — every candidate is a well-typed AST that passes validation before it can
  win, so the planner cannot emit malformed SQL.

Contrast with a pipeline that executes a model's SQL directly: a column the model invents, a clause it drops,
or a join it guesses goes straight to the database. Here no model writes SQL; a reading the builder cannot
make into a valid query is dropped, and one the readers are unsure of is declined with what they read.

## The one caveat in this example: world queries

`"total amount in France"` is a **world** query — `France` is *not* a value in the uploaded cities, so it can
only be reached by resolving `city → country` against the Wikidata-backed entity store, with
Schema.org-named typing evidence. The shared router
([`engine/routing.py:route`](../engine/routing.py)) detects a **necessary world dependency** and lets the
`ComposeEngine` build a **view-stack** (`world_join → world_filter → group_agg`) instead of a single
`SelectQuery`. The selected world bindings then lower into the same deterministic plan used by SQL and Python.
For a purely own-data prompt like `"how many customers in Paris"`, you get the single `SelectQuery` exactly as
drawn above (`SELECT COUNT(*) … WHERE city = 'Paris'`). For an analytical own-data prompt such as a top-N or
share, ComposeEngine may supply a multi-view binding; explicit `use=py` or `use=both` lowers that binding through
the same Python/SQL emitter pair rather than accepting an opaque SQLite answer.

See [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) for how routing decides own-data vs. world, and
[`docs/SQL_AST.md`](SQL_AST.md) for the planner in depth.

---

## Where to look in the code

| Concern | File · symbol |
|---|---|
| The schema as the readers read it | `engine/readers/text.py` · `Schema`, `table_text`, `fit` |
| Schema items, readings, re-scoring, refusal | `engine/readers/search.py` · `ReadingSearch` |
| The models and their inputs | `engine/readers/models.py` |
| Outputs to the most probable readings | `engine/readers/decode.py` · `candidates` |
| Readings to a typed AST | `engine/readers/readings.py` · `build` |
| The "read as" line | `engine/readers/read_as.py` · `read_as` |
| The typed AST node grammar | `engine/sql_ast.py` · `SelectQuery`, `SelectItem`, `Aggregate`, `Comparison`, … |
| One scored candidate | `engine/sql_candidate.py` · `ScoredQuery` |
| Run and ground the candidates | `engine/tables.py` · `select_query`, `_executable`; `engine/sql_grounding.py` · `grounded_members` |
| Choose among the eligible candidates | `engine/tables.py` · `_choose`; `engine/sql_rank.py` · `select_ranked_candidate`, `PoolSelection` |
| Offline SQL → typed AST fixtures and training labels | `regress/sql_import.py` · `import_sql`; never used by serving |
| Serving entry point (select, render, execute) | `engine/tables.py` · `select_query`, `_serve_ast` |
| Own-data vs. world routing | `engine/routing.py` · `route`, `compose_owns`, `reads_upload_whole` |
