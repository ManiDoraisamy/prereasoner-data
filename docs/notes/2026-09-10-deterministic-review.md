# Deterministic execution review — 2026-09-10

Scope: shared-plan lowering; SQL and Python emission; SQLAlchemy object loading; numeric, null,
aggregate, and relationship semantics; request-local execution selection; Spider evaluation; and
production/browser release checks. This is a point-in-time engineering record. The canonical
behavioral contract remains [DETERMINISTIC_EMITTERS.md](../DETERMINISTIC_EMITTERS.md).

## Architecture after the review

One immutable `AnalysisPlan` remains the only input to both emitters. The Python emitter does not
parse SQL, and the SQL emitter does not translate Python. The selected planner AST, grounded world
bindings, or selected composition primitives lower into the plan before either program is emitted.

Every direct request now gets an ephemeral `query` analysis context; named workbook revisions keep
their persisted slug. With the default `auto` policy, supported plans use Python when the estimated
total input is at most 10,000 rows and emitted SQL above that threshold. Unsupported plans continue
through the established SQL executor. Explicit Python or verification never accepts a SQL fallback.

The generated classes are real SQLAlchemy mappings. A foreign-key attribute such as
`Order.customer_id` exposes a `Customer` object while its physical key remains in a private mapped
storage attribute. The combined query hydrates joined relationships from the entities it already
selected. Enrichment loads only declared maximal relationship paths, re-roots through combined
objects, and uses `lazy="raise"` to prevent an accidental N+1 query.

## Findings addressed

| Finding | Resolution |
|---|---|
| Omitted `use` on direct requests bypassed the deployment policy | Every direct request receives a transient analysis context, so the configured default applies without creating a workbook revision |
| A row-count underestimate could materialize an unbounded Python join | The ORM query requests at most `limit + 1`; every Python stage propagates and enforces the hard limit |
| A Python exception in `auto` could fail the request | Python runs inside a savepoint; any ordinary runtime failure rolls back and executes emitted SQL against the same outer snapshot, recording the reason |
| Joined ORM relationships could trigger redundant or undeclared loads | Combined entities populate scalar relationships directly; enrichment uses explicit maximal `selectinload` paths and all mappings reject undeclared lazy access |
| A path such as `order.customer.country` could reload an already selected customer | Loader paths re-root at an object already materialized by `combined` |
| Collection joins could silently collapse or duplicate objects | Combined and enrichment stages reject collection relationships until both emitters have explicit multiplicity semantics |
| Group reduction treated a valid `None` accumulator as a missing group | Membership, rather than `dict.get`, now distinguishes group creation from state |
| Python rendered booleans as `True`/`False` while PostgreSQL text casts use lowercase | The `TEXT` operator emits `true`/`false` and preserves null |
| Float literals could introduce binary-float differences | Plan construction canonicalizes finite floats to `Decimal`; non-finite and unsupported date-time literals fail before emission |
| Typed SQL date literals reached Python as strings | The AST adapter parses validated ISO date literals to `date`, so ORM date comparisons use the same type |
| SQLite-backed evaluation could label timestamp text as a date and fail while loading an unrelated mapped column | Generated date mappings use native PostgreSQL `DATE` and normalize SQLite ISO date or timestamp text before execution |
| `IS` accepted operands that the SQL emitter could not represent consistently | Plans restrict `IS` and `IS NOT` to null and boolean literals |
| Relationship predicates could capture an unrelated table | Validation scopes primary and secondary join predicates to the relationship endpoints and association table |
| Explicit execution modes could accept negative or boolean row budgets | All modes validate nonnegative integer estimates and limits before selection |
| Empty scalar aggregate with a zero input limit could lose SQL's one-row result | Scalar reduction retains SQL's one output row while the input materialization remains bounded |
| Spider Python evaluation risked defining a new accuracy metric | The existing runner now executes the selected AST with SQL, Python, auto, or verification and continues to use the existing gold execution and `spider_eval.compare` contract |

Earlier changes in this release also preserved deterministic evidence through the knowledge and chat
adapters, checked the complete emitted view stack for coverage, removed the 50-row result truncation
(while retaining a 50-row trace preview), required stable ORM identity, retained composite keys and
prior calculated values, and made Python/SQL verification share one repeatable-read snapshot.

## Correctness and performance boundaries

Stage parity proves that two implementations of the same shared plan agree; it does not prove that
the planner chose the right plan for the question. Spider scalar-gold evaluation supplies that
independent expected-result check. Its Python modes preserve the selected candidate, planner ranking,
input cap, gold query, and comparison logic. Reports must include lowering coverage and must not
present an oracle `gold_tables` run as the gold-blind headline.

Python's threshold is deliberately enforced twice: once by the estimate used for selection and once
by actual materialization. Width, reference depth, and correlated operations can still affect runtime
inside the bound. `auto` is therefore both a preference and an availability policy, whereas explicit
Python is a fail-closed diagnostic choice. Verification remains the strongest parity mode because it
compares every named stage on PostgreSQL.

SQLite fixtures validate generated-source execution and evaluator integration, but PostgreSQL is the
release authority for ORM loading, numeric behavior, schemas, and knowledgebase relationships. Browser
fixture tests validate request propagation and UI behavior; an authenticated hosted Chrome run is a
separate launch check.

## Release evidence

Release evidence is recorded in the task/commit that deploys this review rather than frozen into this
design note. A launch claim requires, at minimum: focused emitter tests; the hermetic repository runner;
the live PostgreSQL dataset matrix in SQL, Python, and verification modes; the existing Spider
scalar-gold evaluator with Python-aware fields; hosted health checks; and a Chrome journey through the
deployed application. Any skipped authentication- or infrastructure-gated check must be named plainly.
