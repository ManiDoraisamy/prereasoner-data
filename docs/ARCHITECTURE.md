# Architecture

Status: **current runtime architecture**. Future changes are marked as targets or pending work;
they are not shipped features. See the [documentation map](README.md) for the difference between
current, opt-in, external, and planned behavior.

PreReasoner represents a question, its data, and its source evidence as named dimensions. The current
runtime composes those dimensions into a checked SQL query, runs it, and returns the result with its
rows and trace. A frozen Qwen model supplies signals about intent and schema. It does not generate
SQL or numeric answers. AST construction, routing, joins, validation, ranking, and execution are
deterministic for fixed inputs, configuration, database state, and model files.

Read [GETTING_STARTED.md](GETTING_STARTED.md) first when setting up the repository. Read
[SQL_AST.md](SQL_AST.md) for planner internals, [SOURCE_DATA.md](SOURCE_DATA.md) for
publisher-owned references, and [../db/README.md](../db/README.md) for the database contract.

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
        +-- response: rows and SQL, plus route-specific evidence/provenance
        +-- optional Firebase RTDB progress events
```

The optional `orchestrator/` service wraps this API in a conversational tool loop. `mcp_server/` exposes the same
engine operation to MCP clients. Neither component owns data reasoning or may invent a numeric result.

## Data Scopes

The PostgreSQL deployment uses four distinct scopes:

| Scope | Schema | Contents |
|---|---|---|
| Wikidata shared data (legacy names) | `knowledgebase`, `public` | Current resolution index, taxonomy, Wikidata-backed entity tables, and staging/geo data; target migration is described below |
| Synchronized reference sources | See `SOURCE_DATA.md` | Nine publisher-owned schemas with active physical releases; logical datasets remain separately deployment-gated |
| Application metadata | `chat` | Admin-migrated conversation, user-profile, and ownership tables; serving receives DML only |
| Conversation | `c_<32hex>` | Uploaded tables and persisted world-resolution bridges |
| User | `m_<md5(subject)>` | Reusable private reference tables |

The verified Firebase subject determines the user scope. A client-supplied conversation id is accepted only after
an ownership check. Client input never selects a user schema directly.

Private references are not added wholesale to a SQL `search_path`. `engine.master.relevant_tables()` loads a
bounded set, applies the production foreign-key detector to uploaded and saved tables, and selects only references
connected to the request. Selection runs to a fixed point, so a valid multi-hop chain can be included. Selected
references then become ordinary typed planner tables; there is no reference-specific SQL generator.

## Semantic Contract: Vocabulary, Observations, And Facts

Keep these three layers separate:

1. **Schema.org is the vocabulary.** The pinned Schema.org 30.0 contract defines the classes,
   properties, inheritance, domains, and ranges that training and serving can name. A coordinate
   can be represented even when the current corpus has no examples for it.
2. **Source-owned datasets supply observations.** Wikidata properties are mapped to Schema.org
   properties, and publisher relations are exposed through explicit source mappings. Wikidata is
   the main public entity-identity bridge; it is not the authority for facts owned by IANA, CLDR,
   GeoNames, ECB, CDC, NLM, or another publisher.
3. **Versioned source releases supply answer facts.** Models learn how a question and a column fit
   together, not mutable values. A model may recognize an exchange-rate relation, but the dated rate
   still comes from a pinned ECB release and must pass temporal and calculation checks.

There are currently two learned stages over this contract. The shared encoder uses a historical
71-property compatibility basis derived primarily from capped Wikidata observations, plus
structural, intent, and calculation training. The active named-dimension head uses the newer
multi-source Schema.org corpus and emits calibrated class proposals consumed by `engine/router.py`.
It can propose a resolver family but cannot authorize a join or change a numeric answer. These are
distinct artifacts, and their corpus and metric claims must remain separate. See
[TRAINING.md](TRAINING.md) and [MODEL_CARD.md](MODEL_CARD.md).

## Domain Semantics And Enrichment

Market-led domain profiles and deterministic shared-reference enrichment are specified in
[KNOWLEDGE_ENRICHMENT_ROADMAP.md](KNOWLEDGE_ENRICHMENT_ROADMAP.md). The active publisher
inventory is recorded in [SOURCE_DATA.md](SOURCE_DATA.md). A physical source release being
active does not make it planner-visible. `iana_country` is the first code-approved logical
dataset in the generic enrichment registry. Raw Terraform still requires
`ENRICHMENT_ACTIVE_DATASETS=iana_country`; the guided Community deployment sets it after the
database grant step. The raw Terraform default remains empty.
The production request flow now contains the guarded integration boundary: explicit intent,
domain-role recognition, database-backed registry selection, request-local materialization,
trusted-edge propagation, and serving provenance. Currency conversion is a deliberately
bounded exception to the still-disabled generic temporal registry: the ECB sync is transformed
offline into a release-labelled daily `knowledgebase.exchange_rate` projection, and the world
planner joins dated facts on the exact `(currency, date)` pair. It carries the prior published
business-day value only during projection construction, never through request-time network or
latest-prior SQL. The calculation verifier proves direction and aggregate arithmetic; missing
coverage fails closed. No embedded or demo FX fixture is a production fact source.

The target source-materialization design makes the database boundary explicit: physical shared
schemas are source-owned (`wikidata` after its pending migration and the publisher schemas in
`SOURCE_DATA.md`), while domain roles describe request-private operational tables and classify
source datasets in the planner registry. Each new publisher source owns a versioned `release`
table. The target `public` schema contains no application data, but the current runtime still uses
legacy `public` Wikidata staging and geo tables declared by `db/init.sql`. `engine.relations` remains the
relationship-graph owner, and the existing typed AST remains the only SQL planner. Enrichment does not add a router,
planner, or request-time network path.

Existing Wikidata country, currency, timezone, city, and administrative-area tables migrate from legacy
`knowledgebase` and `public` into source-owned `wikidata`. IANA and CLDR facts remain in `iana` and `cldr`; they are
linked to Wikidata by explicit registry edges and are not copied into domain schemas. Each source fact has one
writable owner and each exposed relation has one planner role.
The measured baseline found incomplete/mixed Wikidata code data and empty timezone staging, so M1 uses pinned IANA
and CLDR snapshots. Each implemented refresh inserts a complete immutable release and activates it atomically;
upserting into active reference rows is prohibited because it retains upstream deletions and breaks deterministic
replay. The legacy Wikidata schema migration is still pending.

## Request Flow

1. `db/init.sql` plus the privileged application migration command prepare shared `chat` metadata before deployment;
   `engine.server` then validates the body, verifies the principal, and parses uploaded tables.
2. `engine.master` validates or selects private references. `engine.relations.discover_fks()` is the canonical
   relationship detector used here and by planning.
3. `engine.server` resolves the conversation id and verifies ownership before selecting its working schema.
4. `engine.knowledge.KnowledgeReasoner` receives the complete working table set and the question.
5. `engine.routing.route()` makes the single serving route decision. Composition owns only a grounded world
   dependency that needs multi-step operations. Self-contained uploaded/reference data stays on the AST path.
6. The selected planner emits guarded, quoted, read-only SQL and executes it against the conversation schema plus
   the explicitly reachable shared knowledge tables.
7. Cross-route calculation verifiers inspect typed planner evidence before a result is released. Without changing
   scores, the shared registry selects the highest-ranked candidate that realizes every detected calculation. An
   unmet or ambiguous calculation replaces the numeric result with a structured clarification.
8. The engine returns rows, SQL, route evidence, and intermediate views. Trace writes are best effort and do not
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
3. read the required rows from the pinned, offline-built Wikidata projection;
4. persist a conversation-scoped cell-to-QID bridge;
5. join through QID foreign keys and execute the requested operation.

`engine/knowledge_query.py` owns ordinary world lookup. `engine/knowledge_compose.py` and `engine/compose.py` own
multi-step operations that genuinely require a world dependency. `engine.routing.compose_owns()` is the authority;
a primitive prediction alone cannot seize a self-contained own-data question.

World lookup does not contact Wikidata at request time. Missing rows or ambiguous keys are an explicit
coverage/abstention outcome; synchronization and release activation are offline operational responsibilities.

## Named-Dimension Typing And Its Evidence

Typing uses URI-named Schema.org coordinates, and every class decision reports the computation that
produced it. `engine/schema_model.py` summarizes a table or column through the shared Qwen encoder and
emits calibrated probabilities for 80 trained property URIs. `engine/schema_decode.py` computes each
class as:

```text
sigmoid(class_bias + sum(property_probability * signed_property_weight))
```

The response evidence contains the bias, class threshold, each property probability, signed weight,
and contribution. `tests/test_schema_decode.py` independently recomputes the score and tests causal
interventions on named coordinates. This is the actual calculation path, not a generated explanation.

The compiled Schema.org 30.0 contract (`engine/schema_org.py`,
`engine/data/schema_org_v30.json`) represents all 1,521 properties and 926 classes. The promoted head
trains 80 property coordinates, validation-qualifies 56, and releases 11 classes after validation-only
selection and untouched-test checks. Unsupported coordinates and classes explicitly abstain.

`engine/router.py` is the one learned column-routing owner. It maps a released class through ontology
inheritance to a coarse resolver family. A class proposal is never sufficient for a join:
`engine/knowledge_query.py` verifies that the values ground to source keys. When the model abstains,
the inherited exact source-membership route can recover grounded coverage. This matters for geo
classes that do not yet clear model release gates. The old 71-coordinate family consensus remains in
the bundle only for shared-encoder compatibility and diagnostics; it is not a competing production
router.

Two corpus invariants are enforced at build time, because a leaking corpus trains cleanly and scores *better*
for leaking. **Splits are drawn per derivation group, never per instance:** the group is everything left of the
first `#` in an instance id, so a table, its columns, its presentation variants and its row windows share one
draw. `SemanticInstance.validate` re-derives the split and rejects any assigned value, which makes the previous
"inherit the parent's split" patch — one policy with two implementations — unrepresentable. **Every labelled
property must be evidenced in the text the encoder actually reads:** labels whose values appear nowhere, or only
past the `max_len=128` truncation point, are wrong supervision rather than weak, so the builder drops them and
packs wide relations into facets that each fit the budget. The build additionally refuses to write a corpus in
which identical text spans two splits, or in which the realized split shares drift out of bounds.

Mutable facts stay outside model weights. The model learns that `quote_currency + effective_date + units_per_eur`
is an exchange-rate shape; the rate for a date still comes from the pinned `ecb.exchange_rate` release. Class
evidence is captured through the typing buffer in `engine/knowledge_query.py` and attached to the answer by
`engine/knowledge.py`; a load or decode failure falls back to deterministic grounding and never fabricates a
classification.

## Deterministic Reference Enrichment

Publisher-owned reference facts remain in publisher-named schemas. `engine/enrichment/registry.py` is the single
policy owner and describes each logical dataset with a `DatasetDefinition`: qualified relations, identity and lookup
keys, lookup cardinality, ambiguity behavior, temporal selection, licensing, row-level restrictions, and activation.
Registration never grants planner visibility.

`engine/enrichment/store.py` resolves exactly one active source release and returns a typed `SnapshotPin` containing
the source schema, release ID, schema version, and definition hash. Reads are key-bounded, release-qualified, ordered,
and network-free. Replays may read a retired pinned release; ordinary requests resolve the active release once at the
start of enrichment.

Source capabilities are intentionally different: exact dimensions, ambiguous relations, numbering-pattern metadata,
temporal series, temporal rule sets, bounded calendars, terminology hierarchies, and rights-bearing document graphs.
`engine/enrichment/intents.py` extracts only explicit requested attributes. `engine/calculations/` owns
typed arithmetic intent, AST expansion, and post-ranking proof. Its registered specifications currently
cover currency filter/conversion, ratios including per-capita measures, and flat tax, commission, and
explicit annual one-year simple-interest amounts. The shared encoder can order eligible operands; typed
columns, unit rules, complete join keys, and all-branch evidence determine admissibility. A planner route
without typed computation evidence fails closed. `engine/currency_intent.py` remains the currency
specification's ISO syntax and `rate_to_<code>` convention, not a second verifier. Ordinary own-data
grouping does not activate enrichment.
`engine/enrichment/adapters.py` returns typed match, absence, ambiguity, policy-denial, and ineligibility
outcomes while retaining snapshot and license provenance. Temporal capabilities still abstain
because row-level temporal AST semantics are not implemented in the generic enrichment planner.
The ECB daily projection above avoids that unsupported operation by materializing exact-date rows
offline; VAT rules, holidays, and other temporal source definitions remain disabled until their
own semantics and evaluation gates are complete.

Trusted composite edges pass separately to `TableQuery.ingest`; they are not accepted from a client table payload.
`SchemaGraph` stores their ordered column pairs as one foreign key, and `sql_ast.Join` validates and renders them as one
atomic `ON a.x = b.x AND a.y = b.y` clause. Legacy scalar fields remain available to existing planner components.
Source activation has two keys: the registry definition must be code-approved and the deployment allowlist must
name it. `db/reference_grants.py` derives read-only relation grants from that same registry; a deployment must use
a non-superuser serving role. Snapshot rollback uses `db/sync/releases.py` and accepts only a previously validated
retired release.

`engine/domain_profiles.py` owns seven versioned market profiles and their Schema.org mappings;
`engine/domain_typing.py` emits conservative request-local role evidence. Dataset definitions may declare
`compatible_roles`, which is a mandatory runtime gate for domain-sensitive references. `engine/enrichment/runtime.py`
then combines role evidence with explicit request intent, value type, activation, snapshot, exact row coverage, and
policy outcomes. A match becomes an ordinary bounded planner tab plus a trusted edge. The server attaches
`provenance.enrichment` only when a dataset was actually used, so disabled/no-intent requests retain their prior
response shape.

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

The engine container includes the complete runtime. The orchestrator is opt-in in Docker Compose and Terraform;
the engine does not require an Anthropic key. The orchestrator container copies only the auth, trace, and
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

The default [weight repository](https://huggingface.co/prereasoner/prereasoner-weights) is public. The
source manifest pins an immutable revision and every runtime-file hash, so a fresh clone can provision and
verify the compatible bundle without credentials. Reproducing the full hosted application still requires a
locally built knowledge database; hermetic planner, routing, reference, MCP, and browser tests require neither.

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
