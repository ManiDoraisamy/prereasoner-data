# Architecture

PreReasoner turns a question and tabular data into inspectable SQL, executes that SQL, and returns the rows and
trace. The engine does not use a decoder to generate SQL or numeric answers. A frozen Qwen model supplies semantic
signals; typed AST construction, candidate ordering, routing, joins, validation, and execution are deterministic for
fixed inputs, configuration, database state, and model artifacts.

Read [GETTING_STARTED.md](GETTING_STARTED.md) first when setting up the repository. Read
[SQL_AST.md](SQL_AST.md) for planner internals and [../db/README.md](../db/README.md) for the database contract.

## System Boundaries

```text
browser or MCP client
        |
        | authenticated request: tables, question, optional conversation id
        v
engine/server.py
        |  HTTP parsing, limits, auth, conversation ownership
        |  private-reference adapter
        v
engine/knowledge.py
        |  one serving entry point
        v
engine/routing.py
        |
        +-- DELEGATE --> own-data typed AST or ordinary world lookup
        |
        +-- COMPOSE  --> world-dependent multi-step view composition
        v
guarded SQL execution in the conversation schema
        |
        +-- response: rows, SQL, route, views, trace identifiers
        +-- optional Firebase RTDB progress events
```

The optional `orchestrator/` service wraps this API in a conversational tool loop. `mcp_server/` exposes the same
engine operation to MCP clients. Neither component owns data reasoning or may invent a numeric result.

## Data Scopes

The PostgreSQL deployment uses three distinct scopes:

| Scope | Schema | Contents |
|---|---|---|
| Shared | `knowledgebase`, `public` | Resolution index, taxonomy, Wikidata-backed entity tables, geo data |
| Conversation | `c_<32hex>` | Uploaded tables and persisted world-resolution bridges |
| User | `m_<md5(subject)>` | Reusable private reference tables |

The verified Firebase subject determines the user scope. A client-supplied conversation id is accepted only after
an ownership check. Client input never selects a user schema directly.

Private references are not added wholesale to a SQL `search_path`. `engine.master.relevant_tables()` loads a
bounded set, applies the production foreign-key detector to uploaded and saved tables, and selects only references
connected to the request. Selection runs to a fixed point, so a valid multi-hop chain can be included. Selected
references then become ordinary typed planner tables; there is no reference-specific SQL generator.

## Request Flow

1. `engine.server` validates the body, verifies the principal, and parses the uploaded tables.
2. `engine.master` validates or selects private references. `engine.relations.discover_fks()` is the canonical
   relationship detector used here and by planning.
3. `engine.server` resolves the conversation id and verifies ownership before selecting its working schema.
4. `engine.knowledge.KnowledgeReasoner` receives the complete working table set and the question.
5. `engine.routing.route()` makes the single serving route decision. Composition owns only a grounded world
   dependency that needs multi-step operations. Self-contained uploaded/reference data stays on the AST path.
6. The selected planner emits guarded, quoted, read-only SQL and executes it against the conversation schema plus
   the explicitly reachable shared knowledge tables.
7. The engine returns rows, SQL, route evidence, and intermediate views. Trace writes are best effort and do not
   determine the answer.

## Own-Data SQL Planner

The own-data path is one bounded search over a typed SQL AST:

| Owner | Responsibility |
|---|---|
| `engine/sql_ast.py` | Immutable query nodes, type/visibility validation, rendering |
| `engine/sql_schema.py` | Typed schema graph and deterministic join-tree enumeration |
| `engine/sql_search.py` | Projection, filter, aggregate, grouping, order, limit, and base candidate expansion |
| `engine/sql_recursive.py` | Subqueries, `EXISTS`/`IN`, set operations, and self-join shapes |
| `engine/sql_constraints.py` | HAVING, disjunction, and relationship constraints |
| `engine/sql_extrema.py` | Row, aggregate, frequency, and zero-inclusive extrema |
| `engine/sql_profile_expansion.py` | Typed variants driven by predicted structural profiles |
| `engine/sql_rank.py` | Deterministic structural and encoder-derived candidate scoring |
| `engine/tables.py` | Planner facade, SQL guard, and local SQLite execution |

The planner supports multi-table and multi-hop joins. Candidate execution can reject invalid or failing SQL, but
execution success is not treated as proof that a query matches the question. Every deterministic ranking feature
is named in candidate evidence.

Model inference on this path is encoder-only. The encoder supplies similarities for table, column-role, and
structural-profile features. It does not call `generate()` and does not bypass AST validation.

## World Grounding And Composition

World grounding handles facts absent from uploaded data. For example, a table may contain cities while the question
filters by country. The path is:

1. route a column to a grounded entity type;
2. resolve cell values to QIDs through exact normalized lookup, then embedding fallback;
3. lazily materialize required Wikidata-backed rows;
4. persist a conversation-scoped cell-to-QID bridge;
5. join through QID foreign keys and execute the requested operation.

`engine/knowledge_query.py` owns ordinary world lookup. `engine/knowledge_compose.py` and `engine/compose.py` own
multi-step operations that genuinely require a world dependency. `engine.routing.compose_owns()` is the authority;
a primitive prediction alone cannot seize a self-contained own-data question.

World lookup may depend on Wikidata availability for an uncached entity. Existing cached data and emitted SQL make
successful results reproducible, but external source availability is an operational dependency rather than model
entropy.

## Private Reference Lifecycle

Reference tables have a narrow contract: the first column is a non-empty unique key and remaining columns are
attributes.

1. The browser marks an edited reference dirty.
2. Before querying, it saves dirty references through `POST /api/master`.
3. `engine.master.save_master()` validates and normalizes the shape, rejecting unsafe identifiers or keys before
   opening its write transaction.
4. The same operation atomically replaces the saved table.
5. A save failure stops the query instead of silently using an older version.
6. Deletion removes both browser state and the server-side table.

`engine/master.py` owns this behavior and its database transactions. `engine/server.py` only translates HTTP
requests and responses.

## Services

| Package | Role | Must not own |
|---|---|---|
| `engine/` | Authenticated reasoning, planning, grounding, execution | Presentation-only chat policy |
| `web/` | Workbook state, uploads, references, trace rendering | SQL semantics |
| `mcp_server/` | Typed adapter over the engine HTTP API | A second planner or result synthesis |
| `orchestrator/` | Optional conversation and tool invocation | Arithmetic or factual answers without engine evidence |
| `db/` | Reproducible schema and knowledge synchronization | Request routing |
| `training/` | Offline datasets, fitting, and calibration | A competing serving implementation |
| `spider/` | Serving-faithful evaluation and research artifacts | Production selection shortcuts |

The engine container includes the complete runtime. The orchestrator container copies only the auth, trace, and
configuration modules it imports; it does not contain planner weights or engine implementation modules.

## Concurrency And Failure Behavior

- Model instances are loaded once per service process and shared by the serving stack.
- World-sensitive operations are serialized where shared mutable database state requires it.
- Database operations use bounded inputs and explicit transaction ownership.
- SQL is read-only, identifiers are quoted, and conversation schemas are ownership checked.
- Reference writes are atomic and failures are visible to callers.
- Optional trace or presentation failures do not fabricate a successful answer.
- External lookups use bounded retry behavior; live integration tests can therefore be slower than hermetic tests.

## Artifacts And Configuration

`engine/config.py` is the owner for runtime environment variables. `.env.example` documents deployable defaults.
Runtime model artifacts live under `engine/data/` and are validated by a manifest; they are not source files and are
not duplicated under versioned names. Training output becomes serving input only through the documented promotion
process in [TRAINING.md](TRAINING.md).

The default weight repository is private at the time of writing. Publishing the source therefore does not by itself
make the full application reproducible; an open compatible bundle or an explicitly configured replacement is still
required. Hermetic planner, routing, reference, MCP, and browser tests do not require that bundle.

## Change Rules

The repository has one owner per decision:

- routing: `engine.routing.route()`;
- relationship discovery: `engine.relations.discover_fks()`;
- own-data SQL representation: the typed AST;
- private-reference behavior: `engine.master`;
- runtime configuration: `engine.config`;
- measured Spider claims: `spider/results/RESULTS.md`.

When replacing behavior, migrate every caller and remove the displaced path. Do not add another planner, router,
evaluation contract, or model bundle. See [../CONTRIBUTING.md](../CONTRIBUTING.md) and [../CLAUDE.md](../CLAUDE.md)
for the required validation and change discipline.
