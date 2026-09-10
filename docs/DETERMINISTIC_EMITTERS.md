# Deterministic SQL and Python execution

The own-data AST, grounded world bindings, and selected composition primitives lower into one
shared plan. Both emitters generate readable programs from that plan. This document describes
the source tree; deployment and evaluation evidence must identify the tested revision separately.

## One planner, two readable programs

The engine constructs and ranks typed SQL AST candidates. For a supported winner,
`lower_select_query()` constructs one immutable `AnalysisPlan`. SQL and Python emitters independently
consume that plan. No model writes Python source, and neither emitter translates the other's source.

Sonnet proposes a named analysis action and slug. The engine validates the intent and owns planning,
revision allocation, code generation, and execution. A slug such as `total_amount` is the Python
method name and every SQL view's prefix. Each revision regenerates its program. A generated package
currently contains one analysis method, not all conversation methods in one accumulating module.

## The view chain is the Python data flow

The plan begins with `combined`. Every later stage consumes its immediate predecessor; the final
stage projects or reduces its input. The generated wrapper follows this structure (arguments abbreviated):

```python
class OrdersCustomers:
    def total_amount(self) -> AnalysisResult:
        total_amount_combined = View.from_orm(...)
        total_amount_enriched = total_amount_combined.for_each(...)
        total_amount_filtered = total_amount_enriched.filter(...)
        total_amount_calculated = total_amount_filtered.for_each(...)
        total_amount_total = total_amount_calculated.reduce(...)
        return AnalysisResult(result=total_amount_total, views=(...))
```

| Stage | SQL form | Python operation |
|---|---|---|
| Combined | Input joins | `View.from_orm` over one ORM query |
| Enriched | Reference inner joins | `previous.for_each`, traversing object relationships |
| Filtered | `WHERE` | `previous.filter`, with an explicit predicate |
| Calculated | Retained columns plus expressions | `previous.for_each`, retaining objects and earlier values |
| Projected | Selected outputs | `previous.for_each`, producing result rows |
| Reduced | Aggregates and optional grouping | `previous.reduce` or `previous.group_reduce` |
| Ordered | `ORDER BY ... NULLS LAST`, optional `LIMIT` | `previous.sort`, with explicit tie keys |
| Correlated | Share, running total, or previous-period join | `previous.for_each`, with visible reduction/join expressions |

The actual source includes the ORM query, row classes, transformation bodies, predicates, initial
aggregate state, and operator calls. SQL `SUM(gross_amount)` corresponds to
`SUM(result.total_amount, row.gross_amount)` in the reduction callback. `operators.py` contains
ordinary Python functions and loops, not an expression interpreter. `group_reduce` makes one pass
over input rows and finalizes the groups afterward.

Python stages materialize tuples. SQL creates temporary **views**, not PostgreSQL materialized views.
Reading each SQL stage can recompute its predecessors. Both backends retain stage rows for inspection;
that has memory and database costs, including in SQL mode.

## ORM objects and storage

Generated table classes are SQLAlchemy declarative mappings. Stage dataclasses contain ORM objects
and calculated values; they are not the table mappings. For example, a generated `Order` exposes
`customer_id: Mapped["Customer"]` and `city: Mapped["City"]` as relationships. The physical customer
key is stored in a private `_customer_id_value` attribute used for joins and trace flattening.
Composite relationships retain every key pair and have explicit ORM join conditions.

Enrichment follows declared scalar paths such as `order.city.country`. Required references drop
missing objects (inner joins); optional references preserve them (left joins). Collection-valued enrichment is rejected until both
emitters implement its multiplicity. SQLAlchemy select-in loading may issue additional relationship
queries after the combined query.

Production reads the existing PostgreSQL conversation and knowledgebase schemas. A schema translation
maps logical `conversation` to the already authorized `c_<32hex>` namespace. Python execution does
not eliminate conversation schemas or copy PostgreSQL data into in-memory SQLite.

Uploaded tables lack declared primary keys. Automatic lowering proves identity from actual column
values after numeric upload coercion, preferring a unique reference key or identifier and then a
non-null unique composite row. When an uploaded PostgreSQL table has identical duplicate facts,
its snapshot-local `ctid` supplies ORM identity without changing the upload. It is not a durable
business key and must not be retained as identity across snapshots. `"01"` and `1` cannot be distinct
integer identities. Joins still require a proven scalar target. Hand-authored plans must supply valid keys.

World resolution persists an `entity_qid` beside each connected cell. Generated secondary
relationships use that association to expose the actual `knowledgebase.city`, country, hospital,
restaurant, or bank object directly on the input model. Same-name cities can include the uploaded
country as a composite association key. Currency conversion uses the actual exchange-rate object
joined on currency and date, not a precomputed converted amount.

## Request selection

Use one value per URL:

```text
/?load=customer-orders&use=sql
/?load=orders-tiers&use=both
/reason?use=py
/reason/c_6852132aa60a4260a1af3ecced605b4d?use=both
```

`load` selects the home dataset; `use` selects execution for subsequent questions. The browser
preserves it through reason/conversation navigation, example selection, and the Sheets picker.
It includes `use` in direct `/api/reason` and `/chat` bodies. The orchestrator forwards it on every
engine call independently of Sonnet's tool arguments. Unknown values are rejected by request validation.

| Public value | Internal mode | Supported shared plan | Unsupported shape |
|---|---|---|---|
| `sql` | `sql` | Emitted SQL | Existing SQL executor |
| `py` or `python` | `python` | Generated Python | Error; no accepted answer |
| `both` or `verify` | `verify` | Both, comparing every stage | Error; no accepted answer |
| `auto` | `auto` | Row-threshold selection | Existing SQL executor |
| Omitted | Deployment default | Configured policy for named analyses | Existing SQL executor |

An explicit override on a direct request receives a transient `query` slug, enabling the lowering
hook without creating a named workbook catalog entry. A direct request without an override or named
analysis retains its existing SQL behavior. Named analyses use
`DETERMINISTIC_EXECUTION_MODE=auto|python|sql|verify`, default `auto`. This environment setting applies
within the supported subset; it does not force unsupported planners to emit Python.

`auto` selects Python at or below `DETERMINISTIC_PYTHON_ROW_LIMIT`, default `10000`, using the total
input row estimate. It does not estimate join expansion, object size, or reference loading. No
benchmark establishes a universal Python speed advantage or the optimal crossover point.

Successful responses identify the actual execution, for example:

```json
{"execution":{"requested":"verify","actual":"verify","verified":true,"implementation":"shared_plan"}}
```

`actual` uses internal names; `implementation` is `shared_plan` or `sql_executor`. Shared-plan responses
also include `deterministic`: actual mode, both sources, manifests, hashes, and optional debug path.
The MCP/chat adapter preserves this evidence. On an unsupported explicit Python request, the error
may report `actual: "sql"`: the current guard checks the completed route result before accepting or
saving it. It does not preflight every planner, and provisional progress can precede the final error.

Opening an existing conversation or inspecting a revision restores saved results. Changing `use`
does not rerun history; submit a question to use the new mode. The current UI has no dedicated
generated-Python source viewer.

## Execution and comparison

`DeterministicAnalysis.run()` accepts a SQLAlchemy engine or connection. With an engine it opens one
transaction for the entire run and uses PostgreSQL `REPEATABLE READ`, so both backends and all stage
reads share a snapshot. Callers supplying a connection own its transaction and isolation level and
must arrange equivalent snapshot consistency.

Python loads uniquely named in-memory modules, calls the slug method, and removes those module
registrations on exit, including failures. SQL creates temporary views, reads them, and drops them
in reverse order. The service returns complete rows; only trace previews are limited to 50 rows.
The serving API separately retains its existing 50-row answer preview.

Operators propagate SQL nulls, ignore null aggregate operands, return zero for empty counts, and
return null for empty sums/averages/minima/maxima. Division by zero returns null. Python execution
uses a local 128-digit Decimal context; division and averages round to 20 decimal places with ties
away from zero, matching the emitted PostgreSQL `ROUND(..., 20)` policy.

Verification compares each ordinary stage as an unordered multiset, preserving duplicates and column names.
Explicit sorted stages are also checked in order; top-N adds deterministic tie keys.
Decimal values, integers, and decimal representations of floats normalize without rounding significant
digits. A mismatch raises `VerificationMismatch` with the failing view in an exception note.
Agreement does not prove that the shared planner correctly understood the question.

SQLite is a hermetic test backend. Its native numeric storage and arithmetic are not PostgreSQL
NUMERIC. Passing simple SQLite fixtures does not validate arbitrary fractional PostgreSQL arithmetic;
production parity needs PostgreSQL tests with representative values.

## Source layout and lifetime

```text
engine/deterministic/
  plan.py                 shared stages, expressions, tables, and relationships
  lower.py                supported typed AST -> AnalysisPlan
  world.py                resolved world slots -> AnalysisPlan
  compose.py              selected composition primitives -> AnalysisPlan
  context.py              request context and final mode reporting
  operators.py            Python loops and SQL-semantic operators
  runtime.py              package loading, SQL execution, normalization
  service.py              policy, records, snapshot ownership
  emitter/sql/__init__.py SQL source emitter
  emitter/py/__init__.py  Python source emitter and debug writer
  _gen/py/<conversationId>/<slug>/
    base.py
    orders.py
    customers.py
    city.py
    country.py
    orders_customers.py
    manifest.json
```

Source is emitted in memory in every shared-plan mode, including SQL. Python source is syntax-checked;
Python and verification modes compile and execute it in process. Source comes from the closed emitter.
The loader is not a sandbox for arbitrary client-supplied Python.

`APP_ENV=development` also writes identical source bytes to `_gen`. Before writing, the writer validates
the conversation/slug path and replaces only that directory to remove stale modules. Files remain for
debugging; `_gen` is Git-ignored. Production writes no generated files unless
`DETERMINISTIC_PERSIST_GENERATED=true`. Execution always uses in-memory source, even with a debug copy.
Responses and saved analysis snapshots can retain source: memory-only execution does not mean source
is never persisted.

Records include source and per-file hashes, emitter versions, relationship edges, stages, dataset
version, and knowledgebase release when supplied. World and composition hooks supply the knowledgebase
refresh/model version. Own-data programs have no knowledgebase dependency. A null release is not a
pinned knowledgebase snapshot. Request timing exposes separate emission, Python, and SQL durations
through the existing timing collector; backend durations include stage materialization and ORM loading.

## Coverage and extension

Own-data AST lowering supports unaliased inner joins, Boolean comparison filters, projections and arithmetic,
`COUNT/SUM/AVG/MIN/MAX`, and grouped aggregates whose projected group columns precede aggregates.
Its aliases, self-joins, DISTINCT, HAVING, ordering, limits, subqueries, and set queries remain outside
that AST adapter. This is distinct from the composition adapter's supported ordering and correlated operators.

The world adapter consumes the existing planner's grounded relationship chain, filters, selected
measure, registered calculation, and currency binding. It does not parse rendered SQL. Geographic
and non-geographic scalar world queries, including the default customer-orders FX question, use it.
World-only DISTINCT projections and grouped world extrema still require additional typed bindings.

The composition adapter consumes the existing ComposeEngine's selected primitive records. It supports
filters, grouped reductions, HAVING thresholds, divide, share, running total, previous-year joins,
sorting, and top-N. Its ORM reference graph points to the existing knowledgebase relations, not a
model over the flattened `knowledgebase facts` preview. The current compose planner still materializes
its candidate in SQLite to bind and route; the selected answer is executed by the shared plan.
Candidate planning time is not part of the backend-only timing comparison.

Extend coverage by adding typed plan semantics, implementing both emitters, adding stage parity
fixtures, and connecting the relevant planner. Reusable Python and SQL functions are future extension
points, not a current plugin registry. They need explicit types, null/numeric semantics, versions,
dependency records, and tests against independent expected results.

See [TESTING.md](TESTING.md) for verification boundaries and
[the review record](notes/2026-09-10-deterministic-review.md) for findings and remaining work.
