# Architecture

PreReasoner turns a question and tabular data into inspectable SQL, executes that SQL, and returns the rows and
trace. The engine does not use a decoder to generate SQL or numeric answers. A frozen Qwen model supplies semantic
signals; typed AST construction, candidate ordering, routing, joins, validation, and execution are deterministic for
fixed inputs, configuration, database state, and model artifacts.

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
        +-- response: rows, SQL, route, views, trace identifiers
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

## Semantic Contract: Ontology, Observations, And Facts

The semantic architecture has three layers that must not be conflated:

1. **Schema.org is the semantic shell.** The pinned Schema.org 30.0 contract defines the class,
   property, inheritance, domain, and range coordinates that training and serving name. A coordinate
   remains representable even when no training source currently supplies examples for it.
2. **Source-owned datasets supply observations.** Wikidata properties are mapped into Schema.org
   properties, and publisher relations are projected through explicit source mappings. Wikidata is
   currently the largest single corpus contributor and the public entity-identity bridge; it is not
   the ontology or the authority for facts owned by IANA, CLDR, GeoNames, ECB, CDC, NLM, or another
   publisher.
3. **Versioned source releases supply answer facts.** Models learn semantic shapes, not mutable
   values. A model may recognize an exchange-rate relation, but a dated rate must still come from a
   pinned ECB release and pass deterministic temporal and calculation checks.

There are currently two learned consumers of this contract. The unified column router uses a
71-property Schema.org basis derived primarily from the capped Wikidata entity corpus, plus
structural, intent, and calculation training. The separate table-evidence head uses the newer
multi-source Schema.org corpus. It can emit auditable class evidence but does not route a request or
change an answer. These are distinct shipped artifacts, not evidence that the unified router has
already been retrained on every publisher source. See [TRAINING.md](TRAINING.md) and
[MODEL_CARD.md](MODEL_CARD.md).

## Domain Semantics And Enrichment

Market-led domain profiles and deterministic shared-reference enrichment are specified in
[KNOWLEDGE_ENRICHMENT_ROADMAP.md](KNOWLEDGE_ENRICHMENT_ROADMAP.md). The active publisher
inventory is recorded in [SOURCE_DATA.md](SOURCE_DATA.md). A physical source release being
active does not make it planner-visible. `iana_country` is the first code-approved logical
dataset, but deployment still requires `ENRICHMENT_ACTIVE_DATASETS=iana_country`; the default
empty allowlist keeps current production answers unchanged.
The production request flow now contains the guarded integration boundary: explicit intent,
domain-role recognition, database-backed registry selection, request-local materialization,
trusted-edge propagation, and serving provenance. No exchange-rate fixture is registered in
production policy. The synchronized ECB series remains planner-ineligible until row-level
effective-date selection, currency direction, and rounding are represented and evaluated.

The source materialization design makes the currently ambiguous database boundary explicit: physical shared schemas are source-
owned (`wikidata` after migration and the publisher schemas in `SOURCE_DATA.md`), while domain roles describe request-private
operational tables and classify source datasets in the planner registry. Each source owns a versioned `release`
table. PostgreSQL `public` contains no application data. `engine.relations` remains the
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
3. lazily materialize required Wikidata-backed rows;
4. persist a conversation-scoped cell-to-QID bridge;
5. join through QID foreign keys and execute the requested operation.

`engine/knowledge_query.py` owns ordinary world lookup. `engine/knowledge_compose.py` and `engine/compose.py` own
multi-step operations that genuinely require a world dependency. `engine.routing.compose_owns()` is the authority;
a primitive prediction alone cannot seize a self-contained own-data question.

World lookup may depend on Wikidata availability for an uncached entity. Existing cached data and emitted SQL make
successful results reproducible, but external source availability is an operational dependency rather than model
entropy.

## Named-Dimension Typing And Its Evidence

Typing is intended to use named Schema.org coordinates, and every typing decision is reported as
the firing that produced it. The two current consumers have different authority: the legacy column
family model actively selects candidate world tables and can therefore change routing, while the
newer table-class head is evidence-only and cannot change an answer.

**Column families (`engine/router.py`).** One trained encoder reads the legacy 71-coordinate basis of
`engine/data/alloc.json` off a column, and the family is decoded by consensus: the fraction of a family's
distinctive properties firing above their per-property Youden-J thresholds (`props_thr.json`). `route()` returns
that per-property evidence — property, score, threshold, fired — so a decode such as "place at 1.0" is reducible to
the named properties that produced it. Grounding, not the family, remains the decisive gate for a world join.
This basis predates strict ontology validation: `GeoCoordinates` is a Schema.org class rather than
a property, and `taxonName` is not in Schema.org 30.0. The active router therefore does not yet
fully conform to the ontology-only architecture. Migrating it requires a controlled retrain and
serving transition; the URI-indexed evidence head must not be represented as that migration.

**Table classes (`engine/schema_decode.py`, `engine/schema_model.py`).** The compiled Schema.org 30.0 contract
(`engine/schema_org.py`, `engine/data/schema_org_v30.json`) defines 1,521 property URIs and 926 classes as stable
coordinates. A frozen-Qwen linear head emits a calibrated probability per trained property URI, and a class is a
deterministic superposition of those coordinates: its score is the weighted fraction of its signature properties
that fire above their calibrated thresholds — the same consensus rule the family router uses. Because the score is
exactly the weighted fired fraction, the surfaced fired/missing records are the computation, not a narration of it;
`tests/test_schema_decode.py` asserts that recomputation and the intervention property (suppressing
`schema:currency` collapses `ExchangeRateSpecification` while leaving disjoint classes numerically identical).

The table head's current trained basis contains 75 named properties. It is not the same checkpoint or
property basis as the 71-property unified router. Coverage is explicit rather than implied. Every
ontology class is representable; a class is servable only after it
clears held-out precision and recall gates, and the artifact records each class as servable, calibration-failed,
observed-insufficient, or representable-unobserved. Unservable classes abstain. The corpus
(`training/schema_org/`) projects active publisher releases into class-labelled semantic instances, Wikidata
entities into per-entity and per-property column instances, and the committed demo uploads into class-free
negatives.

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
`engine/knowledge.py`; a load or decode failure disables the evidence loudly and never fails a request.

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
outcomes while retaining snapshot and license provenance. Temporal capabilities still abstain because row-level temporal
AST semantics are not implemented.

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
