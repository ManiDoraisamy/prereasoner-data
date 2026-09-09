# Deterministic SQL and Python emitters

Status: **current for named own-data analyses in the supported lowering subset**. The typed SQL
AST remains the only planner. The feature adds a backend-neutral, immutable view plan and two
deterministic source emitters; it does not add a second language model, planner, or ranker.

## Contract

A named analysis slug identifies the same computation in both languages:

```text
typed SQL AST
      |
      v
immutable AnalysisPlan
      |
      +---------------------------+
      |                           |
      v                           v
SQL view emitter             Python source emitter
      |                           |
combined -> filtered -> ...   combined -> filter -> ...
      |                           |
      +------------+--------------+
                   v
          optional parity check
```

The plan owns table classes, physical columns, ORM relationships, expressions, aggregate
operators, and the ordered materialized-view chain. Both emitters consume that same plan. Neither
emitter parses the other emitter's source.

Every view after `combined` must consume the immediately preceding view. The constructor rejects a
chain that skips a step. For example:

```text
total_amount_combined
    -> total_amount_enriched
    -> total_amount_filtered
    -> total_amount_calculated
    -> total_amount_total
```

The equivalent Python function is deliberately feed-forward:

```python
def total_amount(self) -> AnalysisResult:
    total_amount_combined = View.from_orm(...)
    total_amount_enriched = total_amount_combined.for_each(...)
    total_amount_filtered = total_amount_enriched.filter(...)
    total_amount_calculated = total_amount_filtered.for_each(...)
    total_amount_total = total_amount_calculated.reduce(...)
    return AnalysisResult(result=total_amount_total, views=(...))
```

`View.for_each`, `filter`, `reduce`, and `group_reduce` contain ordinary deterministic loops. The
generated function shows every transition and puts its operator expression at the call site. For
example, SQL `SUM(gross_amount)` is emitted as
`SUM(result.total_amount, row.gross_amount)` inside the reduction step. Grouping still has one
named `group_reduce` stage and one pass over its input; it does not create an unreported helper
view.

## ORM object model

Generated table modules use SQLAlchemy 2 declarative mappings over the existing PostgreSQL
conversation and knowledgebase schemas. Production does not copy PostgreSQL data into SQLite.

A foreign-key attribute is an object relationship, not a scalar pretending to be an object:

```python
class Order(Base):
    _customer_id_value: Mapped[int] = mapped_column(
        "customer_id",
        BigInteger,
        ForeignKey("conversation.customers.customer_id"),
    )
    customer_id: Mapped["Customer"] = relationship("Customer", ...)
    city: Mapped["City"] = relationship("City", ...)
```

The physical key remains private because SQLAlchemy needs it for joins. The public attribute is the
related object. A reference object may expose another object, such as `order.city.country`.
`schema_translate_map` binds the logical `conversation` schema to the authorized physical
`c_<32hex>` schema at execution time. Shared `knowledgebase` and `public` mappings retain their
fixed schemas.

Because uploaded PostgreSQL tables do not declare primary keys, lowering proves an ORM identity from
the request rows. It prefers a unique relationship target or identifier and otherwise uses the
unique composite row. A table with no stable identity is not dual-executed; it remains on the SQL
path. This prevents SQLAlchemy's identity map from merging repeated facts that share a foreign key.

The first view performs one ORM query that materializes the input object tuple, for example
`Order` plus `Customer`. An enrichment view traverses declared object relationships to add `City`,
`Country`, `Currency`, or another associated reference object. It does not perform a name-based
reflection guess at runtime.

## Source layout

Implementation:

```text
engine/deterministic/
  plan.py                  immutable shared plan and validation
  lower.py                 typed SQL AST -> supported shared plan
  operators.py             SQL-semantic Python operators and View loops
  runtime.py               in-memory package loader and parity comparison
  service.py               emission and execution policy
  emitter/
    sql/__init__.py         SQL view-stack emitter
    py/__init__.py          SQLAlchemy/Python source emitter
```

One generated package contains the table classes plus a wrapper named from its input objects:

```text
base.py
orders.py
customers.py
city.py
country.py
orders_customers.py        class OrdersCustomers; method total_amount(...)
```

The emitted package and SQL program record SHA-256 hashes. Their combined manifest records the
dataset version, knowledgebase release when supplied, relationship edges, view names, and both
emitter versions.

## Execution modes

`DETERMINISTIC_EXECUTION_MODE` controls supported named analyses:

| Value | Behavior |
|---|---|
| `auto` | Python at or below `DETERMINISTIC_PYTHON_ROW_LIMIT`; SQL above it |
| `python` | Compile and execute the generated Python package |
| `sql` | Execute the generated SQL view stack |
| `verify` | Execute both and fail at the first named stage whose normalized rows differ |

The `/reason` browser accepts a request-local override for a named analysis:

```text
/reason?use=sql
/reason?use=py
/reason?use=both
/reason/c_<conversation-id>?use=both
```

`sql` selects SQL, `py` selects generated Python, and `both` maps to `verify`. The browser carries
the preference in every direct `/api/reason` request and every orchestrated `/chat` engine call,
including follow-ups and the conversation URL rewrite. An omitted value uses the deployment setting.
The override is request-local and never changes `DETERMINISTIC_EXECUTION_MODE` for another concurrent
user.

`auto` is the default and the row limit defaults to `10000`. The estimate is the total number of
input rows selected for the plan. Verification compares every materialized view through exact
decimal normalization and ignores unspecified row order. A mismatch names the failing stage, so the
difference is localized instead of appearing only as a wrong final value. Response view records
include the shared operation name, column order, up to 50 materialized rows, and the exact SQL body.

The source is deterministic, parsed with Python's AST parser before it is accepted, compiled, and
executed as an ephemeral in-memory package. Production does not import a generated file from disk.

## Development files and production memory

With `APP_ENV=development`, the exact Python bytes that were compiled are also written to:

```text
engine/deterministic/_gen/py/<conversationId>/<slug>/
```

The directory contains the generated modules and `manifest.json`. Before a new revision is written,
only that validated conversation/slug directory is removed, preventing stale modules from a prior
revision. `_gen` is ignored by Git.

Production is memory-only by default. Setting `DETERMINISTIC_PERSIST_GENERATED=true` explicitly
enables the same debug write in another environment. The response/saved analysis may retain the
exact source and hashes for interpretation and audit, but production execution never depends on
that persisted copy.

## Current lowering boundary

`engine/deterministic/lower.py` currently lowers unaliased `SelectQuery` plans containing:

- one or more input tables connected by inner joins;
- one non-aggregate comparison filter;
- column or arithmetic projections;
- `COUNT`, `SUM`, `AVG`, `MIN`, and `MAX`;
- aggregate operands containing `+`, `-`, `*`, or `/`; and
- optional grouping when projected group columns precede aggregates.

The full SQL AST supports more than this initial dual subset. Aliases, self-joins, `DISTINCT`,
boolean predicate trees, `HAVING`, ordering, limits, subqueries, set queries, and advanced world
composition continue to execute through the existing SQL AST/view path. They are not translated
approximately. Extending dual coverage means adding a typed plan node, implementing it in both
emitters, and adding a parity test.

Manually constructed `AnalysisPlan` objects already support mixed conversation and associated
knowledgebase table classes and multi-hop enrichment paths. The current automatic serving hook is
limited to the own-data AST subset above; migration of the specialized world/compose plans must
make those planners emit the same neutral plan before they can use Python execution.

## Tests

Run:

```powershell
python -m tests.test_deterministic_emitters
python -m tests.test_analysis
python -m tests.test_sql_ast
```

The emitter suite verifies byte determinism, object-valued relationships, exact stage chaining,
typed-AST lowering, development source identity, execution policy, trace materialization, and
SQL/Python parity at every stage of the same relational graph.
