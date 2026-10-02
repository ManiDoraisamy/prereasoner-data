# Decision record — the open-source consolidation

This repo was consolidated from an iterative research codebase (twenty backend generations, a
separate frontend repo, three Cloud Run services). Every structural choice below was made
deliberately during that consolidation; this file records what changed and why, so reviewers
don't have to reverse-engineer the reasoning.

## One service, not three

The backend previously ran as three Cloud Run services (dimension / reason / world). The reason
and world services loaded the **same** model stack (`KnowledgeReasoner`); splitting them meant paying
for identical weights in two containers, three sets of logs, three cold-start paths. The split
was historical, not architectural. The consolidated server (`engine/server.py`) serves
`/api/reason`, `/api/knowledge`, `/api/dimension`, and `/healthz` from one process with one shared
model instance. The stateless dimension endpoint keeps its own model and lock, preserving the
original concurrency semantics exactly.

## Names describe function, not lineage

Internal names carried generation numbers (`query14`, `server18`, `Query19World`,
`runtime20_model.pt`). All public names are functional: modules like `pg.py`, `entities.py`,
`world.py`; classes like `PgQuery`, `EntityQuery`, `KnowledgeReasoner`; artifacts like `encoder.pt`.
The complete old→new map is in [docs/notes/engine.md](docs/notes/engine.md). Class renames are
safe because all model artifacts are plain `state_dict`s, not pickled modules (verified by
loading the shipped weights into the renamed classes).

## Clean API break, no legacy aliases

Old endpoints (`/infer-runtime20reason` etc.) are gone, not aliased. The frontend lives in the
same repo and was updated in the same change; keeping aliases would only preserve confusion.

## Dropped rather than carried

- Three unreachable frontend pages and two rewrites to long-dead backend services.
- A stub Cloud Functions codebase (all logic lives in Cloud Run) and its committed venv.
- Superseded model generations loaded at startup and then immediately overwritten by the
  unified-encoder overlay (~140 MB of weights that did nothing).
- A 34 MB SQLite fallback (`words.db`) whose every call site is overridden by the Postgres
  executor on live routes.
- Per-service bundle directories that duplicated the entire package and its weights (~327 MB).

## One foreign-key detector

Foreign-key discovery had drifted into two implementations: `engine/relations.py:discover_fks` (the
principled inclusion-dependency detector — a child column references a parent when the parent is a UNIQUE
key and the child's values are included in it, boosted by name/type agreement) used by the typed-AST
planner, and a second, conservative detector in `engine/joins.py` that gated on column-*name* matching and
fed the compose panel. They disagreed: the AST planner joined a **string** foreign key
(`orders.customer → customers.name`, a name — not a number), but the compose panel dropped it because the
column names differed, producing a derivation that summed the wrong column. A foreign key is a referential
inclusion, not a numeric type, so this was a bug, not a policy. `engine/joins.py` now delegates discovery to
`engine/relations.py` (one detector, shared by planner and panel) and retains only `join_plan` (compose's
fact selection and flatten-safe `keep` lists). The uniqueness and self-id guards live in one place, so the
two paths can no longer diverge.

## Private references use the own-data planner

Persistent product, SKU, region, and similar dimensions live in a per-user `m_<md5(sub)>` schema. They are
own-data relationships, not public-world grounding, so they do not have a separate SQL generator and the complete
master schema is not added to `search_path`. Before serving, `engine.master.relevant_tables` loads the authenticated
user's saved dimensions, validates connectivity with `engine.relations.discover_fks`, follows direct and multi-hop
links to a fixed point, and converts only bounded relevant tables into the planner's normal typed table form.

This materialization was chosen over a three-schema `search_path` because AST search needs the same bounded rows and
values used for schema linking and FK discovery. Selecting in one place keeps planning and execution aligned and
prevents unrelated cross-conversation references from entering a request. The browser saves dirty references before
submitting; a failed save aborts the turn rather than falling back to a stale persisted copy.

## Calculation correctness is verified after ranking

Candidate scores are preferences, not answer-validity proofs. Calculation families therefore implement
one registry contract: detect an explicit intent, bind typed operands, propose an AST expression, and
verify the completed AST after ranking. `engine/calculations/` owns this contract. It chooses the first
ranked candidate satisfying every detected calculation and clarifies when none is admissible.

The shared Qwen encoder may order already eligible operand bindings, but it cannot authorize an
identifier as a measure, invent a join, choose rate units, or certify an answer. Those decisions remain
deterministic and inspectable. Verification preserves every set-operation branch and requires the same
typed expression on every branch. Currency conversion, ratio-of-sums, and flat tax/commission/explicit
one-year simple-interest application are registered specifications; piecewise schedules and unbound
temporal rates abstain. This separation keeps recognition, search preference, and answer admissibility
independently testable.

## Compound questions use one bounded proposal and one executable DAG

Complex requests such as “rank products, rank customers, then find products those customers have not
bought” are not forced into a fake linear `combined → enriched → calculated` chain. They contain
independent relations and a real dependency graph. The ordinary typed-AST planner is still tried first.
Only when the selected query is compound may the orchestrator retry once with two to four
natural-language leaf questions and a closed `cross`/`anti_join` topology.

This is deliberately neither eager chain-of-thought planning nor recursive execution. Eager
decomposition would pay for and trust a model on simple questions. Recursive decomposition would make
control flow, cost, and termination depend on repeated model judgments. Here the model proposes only
the semantic split after deterministic evidence says it is needed. Runtime code enforces one retry,
the unchanged question/workbook identity, full output reachability, and bounded Cartesian products.
The existing planner binds each leaf to real schema, compiler code infers merge keys from typed outputs,
and one immutable DAG emits both SQL and human-readable Python. Intermediate rows never return to the
model to drive another computation; SQL/Python parity is checked at every materialized DAG node.

## Config is environment-only

The old code defaulted to a hardcoded Cloud SQL IP and a hardcoded Firebase RTDB URL. All
connection and behavior config now flows through `engine/config.py` from environment variables
(see `.env.example`). Two deliberate deltas: `KB_PG_SSLMODE` defaults to `prefer` so local
Postgres works (set `require` for Cloud SQL public IP), and trace streaming is a clean no-op
when `RTDB_URL` is unset (the frontend already falls back to full-JSON responses), so the
system runs without any Firebase RTDB at all.

## Currency is knowledgebase data, not an uploaded sheet

All knowledgebase data is temporal; only the sync frequency differs (ECB rates daily, city
population yearly). The join is always the one three-way shape — conversation schema + tenant
schema + knowledgebase tables — so exchange rates are a world table
(`knowledgebase."exchange_rate"`, built by `db/sync/build_exchange_rate.py` from the pinned ECB
release), joined by value on `(currency_code, date)` for dated fact tables and pinned to `as_of`
for undated ones. No attach/enrichment side-channel exists, and no rate-date policy question is
asked: the join keys decide. An uploaded rate sheet wins where it overlaps but cannot veto
knowledge it does not cover. The projection carries each active series forward a bounded
`CARRY_FORWARD_DAYS` past today (the weekend rule generalized to holidays and sync lag);
past the bound the coverage check declines rather than converting at an arbitrarily old rate,
and `updated_at` always keeps the true publication date. The `<service_name>-ecb-rates-refresh`
Cloud Run job (`infra/main.tf`) rebuilds the projection daily from the same immutable engine image
the service runs.

## The database contract is now explicit

The world database schema had accreted across generations of setup scripts; no single file
described it. [db/init.sql](db/init.sql) is now the reconstructed, idempotent contract
(extensions, `words` + HNSW index, world tables), and [db/README.md](db/README.md) documents
which projections each sync step pre-seeds. Notably, geo queries are plain-SQL haversine —
no PostGIS — so the stock `pgvector` image suffices.

## Serving is read-only on shared facts; request-time Wikidata fill removed

Superseded decision, kept for the record. Through 2026-09-03 the entity path lazy-filled
`knowledgebase."city"`/`"country"`/long-tail tables from Wikidata at request time
(`engine/knowledge_sync.py`, deleted). When the serving role lost schema write access
(2026-08-27, `permission denied for schema knowledgebase` on every resolving world query),
the writes were routed through three admin-owned SECURITY DEFINER functions with pinned
`search_path`, `format(%I)` identifiers, and a qid-PK `ON CONFLICT (qid)` arbiter, granted
EXECUTE-only to serving.

The 2026-09-04 release-gate rerun as the real serving role exposed the deeper problem: a
request could stall on WDQS network fetches and return flaky undercounts, contradicting the
deterministic-source contract. Request-time fill is now removed entirely. `knowledgebase."city"`
and `"country"` are rebuilt offline by [db/sync/build_qid_world.py](db/sync/build_qid_world.py)
from the synchronized `public.settlement`/`public.country` staging tables; a resolution miss
abstains instead of fetching. The definer functions' SQL remains in `db.sync.app_migrations`
only because migration checksums are immutable; `db.reference_grants.apply_shared_read_boundary`
revokes their EXECUTE and audits that serving keeps zero write paths into shared facts.

## Reproduction honesty

`training/` reproduces the shipped model from the published checkpoints and documents precisely
which early-generation corpus artifacts would be needed for a true from-scratch retrain (they
are data artifacts, not code, and are stated as such rather than papered over).

## Fresh git history

The predecessor repos' histories contain hardcoded infrastructure IPs, committed virtualenvs,
and large binaries. This repo starts clean; the private repos remain as archives.

## License

Apache 2.0 — the patent grant matters for research code intended for broad reuse.

## The generalized Schema.org head is the active class-routing vocabulary

The retirement gate recorded here on 2026-08-20 has been completed. `engine/router.py` now consumes
the URI-indexed Schema.org property head and calibrated class signatures; it no longer executes the
old nine-family property-consensus decoder. The shared Qwen/LoRA encoder and historical allocation
remain because structural intent, ranking, calculation retrieval, and the generalized head use that
representation. They are not a second class-routing owner.

The promoted v2 class model is a deterministic logistic superposition of surfaced property
probabilities and signed weights. It represents every Schema.org 30.0 class, releases only classes
that pass validation-only selection and untouched-test gates, and abstains everywhere else.

The model's authority remains deliberately narrow:

* a released class may propose a coarse resolver family;
* ontology inheritance defines that class-to-family mapping;
* exact source-key grounding authorizes the world join;
* deterministic membership fallback preserves grounded coverage when the model abstains; and
* typed planners and calculation specifications remain the only owners of SQL and arithmetic.

This separation lets new publisher observations expand named Schema.org coordinates without adding
another hand-built family model, while preventing a plausible learned classification from granting
access to unrelated source facts.

## The engine client is async, and the orchestrator calls it in-process (2026-09-07)

`mcp_server/engine_client.py` became async-first: `call_query`/`call_describe` are coroutines
awaited by BOTH entry points — the chat orchestrator directly in-process, and the standalone stdio
MCP server for external clients. The orchestrator previously spawned `python -m mcp_server.server`
per chat turn purely to relay the same HTTP call; that cost a measured 0.86s of interpreter startup
per turn and carried the user's token through process-wide env, which becomes cross-user shared
state once the caller serves concurrent turns in one process. Identity is now an explicit `token=`
argument on every orchestrator call; the env fallback remains only for the standalone stdio server
(one client per process). The explicit-token-beats-env contract is pinned by `tests/test_mcp.py`.

This is an API-breaking change for any external PYTHON consumer that imported the previously
synchronous `call_query`/`call_describe` (they must now await them, or wrap with `asyncio.run`).
The MCP wire contract — tool names, arguments, and the JSON envelope — is unchanged, so MCP clients
are unaffected. All in-repo callers were converted in the same change.

## knowledgebase.words is indexed norm-leading (2026-09-07)

Serving resolution/routing/classification lookups filter `words.norm = ANY(...)` with type
constraints that are positive, NEGATIVE (`type NOT IN`), or absent. On PostgreSQL 16 a
type-leading index cannot seek for the latter two, and production — which had drifted from
`db/init.sql` and had NO norm index at all except the city-only partial — served every such lookup
as a parallel seq scan of the ~790MB heap (measured ~0.5s each, ~10 per request, 87% of server-side
SQL time). `ix_words_norm_type (norm, type)` replaced the declared-but-never-built
`ix_words_type_norm (type, norm)` in BOTH `db/init.sql` and knowledgebase migration v3 before
either was applied anywhere, keeping one index design. Measured after: the three hot shapes went
from ~500ms seq scans to <1ms index scans. Type-only lookups remain served by `ix_words_type_qid`.

## Round-2 latency: encode cache, streamed prose, bridge reuse (2026-09-07)

Three additions on the measured evidence of the first round's `[timing]` lines:

* `TableQuery._encode` holds a bounded per-instance LRU keyed on exact text. Measured before:
  49-70 texts per request, 19 unique — column names re-encoded 5-8x per turn and identically on
  every follow-up, 60-80% of production request time. A cached text returns the identical vector
  (strictly more deterministic than re-encoding under different batch padding).
* The orchestrator streams its rounds (`messages.stream`) and pushes the growing reply through
  `engine.trace.StreamBuffer` — coalesced FULL-STATE writes to the turn's `reply` node, ≥100ms
  apart, on a background thread; `close()` + the existing final `reply` emit stay authoritative.
  Full-state writes are the reconnect story: the node IS the state, no sequence replay. The
  /api/converse PRESENT reply is deliberately NOT streamed: the serving container speaks HTTP/1.0
  (no chunked transfer), the call is ~1s total, and flipping the whole server to HTTP/1.1 to shave
  ~0.5s off the last second is a bad trade. Revisit only alongside a broader server change.
* `_persist_connected` is gated by a per-conversation content hash (pairs + world refresh stamp +
  model revisions, ledger table `_bridge_state`): an unchanged bridge skips DROP/DELETE/INSERT
  entirely (resolution slides still stream), and a rebuilt one uses one paged `execute_values`
  instead of one INSERT per row. Statements slower than 150ms log a literal-redacted fingerprint.

## Per-tenant schema ownership follows the serving role (2026-09-07)

A production 500 (`master write failed: InsufficientPrivilege`) traced to the serving-role
migration: schemas created while the engine still connected as the admin role stayed ADMIN-OWNED,
so once serving switched to its least-privilege login it was denied `CREATE ON SCHEMA` there.
Measured impact: 78 live conversations across 6 users, and all 10 reference-data schemas — every
reference-table save failed. New schemas were unaffected, which is precisely why it survived every
release check: all of them created a FRESH conversation.

`db/reference_grants.py:adopt_legacy_tenant_schemas` is the fix and now runs with the other
boundary work on every bootstrap. It transfers only per-tenant namespaces (`c_<32hex>`,
`m_<32hex>`) and their tables; shared schemas stay admin-owned and read-only to serving. Because
PostgreSQL requires the admin to be a member of the target role to reassign ownership — and Cloud
SQL's `postgres` is not a true superuser — it takes that membership only when missing and gives it
back immediately. It audits that zero per-tenant schemas still deny CREATE, and raises if any do.

The testing lesson is recorded as a release gate in CLAUDE.md: a major release must exercise an
EXISTING conversation, not only a new one. `eval.txt` follow-ups per demo dataset exist for the
same reason — the first turn of a fresh conversation is the least representative thing to test.

## Two production defects the first Chrome dataset sweep found (2026-09-08)

The release gate added in 86f0598 was run for the first time and immediately caught a bug that
every previous check had missed.

**Saved reference data 500'd every request.** `enrichment/runtime.py:table_versions` versions every
uploaded and saved-reference table by content hash; planner cells are `Decimal`
(`tables._typed`); `artifact_provenance.canonical_json_sha256` could not encode one. A single
fractional cell in a user's saved reference table therefore made every request they sent fail with
`world request failed: TypeError`. Fixed in the hasher via `numeric.wire_value` — the repository's
exact-scalar contract, so equal numbers hash equally and precision is never invented.

Two process lessons are worth more than the fix:

* The first attempt patched the RESPONSE encoder on a plausible theory and was WRONG. The proof was
  negative evidence — the new `[serialize]` log never fired — which is why the log line exists.
  Diagnosis only became possible after running the engine locally under the `serving` role, because
  the admin role can no longer read tenant schemas (a deliberate consequence of the ownership fix).
* `world request failed: TypeError` named no location. The 500 handler now records the failing
  frames (positions only, never the message), and that pinned the caller on the first production
  request after deploy.

**Resolved (2026-09-08): the empty-value answer had two independent causes, both fixed.**
With a conversation-supplied EUR claim, "total budget in Germany in US dollars" returned status
`answered` with an EMPTY value. Reproduced locally only after driving the HTTP entry point with a
signed attestation header (the earlier harness never built production conversation state, which is
why "it could not be reproduced" — the harness was wrong, not the report).

* **Cause 1 — a synthesized column was routed as a world entity.** Dataset semantics materializes
  the claimed currency as a private constant column, every cell `'EUR'`. Value-membership routing
  resolved `'EUR'` to a CITY qid (Q3734597, in Italy) and built a world join on it, so the query
  filtered `city.country = Germany` against a city in Italy and matched nothing. Engine-internal
  columns are not user data: both route sources — `engine/entities.py:_value_membership_routes` and
  the learned map merged in `engine/knowledge_query.py:route` — now skip them via the one predicate
  `dataset_semantics.is_synthetic_currency_column`.
* **Cause 2 — an aggregate over zero rows was presented as an answer.** SQL returns a single
  all-NULL row for SUM/AVG/MIN/MAX over an empty relation, which rendered as `[['']]` with no
  clarify. That is what made cause 1 SILENT rather than visible, and it was reachable on its own:
  "total budget in Africa" on a dataset with no African row answered `''`. The gate is
  `knowledge_query.verify_nonempty`, applied on `KnowledgeQuery.serve` -- the ONE terminal every
  caller shares, since evaluators call it directly and compose keeps this delegate authoritative
  (a composed re-expression only stands when it reproduces the delegate's answer, so a delegate
  clarify propagates). It decides on the TYPED aggregate evidence the planner already publishes
  (`computation.branches[].outputs[].aggregate_functions`), not on SQL text. It fires only when every output is an aggregate and every cell is empty, so
  plain SELECTs are untouched and COUNT — which yields 0, never NULL — stays a real answer.

Two placement mistakes are worth recording, because both were caught by evidence rather than review:

* The gate first keyed on `column_provenance`, which `provenance.decorate_response` attaches AFTER
  `serve` returns — so it silently never fired. Dumping the actual pre-decoration payload, instead of
  reasoning about its shape, is what found it.
* It was then placed on `KnowledgeReasoner.serve`, which the HTTP path uses but evaluators bypass.
  `tests/test_datasets.py` calls `KnowledgeQuery.serve` directly and kept failing, which located the
  correct owner. `eval.txt` gained one non-numeric expectation, `=> clarify`, so the release gate can
  express "this must NOT be answered" — a numeric-only gate cannot catch a wrong blank.

## Named analyses share source tables and keep immutable revisions (2026-09-09)

A conversation has one set of uploaded source tables under their canonical CSV stems. `chat.working_table`
records deterministic hashes for the uploaded, private-reference, and enrichment tables currently materialized
in its PostgreSQL working schema, so unchanged tables are reused. Uploaded data and declared dataset semantics
advance the conversation's monotonic `dataset_version`; analysis revisions record that version, making
freshness independent of which question-local reference tables another analysis happens to select.

Each distinct result is an engine-owned `chat.analysis`; each successful create or modify is an immutable
`chat.analysis_revision` containing the exact SQL, rows, views, and provenance returned to the client.
The conversational model proposes `create`, `modify`, or `inspect` plus a slug, using the compact catalog
provided by the engine. It never assigns IDs, writes view names, authorizes access, or changes SQL semantics.
Derived wire names use `<analysis_slug>_<logical_step>`, while the browser continues to show concise logical
tab labels. A rail link includes the analysis id and revision and restores only that derived stack, preserving
the conversation's input and private-reference tabs.

This supersedes the browser-only policy where every follow-up marked one anonymous derived stack stale and
discarded it on the next result. Snapshot v1 remains readable for existing conversations; new snapshots are
v2 and include analysis identities. Remove v1 reading after the configured 90-day conversation retention
window has elapsed from the first release containing this migration.

## One named-analysis plan emits both SQL and readable Python (2026-09-09)

The typed SQL AST remains the only own-data planner. For the supported named-analysis subset, its
validated winner lowers into one immutable `AnalysisPlan`. Two deterministic emitters consume that
plan: one creates the named SQL view stack and one creates SQLAlchemy table classes plus a wrapper
whose method name is the analysis slug. This prevents either source language from becoming a
separate planner or a model-authored black box.

ORM foreign-key attributes are related objects. Their physical scalar columns remain private mapped
attributes, so `Order.customer_id` is a `Customer` while SQLAlchemy can still join through the stored
key. The ORM binds directly to the authorized PostgreSQL conversation schema and shared
knowledgebase schemas; production does not create a parallel SQLite schema.

`auto` execution uses Python for bounded small inputs and SQL above the configured row threshold;
`verify` executes both and rejects unequal normalized rows at the first mismatching named stage.
Production compiles generated Python as an ephemeral in-memory package. Development also replaces the exact
`engine/deterministic/_gen/py/<conversation>/<slug>` tree for inspection; production writes that tree
only when explicitly configured. Advanced AST and specialized world/compose shapes remain on their
existing SQL paths until both emitters gain typed support; no approximate translation is allowed.

## Explicit execution modes must report the backend actually used (2026-09-10)

The URL `use` parameter is request context, forwarded through the direct engine or chat adapter;
it never changes the process-wide default. Direct requests with an override receive a transient
`query` slug without creating a named workbook. Persisted analyses keep their engine-owned slugs.
The own-data and MCP response adapters preserve stage records and generated-source evidence.

An explicit Python or verification request cannot accept an answer produced by the unsupported
SQL path. The final server guard returns an error before saving such an answer. SQL and automatic
selection retain the broader existing planners until those planners emit the shared typed plan.
The guard is currently post-execution; route-specific capability preflight is still future work.

Shared plan validation and join/storage-attribute rules have one owner in `engine/deterministic/plan.py`.
Complete execution results are separate from 50-row trace previews. When the execution service owns
the PostgreSQL connection, both backends and stage reads use one REPEATABLE READ transaction.
Exact source hashes and backend agreement remain distinct from independent answer correctness and
from knowledgebase release pinning. See `docs/DETERMINISTIC_EMITTERS.md` for the current boundaries.

## Own-data queries are chosen by a fitted arbiter over search and proposer candidates (2026-09-23)

The own-data planner served the deterministic typed-AST search's hand-ranked top candidate. On Spider
dev (`whole_db`) that answered 365/1,034 strictly, while the search's pool held a correct query for
543: the grammar rules could not enumerate the missing shapes, and the ranking could not find the
ones it had. The served selection is now `TableQuery.select_query`: the search's candidates, plus up
to four beams from a LoRA-adapted Qwen2.5-0.5B proposer fine-tuned on Spider TRAIN gold SQL, executed
on an in-memory copy of the request's tables and chosen by a logistic arbiter over nine named
features. The evaluator measured that pipeline at 645/1,034 (62.4%) before it shipped; its served
measurement is in `spider/results/RESULTS.md`.

What stays true: every executed query is a typed AST the engine validated and rendered. Proposer text
passes the importer (`engine/sql_import.py`), the validator and the renderer, or it is dropped. The
choice is deterministic arithmetic whose per-feature contributions each response reports
(`planner.selection`). One function serves and plans decomposition leaves; the Spider evaluator, the
offline regression gate and arbiter training call it too, and the decomposition probe reads its
first stage (the search).

The arbiter learned Spider's conventions, not the product's, and three product contracts are
therefore constraints on its ranking rather than features of it. A named request is compound — and
requests decomposition instead of executing a fragment — when the search reads the question as a set
operation; the proposer only emits single queries. A named request that is not compound is served by
the best-ranked single query (one dual-emitter branch). A decomposition leaf reads single queries in
the arbiter's order, its choice first, each names-only ranking with its ORDER BY measure projected,
and serves the first reading that sums and ranks by the measure the question names at the ranked
entity's grain. All three mirror the existing calculation constraint: they filter the arbiter's order
and never rescore it. Spider evaluation has no analysis context, so none of them changes its numbers.

The first production flip of this release was rolled back after the live demo gate
(`tests.test_datasets`) found a regression the hermetic suites could not: compound structure was
also read from the arbiter's CHOICE. For "total amount for restaurants in United States" over a sheet
with no country column, the proposer's top beam was an `INTERSECT` over invented values, the arbiter
chose it, and every named request asked for a decomposition, so the world join never ran. Compound
structure is now the search's reading alone, and the compose path's probe runs only the search, which
also removes the proposer's decode (about 40 s on CPU) from every composed world question.

The Chrome release gate then found the leaf contract too weak in both directions. It accepted any
aggregate, so for "top 2 product category names by total revenue" a lower-ranked member that summed
revenue but ordered category-product pairs by product name was served, and the answer was wrong; and
it could not see that the arbiter ranked "top 2 customers by total spend" by units. The contract now
requires the named measure to be summed and, for a ranking, ranked by, and a leaf never needs a
lower-ranked member just to show a measure its choice already ranks by.

The same gate found an orchestrator defect older than this release: a model tool call without a
question reached the engine, and the engine's validation error became the user's reply. The
orchestrator now validates the question with the engine's own `validate_question` and returns a
malformed call to the model to repair, and its repair of a column written as the table now also
accepts a case-only difference ("Budget" for the header's "budget"), which had turned "This is in
euros" into a clarify. Capacity is the known limit of the CPU deployment: the engine
serves one request at a time per instance, a decomposition costs about a minute of proposer CPU, and
three concurrent complex questions exceeded the orchestrator's 240 s turn budget during the gate. The
lever is inference hardware, not the budget.

The live geo suite found the last one: for "total order amount in KWD" (a currency neither the sheet
nor the knowledgebase covers) the arbiter chose a beam that dropped the SUM and filtered
`currency = 'KWD'`, and the currency specification read any query without a value output as a filter
reading, so an empty table replaced the decline. Only the parser's row-selection intents are filters
now; an output-unit request is realized by a monetary aggregate or declined.

Retired rather than kept as alternatives (git holds them): the trained rank head and its search hook,
execution-feature reranking, the proposer-first policy, the two-proposer pilot layout (its arbiter
slots for the second proposer were constant with coefficient 0.0 and are dropped with bit-identical
scores), and value-linked prompts. `sqlglot` becomes a serving dependency. The proposer's beam search
dominates own-data request time on CPU; `DEVICE` places it on a GPU when one is provisioned.

## A LoRA adapter's identity is its model files; the Schema.org interpreter must load (2026-09-23)

From at least 2026-09-14, every production engine container logged `[knowledge_query] schema
interpreter unavailable: ValueError` when it first routed a table. `SchemaInterpreter` refused the
promoted head as "trained against a different encoder adapter". The encoder identity the head
records hashed every file in `engine/data/qwen_lora`. The machine that trained and promoted the head
held a stale `README.md` there, a PEFT model card from an earlier adapter, which
`weights_manifest.json` neither pins nor fetches, so no image could reproduce the identity. The
serving loader caught every exception, printed only its type name, and served without the
interpreter. Both the learned column router and table-level class evidence were off. The health
check, the release smoke and the live gates still passed, because exact source keys, not the class
model, authorize a world join.

A LoRA adapter's identity is now its model files: `engine/artifact_provenance.py:adapter_sha256`
hashes exactly `adapter_config.json` and `adapter_model.safetensors` and raises when either is
missing; `semantic_encoder_fingerprint` is the base-model pin plus that identity. Every adapter
identity uses it: the interpreter's check, Schema.org training and promotion, the unified-encoder
promotion (its inline copy of the formula is gone), the SQL proposer and its promotion (its private
file list and temporary-directory hash are gone), and the Spider evaluator's provenance. For a
directory holding exactly those files the digest equals the old whole-directory hash, so the
proposer/arbiter pairing and every identity recorded from a clean directory stay valid.

The promoted head's identity was re-recorded through `training/schema_org/promote.py`, the one
writer, which re-ran every gate: `c3f61d5e…` became `ea5bdbf0…`. The head weights (`cef8a43c…`),
thresholds and class signatures are byte-identical, and `schema_training_manifest.json` carries an
`identity_corrections` record. The correction is sound because the old whole-directory hash of
today's adapter files plus that README reproduces the recorded `c3f61d5e…` exactly: the head was
trained on these adapter files.

A silent downgrade can no longer ship. The interpreter is part of the bundle: `KnowledgeQuery`
loads it at construction, so a bundle it cannot load fails the container's startup probe, and the
in-image regression gate (`regress/run_regression.py:run_bundle_checks`) loads it at build, so such
an image is never pushed. The documented "degrades safely without the head" contract is withdrawn:
bundle validation already requires the head, so that path was reachable only through integrity
failures like this one. With the interpreter restored, production again runs learned column routing
and records class evidence; exact source keys still authorize every world join.

## Complete questions reach the engine as typed; a rejected dataset op is repaired once (2026-09-24)

The Chrome pass of 2026-09-24 found three orchestrator-side misses. A complete question asked after
related turns reached the engine with those turns' context appended: "How many payments are listed?"
as "... for PHOENIX SOFTWARE LTD", "What is the highest amount paid?" as "... to suppliers?", and
"total budget in Germany" with the currency of an earlier conversion. The engine reads the added words
literally, so each answered a different question. "only use the top 2 customers" sometimes changed
both cutoffs or made no call. In a reopened conversation "This is in euros. Whats in USD" ended on the
engine's validator sentence ("names a table that is not uploaded: 'budget'") as the reply. On the live
model, before any change, the first three shapes went through verbatim 0/5, 0/5 and 2/5 times, and
the cutoff shape was right 3/5 times.

Prompt rules 3 and 4 now draw the boundary explicitly. A message that says on its own what to compute
is standalone, also mid-conversation, and is sent as typed. Only a message that cannot be answered on
its own is rewritten, and the rewrite changes only what the message names. The tool's `question`
description says the same. One code guard enforces rule 3 for the shape the model kept producing:
when its question is the user's own words (three or more) with words appended, the user's words are
sent (`orchestrator._verbatim_standalone`). The guard is structural. It does not look at what was
appended, so it is not the currency-only guard the prompt-owned design rejected, and a shorthand
rewrite, which never starts with the user's words, passes untouched. After both changes each of the
five shapes, including the qualifier carry-over rule 4 requires, went through 8/8 times, and
`tests.test_orchestrator` asserts them against the live model.

The engine now marks a dataset-op rejection (`DatasetOpError`) as `dataset_ops_rejected`. The engine
client forwards it, and the orchestrator grants the model one `repair_required` round that lists the
uploaded sheets and their columns (`engine/dataset_attestation.py:uploaded_columns`, the one header
parser, which the column-as-table repair also uses). A second rejection is terminal. The engine
persists no op it rejects, so the retry replays nothing.

## A text literal must be grounded in the column it filters (2026-09-24)

The Chrome pass of 2026-09-24 served every product for "how about customers from Lyon?". The
orchestrator's leaf read "product names bought by Lyon customers". The SQL proposer, which reads the
schema and never the values, bound 'Lyon' to `purchases.customer_name`, and the arbiter ranked that beam
first. The deterministic search, which links values against the data, had bound it to `purchases.city`
in every one of its candidates.

A pool member is now eligible for selection only when it runs within the step budget AND every text
literal it tests with `=`, `!=`, `IN` or `NOT IN` is grounded: the literal occurs in its own column, or in
no column of the request's tables (`engine/sql_grounding.py`, called by `TableQuery.select_query`).
Matching folds case and whitespace. Aliases resolve to their tables, subqueries are checked in their own
scope, and derived-table columns are skipped. A literal that no column holds is left alone, because the
question may name a value the data lacks and the honest answer is then empty. Numeric, date and pattern
comparisons are out of scope. Eligible members keep their arbiter scores, so the choice changes only when
the arbiter's pick was mis-grounded, and `PoolSelection` records `grounded` next to `executable`.

When no member is grounded the selection is empty and the request is not answered, rather than served a
filter that answers a different question. Served Spider `whole_db` (`841f08c`) measured 647/1,034 strict
(62.6%) and 696 lenient against 645 and 693 before: 7 strict wins and 5 losses. All five losses are
flight_2, whose gold queries return nothing because of leading spaces in its codes, so a mis-bound filter
that also returned nothing used to score as correct. Three flight_2 questions have no grounded member and
now raise. Preferring grounded members only when one exists would win those artifacts back and serve
mis-bound filters to users. `spider/results/RESULTS.md` has the transition matrix.

## A decomposition keeps only the cutoffs its question states (2026-09-24)

The second Chrome pass of 2026-09-24 served 14 customer-product pairs for "List the product names that
no customer from Paris has bought". The model decomposed it as Paris customers crossed with products,
minus purchases. The engine rejected that because cross inputs need explicit limits. The model then
resubmitted with "the top 100 customer names from Paris" and "the top 100 product names", and the
engine compiled it. The limit exists to bound the Cartesian product, and the model met it by inventing
a cutoff the user never asked for.

`engine/decomposition.py:build_decomposed_plan` now takes the decomposed question and rejects any leaf
that keeps N rows (N other than 1) unless the question states N in digits or words, parsed with the
planner's own `parse_number`. One row is exempt because it is the planner's reading of a singular
superlative ("the best-selling product"). The rejection reaches the model through the existing bounded
repair round, and it says that a question naming no cutoff is not answered by crossing two lists. The
cross-limit message now says the limits must be stated by the question. The shipped complex fixtures,
whose cutoffs are all stated, compile unchanged. The wrong pair shape itself is rare: 42 of 42 stubbed
proposals for this prompt used the correct anti-join. This rule makes the rare case end in a repair or
a clarification instead of an answer to a different question.

## A re-asked analysis is recalculated, never repeated (2026-09-24)

The existing-conversation half of the second Chrome pass of 2026-09-24 re-asked follow-ups in
conversations that had already answered them that morning. For "total amount in Belgium in US dollars"
and "What is the highest Net PO Value?", the model repeated the earlier reply and made no engine call.
The Belgium reply used the morning's exchange rate (367.4342), where today's rate gives 366.0174, and
neither reply had a workbook step.

The orchestrator already treated such a message as a recalculation: `_recalculation_target` matches a
message that restates a catalog analysis's question and pins the engine call to that analysis. It only
applied when the model chose to call the tool. `_run_turn` now enforces that call.

The first version (`dedfb27`) added a correction note when a recalculation round ended with no engine
call. The re-check on that release found 7 more of 50 re-asked turns answered from memory. Six were
short questions ("minimum notice_days", "total quantity for VIP customers") that the catalog does not
match: its match needs six words, and its latest question is often the model's paraphrase or a later
question on the same analysis. In the seventh, the model ignored the note. Two changes followed:

- A message that repeats one of the user's earlier messages word for word, and is answered with a
  number, also counts as a recalculation. The number keeps a repeated "thanks" from being turned into a
  query.
- The correction round forces the `prereasoner_query` call with `tool_choice`. The API cannot force a
  tool call with thinking on, so that single round runs without thinking. A probe against the API
  showed that the presentation round afterwards works with or without thinking.

The note never reaches the saved transcript, which keeps only the user's words and the final reply. A new
message that is answered from the conversation ("what was the minimum you told me?") keeps its reply.

## The chat service verifies sign-in; only the engine resolves the storage principal (2026-09-24)

`c8533ca` (Excel add-in) made the storage principal stable across sign-in providers. It is now mapped
once from verified claims into `chat.auth_principal`: existing Google accounts keep their Google subject,
and accounts without Google use their Firebase UID. `engine.auth._verify_principal` resolves that mapping
in Postgres, and the chat server called it on every turn. The chat image ships no database driver and no
`engine/pg.py`, and the chat service has no Cloud SQL connection, so every chat turn would have failed
with "sign in required". The release review caught this before deploy.

`engine.auth.verified_identity` now only verifies the token and returns the Firebase UID and Google
subject. The chat server authenticates with it alone, and only the engine resolves the storage
principal. Dataset attestations are keyed by the verified Firebase UID rather than the storage principal:
both services derive the UID from the same token, and the chat cannot resolve the storage principal
without a database. Giving the chat service database access, or an engine lookup per turn, was rejected.
The first widens a service that has no database privileges today, and the second adds a request to
every turn.

## Grounding checks both operand orders and keeps exclusions (2026-09-25)

A review of `engine/sql_grounding.py` found two gaps. First, it bound only `column = 'literal'`, and the
production importer accepts `'Lyon' = customer_name` from the proposer, so the reversed form escaped the
check. Second, the policy for `!=`, `<>` and `NOT IN` was not stated where the rule lives. An interim
change (`db9a1d8`) stopped checking exclusions. The owner chose to keep checking them: excluding a value
its column never holds excludes nothing, which is the Lyon mis-binding in negated form. The module now
checks `=`, `!=`, `<>`, `IN` and `NOT IN` in either operand order, and states the accepted cost. When two
columns share a domain and the question names the one that lacks the value, that reading is ineligible.
The tests pin that case alongside the contrasting ones.

The served Spider result stays 647/1,034. The exclusion policy is the one measured at `841f08c`, and the
reversed-order check only adds bindings, so a member can only lose eligibility. A dev answer can change
only when its selected query contains a reversed text-literal comparison, and none of the 1,034 selected
queries does, in either recorded run. `LIKE` remains unchecked; extending the rule to it could change the
11 dev questions whose selected query uses `LIKE`.

## One number-format rule for the Excel add-in and the workbook upload (2026-09-25)

The Excel add-in (`c8533ca`) counted a format as a date when it contained a date letter anywhere. So
`#,##0.00;[Red]-#,##0.00`, `[$USD] #,##0.00` and `[Blue][>=100]0` turned amounts into dates (1,234.50
became `1903-05-18T12:00:00Z`), and `[h]:mm` durations became timestamps. The web upload decides dates
with SheetJS, which skips bracket sections, but it also read `[h]:mm` as a date: 27:30 became
`1900-01-01T03:30:00`.

`web/public/lib/number-format.js` now owns the rule for both importers. It keeps only real date and
time codes, dropping quoted text, escapes, padding and bracket sections other than `[h]`, `[m]` and
`[s]`. An elapsed duration keeps the day count Excel stores, in both importers. The upload keeps cell
formats (`cellNF`) and returns elapsed cells to that count. SheetJS's 1900-02-29 shift is undone, and
the 1904 date system has no shift. All 9 shipped workbooks convert to byte-identical CSV.

## A money noun that names its table asks for that table's money total (2026-09-25)

"Whats the sales in france" on a sheet named `sales` (columns including `amount`) answered 5, the
number of orders. On the world path the intent head read COUNT, and for "total sales in France" it
read SUM, but the table-noun rule in `EncoderQuery.read_op_all` turned both into `COUNT(sales)`
(regress B6). The own-data search deliberately kept a money noun that names a table as the entity, so
"what's the sales in London" listed rows. The owner decided the everyday reading: a money noun
(`sql_expansion.MONEY_MEASURE_NOUNS`) that names the table reads as its money total, `SUM` of the
table's money-named column (`MONEY_MEASURE_COLUMN_WORDS`), unless the question counts it ("how many
sales", "number of sales") or lists it ("list", "show", "display the sales").

One predicate serves both planners, `sql_expansion.money_total_position` / `money_total_columns`.
The world operand choice applies it whatever the intent head reads. In the own-data selection it is a
contract after the calculation intents: the best-ranked pool member that aggregates a money column is
served when one exists, otherwise the ranking stands. A converted total (`SUM(amount * rate)`) satisfies
the contract, so a currency intent's choice is never displaced. A money-named table with no money column,
an entity noun ("total customers") and a column literally named for the measure keep their readings.

No Spider dev database has a table named with a money noun, so none of the 1,034 dev questions can
trigger the rule and the served result is unchanged (spider/results/RESULTS.md).

## The Google Sheets add-on renders the web rail as a shared component (2026-09-25)

The add-on had its own sidebar (`Sidebar.html`, `Previous.html`) with its own step labels (in the sidebar
and again in `Code.js`) and its own CSV conversion. It followed the analysis by polling the database's
REST API. So it drew answers differently from the web rail and showed progress late. The owner asked for
the web app's sidebar as a component inside the add-on, styled like the add-on.

A same-day attempt framed the whole web page (`/embed/sheets`, `lib/host-bridge.js`) and shipped as Apps
Script version 22. It put the web's header, drawer and workbook inside Sheets. Version 23 replaced it, and
the embed was removed.

- `Sidebar.html` is the add-on's own UI with Google's add-on CSS. It loads the web rail's rendering from
  chat.prereasoner.com: `lib/turn-renderer.js`, which now owns the step presentation that lived in
  `workbook.js` (step names, sentences, live status, lineage, backend badge, "read as" line,
  `stepsFromViews`). `workbook.js` and the sidebar both call it.
- Live progress uses the web's own subscriptions (`lib/firebase-init.js` `subscribeTurn` /
  `subscribeRun`, imported as a module; `/lib/**` is served with `Access-Control-Allow-Origin: *` for
  that). The sidebar signs in with the Google token from `Code.js` (`signInWithHostToken`), so each step
  appears as it finishes and the reply streams as it is written.
- The sheet is read with the upload importer on the sidebar page (`WORKBOOK_IMPORT.convert`, which the
  upload worker also runs), so the add-on and an upload of the same cells produce the same tables.
- `Code.js` returns cells and makes the Prereasoner calls server to server, as before. The chat service
  accepts browser requests only from its own origins, so the add-on keeps `script.external_request` and
  its URL allowlist. `/chat` goes directly to Cloud Run for its 300-second timeout, and the steps return
  without their rows.
- The sidebar state saves version 2 (the shared steps). Version 1 states from before still render.
- The data-use notice the OAuth verification describes stays word for word before the first question.
- This supersedes the Sheets sidebar parts of `docs/EXCEL_COPILOT_PLAN.md`. The Excel add-in keeps its
  own task pane.

## One import rule for uploads and host grids (2026-09-25)

The Excel add-in (`web/public/office/excel/host.js`) and the Sheets add-on (`sheets-addon/Code.js`) each
converted cells to CSV with their own header rule, and named a populated column without a header
`column_N`. In the owner's screenshot an inserted index column had shifted every header one to the left,
so "amount" labeled the currency codes and the amounts had no header, and the add-on answered from those
labels.

Both hosts now send grids to the upload worker (`web/public/lib/xlsx-worker.js`). A grid carries values,
number formats, failed-formula flags, merged ranges and the date system. `WORKBOOK_IMPORT.gridWorkbook`
writes the grids as the `.xlsx` those cells would export to, and the worker applies the upload's read,
limits and `normalize` rule, and every error names the worksheet. Host grids and an upload of the same
cells produce identical CSV and import metadata in both date systems (`web/tests/workbook_import.test.js`).
Both add-ins check the upload's per-worksheet limits (10,000 data rows, 256 columns) before they read a
tab; they used to cap the rows of all tabs together at 10,000, which the server does not.

A column with values but no header is not a field, so nothing is named for it. It is left out, the import
records it (`import.leftOutColumns`), and the Sheets sidebar and the upload preview say which column. A
header row may therefore name two thirds of the columns, as when row numbers sit in an unnamed column A.
Refusing the whole sheet for one unnamed column stopped the owner's sheet from working once its headers
were fixed. One layout is still refused: only the last column unnamed, holding numbers, while the header
before it sits over text. That is a header row one column to the left of its data, and every answer would
read the wrong column. The message shows the evidence and the fix: `Column H has values but no header, and
the headers look one column to the left of their data (G1 "amount" is above "GBP"). Put each header above
its data.` An unnamed cell under a merged header remains ambiguous, and the 9 shipped workbooks convert
unchanged.

## Grammar words carry no aggregate and no search (2026-09-27)

"who ordered a trench coat in France" was answered COUNT = 5: the operator readout fires COUNT above
0.05, and the article "a" read 0.15. "everything in France" went to the semantic search, which ranked
France's orders by similarity to the word "everything". Both read meaning off closed-class words.

`engine/closed_class.py` owns the engine's one spaCy model and the closed-class reading of a question:
the words its tagger reads as determiners, pronouns, adpositions, auxiliaries, conjunctions, particles,
interjections or punctuation in that sentence. The operator readout ignores them, the semantic search
never searches for them, and the coverage check never reports them dropped. The tagger reads context,
so no word list has to track the grammar. Negation and exclusion cues are closed-class but carry a
constraint, so they are never ignored, and no semantic search answers a question that uses one ("orders
in France without a trench coat" asks to clarify). A question that asks for a computation ("highest",
"how many") is never a semantic search either. When the sheet's free text mentions every content word,
the search is literal ("trench coat" returns order 109 alone); cell values the question quotes ("Paris",
"Gold") filter exactly.

## A one-sheet trail starts at its lookup and shows labels while running QIDs (2026-09-27)

A one-sheet world trail opened with "Combined into one table · from sales", a copy of the upload that adds
no column and drops no row (docs/SHEETS_AS_REASONING.md rule 3). Hiding that sheet was not enough: the
lookup's own SQL and Python read it, so the trail would name a relation it does not show (rule 2).
`AnalysisPlan.inlined_entries()` marks a one-table entry with one consumer, and both emitters read it
inside that consumer: the SQL selects from the entry's `SELECT` as an inline subquery, and the Python stage
opens with the table's ORM load. Both programs declare the same stages, the first sheet names the upload
as its source, and a combined view that joins two or more sheets stays a stage.

Reference columns store QIDs, and the lookup, filter and Result showed `Q142` and `Q90` (rule 5). After
both programs ran and agreed, the deterministic service labels the rows it displays:
`AnalysisPlan.reference_columns()` traces the columns that carry a knowledgebase value unchanged, through
projections and group keys, and `EntityQuery._qid_labels` resolves them. The one executor every served
plan passes through (`pg._execute_deterministic`) refuses a knowledgebase plan without that resolver, so a
new world trail cannot skip it. The executed SQL and Python keep the stored literal, and an uploaded `Q1`
is never labelled.

## A world projection is a grouped total, and a binary float is read as NUMERIC (2026-09-27)

Every question that names a world column ("which continent has the highest total amount", "average atomic
mass") failed on the served path: the world path refused any projection before lowering it, and the live
suite passed only because it served without an analysis context. `lower_world_query` now takes the typed
projection binding the world path already selected, the world attribute (`dimension`) and which end of an
ordinal ranking to keep (`order`), and ends each shape in the own-data and compose grammar: a grouped total,
with a top-results sheet for a ranking and a second count for "how many". The answer carries its measure
(`Asia, 310`; `Asia, 1`), as compose and own-data answers do.

Two defects surfaced under it. The world path keyed every reference table by `qid`, but elements and states
are keyed by `name`; each table is now keyed by the column its join lands on, and the bridge joins on
`world_key` unless that key is `qid`. And the generated ORM read a double precision column through
`Numeric(58, 20)`, so 1.008 arrived as 1.00800000000000000711 where SQL read 1.008, and Postgres sums floats
with rounding a decimal sum does not have. `reference_schema` marks a double precision column
(`ColumnSpec.binary_float`), and both programs read it as NUMERIC at the source: `CAST(... AS NUMERIC)` in
the SQL lookup and a `NumericFloat` column in the ORM. Every later stage is then exact decimal arithmetic on
identical values.

## One surrogate-key rule (2026-09-28)

Eleven copies of "is this column a surrogate key" had drifted apart. Compose's regex matched `order_id` but not
`order ID`, so the customer-orders sheet offered its order numbers as a measure, and "orders for a brass
magnifying glass in france" summed them. The ranker's regex called `paid` and `uid` keys. The recall modules
counted codes; the search did not. `engine/sql_schema.is_surrogate_key` is now the only rule, read by the
planner, the ranker's features, compose, the world path and the deterministic lowering: the last word of the
name is an identifier word (id, ids, uid, uuid, guid, identifier, key, pk), or the whole name is `index`. A code
(`country_code`) is a natural attribute people ask for by name, not a surrogate key.

The ranker's features are unchanged on every Spider dev column name, so the fitted arbiter sees the inputs it
was trained on. Only three recall modules change on dev, on 16 code-named columns in nine databases. Of those
databases' 551 questions, 539 build byte-identical search pools. The served selection changes on 2 of the other
12 (wta_1 #451, tvshow #629), both wrong before and after.

## World questions have one implementation: the served one (2026-09-28)

Production enters an analysis context for every request (`engine/server.py`), so world questions always ran
through the shared deterministic plan. The world owners still kept a second, context-less SQL implementation:
`engine/knowledge_tables.py`'s plain-SQL branch and hand-built currency trail, and `engine/knowledge_query.py`'s
non-geo VALUES-relation trail, each with its own QID labelling. Most live world suites and both regress world
goldens exercised that branch instead of the served one. Every live suite and golden now serves through
`regress.live_schema.served`, which enters the context the server enters, and the context-less branches and
`EntityQuery._labelize_qids` are deleted. A world request without a context fails loudly.

Serving the suites as production does exposed two gaps in the served trail, both fixed in the deterministic
service: a filter sheet's label did not name its condition (`where country = 'France'`, docs/SHEETS_AS_REASONING.md
step 4 and rule 5), and recorded sheets carried Decimal and date cells that the server converted and logged
as leaks. It also exposed a production answer the context-less golden had hidden: a population threshold
("total sales in big cities with population over 1,000,000") returns a compose table instead of 350 or a
clarify. That is a separate task.

## A word that names a world type is not one of its members (2026-09-28)

"how many countries are the customers in" answered 0. For a phrase that names no entity exactly,
`EntityQuery._resolve` takes the nearest entity by embedding when it is at least 0.80 similar. "countries" sat
0.803 from China, so the question filtered to China and counted no customers. "which countries" sat 0.807 from
the United Kingdom, and "total sales in European countries" filtered to China as well. A phrase whose head word,
in singular form, names a world type (`knowledgebase."words"` rows of type 'type': country, city, state, ...) is
that type, and it no longer reaches the embedding fallback. Exact names still resolve first ("Mexico City",
"China"), and a qualifier that is its own phrase still resolves ("Chinese" in "Chinese cities" is China).

"states" is also an exact alias of the United States, so "how many states are the customers in" still filters
to it. Telling the alias ("sales in the States") from the common noun needs the phrase's grammar, not a word
list.

## A comparison binds the attribute it names (2026-09-28)

"What is the total sales in big cities with population over 1,000,000?" was served as an empty table of
cities. Compose bound every comparison to the aggregated metric (`_having`), so it applied the million to each
city's sales total, and it grouped by every text column the question mentions, so the cities became a
grouping. The delegate's world path binds equality filters only, and it declined the question.

Compose now binds a comparison to the column named right before it ('population over', 'a population of over',
'whose mass is above') or right after its value ('more than 1 million population'), and reads scale words
('1 million'). A comparison on a row attribute other than the aggregated measure keeps rows before the
aggregate. One on the measure, or one that names no column, still thresholds the aggregate ('cities with total
sales over 100'). The noun such a comparison qualifies ('big cities with ...') is the set the aggregate runs
over, not a grouping, unless the question groups it ('by city') or asks for no aggregate ('which cities have
...', which lists them). A world attribute the upload lacks, compared this way, is a necessary world
dependency (`world_dependency['world_threshold']`), and `route()` gives the question to compose. The answer is
350, and both programs compute it through the shared plan. An own-data comparison on a column other than the
measure ('how many orders with amount over 100') no longer has a composition step, so it goes to the typed
AST, its owner.

On the delegate path, "What is the total population?" summed `knowledgebase."city".population`, which is
stored as text. The Python program concatenated the digits into 1426479827518622326844519127886040 and served
the result. This was a regression from the world-measure lowering of 2026-09-27. `AnalysisPlan` now refuses
SUM, AVG and window totals over a column stored as text, whether read directly or carried through views,
before either program is emitted. The question is declined, as it was before that lowering. Answering it needs
a numeric population column in that table.

## A world measure reads the type its maintainer declares (2026-09-28)

`db/sync/build_qid_world.py` maintains `knowledgebase."city"` and `"country"` (`db/sync/schedule.py`), and it
already declared `population bigint`. The live tables had been pre-created with all-TEXT property columns, and
the builder's upgrade step only added missing columns. So population stayed text through every rebuild, and
the delegate's world measure had no number to sum. The fix is at the maintainer, not at the reader: each
rebuild now converges a pre-created table to the declared column types, on the emptied table, before any row
is inserted. `world_target` keeps binding `"city"`; binding compose's `"Cities"` would have given the delegate
a second source for city attributes and left `"city"` out of its own contract. The plan still never casts, and
SUM or AVG over text stays refused.

The rebuild ran on the live world database on 2026-09-28, with approval. It re-derived the same 200,886 cities
and 209 countries from `public.settlement` and `public.country`, so 0 rows changed besides the type and
`updated_at`. "What is the total population?" over Tokyo, Osaka, Nagoya, Lyon and Marseille is now 20,748,671,
the same in both programs. The same question over France, Germany and Japan is 275,984,756. `"u_s_state"`
has no population values at all, because its builder never writes that column; typing it would not answer
anything.

## A noun an aggregate runs over is not a grouping (2026-09-28)

Compose grouped by every text column a question mentioned (`_dims`), so "What is the average population of these
cities?" and "What is the total population of these cities combined?" were served as per-city tables. Both
capability cases failed on every run. The task-8 rule already treated the noun a row threshold qualifies as one
set. `ComposeEngine._aggregated_over` now generalizes it. When the question asks for an aggregate, a text column is
not a grouping if every mention of it is either the object of a domain word (`of`, `in`, `for`, `from`, `among`,
`across`), reached over determiners and at most one other word, or the noun a row threshold qualifies. A grouping
cue ('by city', 'per city', 'each city', 'for each of the cities'), a ranking word or a number ('the top 3 cities')
keeps the mention a grouping. So does any other position ('which cities', 'city totals'), a question that asks for
no aggregate ('which cities have ...' lists them), and a threshold on each group's aggregate. The scalar measure is
not a composition, so it goes to the world path, which reads the typed population (the entry before this one).

The world path then declined the "combined" question for having dropped the word "combined". An adverb that asks
for a total ("combined", "altogether", "overall") is now one of the operator words (`knowledge_query._OPERATOR_WORDS`),
as "total" is.

## Compose's candidate compares numbers as numbers (2026-09-28)

Compose materializes its candidate in SQLite before routing. `filter_view` wrote a number bare, and a view's
decimal aggregate (`decimal_sum`) is TEXT with no affinity or collation. SQLite orders every TEXT above every number,
so "cities with total sales over 100" kept all five cities and "under 50" kept none. Served answers were right,
because they run the shared plan in Postgres. But routing (`_composes` reads the result rows) and the
re-expression of a world-filtered scalar (`_same_answer`) read this answer, and so do hermetic tests. `filter_view`
now compares a number through the registered `decimal_cmp`, whatever the column holds: a decimal aggregate, a
count, a stored decimal, a year. A text value keeps its text comparison. A cell that is not a number fails a
numeric comparison instead of passing every 'greater than'. Ordering already used `COLLATE decimal`.

## A currency named as the output unit is not also a row filter (2026-09-30)

Production, customer-orders: "total amount in Europe in GBP" answered £810. The plan joined the rates and
converted to GBP, and it also kept only the rows whose currency was GBP, the five London orders at rate 1. The 13
European rows (810 GBP, 322 EUR in Brussels, 970 EUR in Paris) come to 1,917.48 GBP. Every "in <ISO code>" total
over a sheet that holds that code did the same: "total amount in Belgium in USD" kept no rows at all. The world
path already claimed the conversion phrase from the world filters ("in US dollars" is not the country United
States). `_own_value_matches` still read the whole question, so the code inside the phrase matched a cell of the
currency column. It now reads the question with the conversion phrase claimed. A currency named outside the
phrase still filters ("GBP orders in Europe in USD"), and a COUNT "in GBP" is still a row selection.

The currency verdict (`engine/calculations/specifications.py`) called that plan satisfied, because every branch
applied the typed rate. A branch that also keeps only the rows already in the target realizes one phrase twice,
and it is now the filter reading, which is ambiguous. Selection takes the best-ranked query that converts every row.

A value the upload holds is claimed by the upload before the world resolver reads the question. "how many orders
in GBP" counted 0: "GBP" is in no alias list, and the resolver's embedding fallback read it as Guinea-Bissau. It is
now the currency filter the sheet answers, 5.

## A place noun names the rows, not the answer (2026-09-30)

The assistant's accepted offer, "total amount for all European countries in GBP", made "countries" the world
column to project. The world path returned DISTINCT countries instead of the total, and the currency verdict
declined. A named TEXT world column (`_world_word_is_output`) is the answer only under a cue that puts it there:
which/what, by/per/each/every, list/show/name, a ranking word, an aggregate word right before it ("What is the
total currency?" lists each currency with its row count, as before), or, for a COUNT, a count cue that governs it
("how many countries"). "how many orders from European countries" counts orders, 13.

A place is read as one name. The coverage gate (`KnowledgeQuery._uncovered`) judged places word by word and
declined correct plans: "united" in "the United Kingdom" surfaced another country, "north" in "North American" a
town called North, and "European" Germany (Q183, 0.75). A span of one to three words that names a qid the SQL filters
on now covers its words, and a demonym is its place plus '-n'/'-an'. "the United States in USD" had passed only
because "united" and "states" are USD's own currency words.

The resolver (`EntityQuery._resolve`) reads a continent's demonym exactly ("North American" is North America, Q49).
The embedding had put it nearer the United States, and "orders from North American countries" filtered one
country. The stem is tried only for continents, where it must itself be a continent; for every type, "plan" would
read as "pl" and "can" as "ca". The meaning walk (`meaning_filter`) then took the nearest hop first, and there the
fuzzy "American" → United States (0.85, via `city.country`) beat the exact continent one hop further. It now walks
the graph for exact names first and falls back to fuzzy ones, the rule `_resolve` already applied inside one lookup.

## A counted noun and a two-word measure name what the aggregate uses (2026-09-30)

The sweep of the shipped sheets found two more readings that dropped part of a question. "how many leads from
Europe" was declined: the sheet calls its rows "responses", so "leads" was no schema word, and the coverage gate's
fuzzy fallback put it 0.85 from the city of Leeds. The noun a count cue governs is what COUNT counts: the head of
the words after "how many" or "number of", up to the first grammar word. Only the head is covered, so "German" in
"how many German leads" is still a place the query must filter on.

"total weight kg for deliveries in Germany" answered COUNT(*) = 1 instead of 2. The measure matcher in
`EncoderQuery.read_op_all` knew one-word column names only. So `weight kg` went unnamed, and the table noun
"deliveries" read as "count the deliveries", which is the rule for "total customers". A column named by all of its
words, or by a first word that starts no other numeric column ("total weight for deliveries"), is now the measure.
The one-word rule and the table-noun count are unchanged.

## Compose: the sheet's name, learned rankings, and required ops (2026-09-30)

"total amount of GBP orders in Europe" listed five items. `_mentions` matches a text column by a loose stem
('ordered'[:5] == 'order'), so "orders", the sheet's own name, grouped by the `ordered` column. The names of the
uploaded sheets under the view stack are now claimed before that match; a column the question names outright still
groups.

"total amount in North America in USD" was composed as the top 3 customers by amount, over the USD rows only. The
learned head fired TOPN with no ranking word in the question, and the default top 3 grouped by the first text column.
TOPN and SORT change which rows answer, so they now need a ranking word, a number, or an explicit sort cue, like the
other operand-gated primitives.

That plan should never have owned the question. The compose host's local-composition branch (`shared_composition`,
2026-09-10) let compose own any plan with a composition op whenever the request carried an analysis context, which
every served request does. It skipped `route()` entirely, including its required-op check, so a question that
needs a `convert` was served by a plan with none. The Spider evaluator never had that branch: it asks
`compose_owns` alone, as `docs/ARCHITECTURE.md` and `tests/test_routing.py` say it must ("own-data HAVING / share /
group-by / yoy ... the typed-AST planner owns them"). Measured on Spider DEV with serving's own gate (the head's
depth evidence, then a composition op), serving handed 180 of 1,034 questions to compose. Compose answered 3 of them
correctly, and the planner the evaluator scores answered 146. The branch is removed: serving asks `route()` alone,
and a plan it gives compose is still lowered through the shared deterministic plan. For those question shapes,
production now runs what Spider measures.

## Decomposition before the decode, and a decode past its budget abstains (2026-09-30)

`tests.test_complex_datasets` failed on the CPU 7B. A named compound request decoded the whole prompt before asking
the search whether it is compound. The decode ran past its 60 s budget, and the request failed instead of asking for
a decomposition. Compound structure is the search's reading alone (`compound_candidate`), so `_serve_ast` now reads it
from `search_pool` first, and a compound named request never decodes. `select_query` takes the search pool it
already ran (`searched=`), and the unused `PoolSelection.search_top` is gone.

A decode past its budget used to escape the selection, and `TableQuery.serve` turned it into a 200 response whose
error was "SQLProposerUnavailable: SQL decoding exceeded its CPU budget". Greedy decoding of the same prompt takes as
long again, so a retry cannot help. The proposer raises `SQLDecodeBudgetExceeded`, and `select_query` serves the
search pool without proposals and records `proposer_abstention` in the selection. One Spider DEV question in the
frozen replay hit the budget and was scored wrong, so this can only add. A busy or closed model is different, since
a retry can succeed. `serve` no longer swallows it, and it reaches the server's 503 `{retryable: true}`.
`/api/dimension` now waits on the shared engine lock for `QUEUE_TIMEOUT_SECONDS` and answers the same 503; before, it
waited without a bound.

## Place lookups are indexed and batched (2026-09-30)

`knowledgebase."city"` (200,886 rows) had no index on `lower(name)`. `db/init.sql` indexes the retired friendly
tables, and the qid projections that replaced them were never indexed. Each served ambiguity check was a parallel
sequential scan (148.6 ms for "paris"), run once per distinct uploaded place. `db/sync/build_qid_world.py`, the
owner of both projections, now creates `lower(name)` indexes on `"city"` and `"country"`, and
`PgQuery.ambiguities` makes one `= ANY(...)` lookup for all values. Value-membership routing
(`EntityQuery._value_membership_routes`) likewise made one lookup per column, seven for the customer-orders sheet,
and now makes one per table. Its tie between two types for one value (Georgia) went by hash order and now goes by
type name. A world request on that sheet issued 88 statements locally before these changes. The live database gets
the indexes on its next rebuild. Creating them sooner is a production database change for the owner to approve:
`CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_kb_city_lower_name ON knowledgebase."city" (lower(name));` and the same
for `"country"`.

## Compatibility paths: three removed, five kept for real consumers (2026-09-30)

The release audit found compatibility code that this file never recorded, as the one-implementation rule requires.
Three paths had only internal callers and are gone: `orchestrator/validation.py` re-exported
`engine.request_validation.validate_chat_request` (the orchestrator imports it directly now),
`orchestrator._matching_analysis` wrapped `_recalculation_target` for two unit tests, and `Router.route` took a
`world_only` argument it discarded (only `spider/probe/typing_probe.py` passed it). The world-path fixes of the same
night had written the demonym rule and the exact place-name lookup twice; `engine.embeddings.demonym_stems`,
`EntityQuery._names_by_type` and `knowledge_tables.COUNT_CUE` are now the one definition the resolver, the coverage
gate and the world-word role each read. The shared count cue also covers a bare "count leads from Europe", which the
coverage gate's own copy had missed.

Five paths remain, each for a consumer outside the current code:

| Path | Consumer | Removal condition | Earliest removal |
|---|---|---|---|
| The response's `currency`, a copy of the currency entry of `calculations` (`calculations/registry.py:attach_calculation_evidence`) | the workbook's `/api/converse` clarify payload (`web/public/lib/workbook.js`) and `regress/browser_gold.py` | both read `calculations[specification == "currency"]` | a public response field: the owner's decision, no date |
| Dropping pre-rename bridge tables ("… connected/unconnected to wikipedia", `engine/knowledge_bridges.py`; renamed in `00e16b4`, 2026-09-06) | conversation schemas persisted before the rename | no `c_*` schema holds a table with either suffix | 2026-12-05 (90-day retention) |
| A Sheets sidebar session with no `chat.sheet_session` row reopens the latest conversation on the same source (`engine/sheet_sessions.py`, `legacy`; `60fa976`, 2026-09-14) | Sheets conversations older than sheet sessions | every unexpired Sheets conversation has a session row | 2026-12-13 |
| A dataset op without authenticated provenance asks to be restated (`engine/dataset_semantics.py:validate_replay`; `8a84991`, 2026-09-07) | ops persisted before attestation | no unexpired conversation holds an unattested op | 2026-12-06 |
| `/api/dimension` keeps the per-cell `evolution` beside `schema_org` (`engine/dimension.py`) | the MCP `describe` coverage hints (`mcp_server/engine_client.py`) | `describe` reads `schema_org` | when that tool changes, no date |

## An output currency survives a complete question in between; a fallback names its currency (2026-09-30)

The Chrome gate on the release found one miss in 136 graded turns. In a fresh formfacade-leads conversation, after
"This is in euros. Whats in USD" and the complete question "total budget in Africa", the shorthand "How about all of
Europe?" reached the engine as "total budget in Europe" (62,000, unconverted). Prompt rule 3 correctly sent the Africa
question verbatim, and rule 4 said to carry every qualifier "from the conversation", but the model read "the same
question" as the Africa one. Rule 4 now says it directly: an output currency the user asked for stays in force for
follow-ups about the same measure until the user names another or asks for the original figures; a complete question in
between is still verbatim and does not cancel it; another measure (a count, a rating) does not take it. With the
reported history on claude-sonnet-5, the old prompt carried USD 4/5 times and the new one 5/5.

A rerun on the old prompt converted but replied "70401". The grounding check keeps the model's sentence only when it
states the engine's scalar, and it could not read an abbreviated amount; the fallback was the bare value. The check now
reads a magnitude word after a number ("$70.4k", "1.2 million", "2.5bn") as that multiple, as it already read "%", and
the fallback names the ISO currency the engine verified ("70401 USD"). That verification is the currency entry of
`calculations`, which the MCP tool output now carries for answered results (docs/MCP.md); a rows-already-in-it filter
or an unverified reading adds no unit.

## A reply says only what the result shows (2026-09-30)

The release gate grades numbers, and every graded number was right. Reading the 163 recorded replies found sentences
that said more than the result did:

- "average price for VIP customers", over a plain `price` column, was "$250.78"; a purchase-order sheet titled in
  pounds was "$128,831,117.68 ... over $5,000".
- The category-gaps demo opened with "Ava, your top spender at 200" and "Travel (your top-earning category)". Both were
  second in a top 2: Cleo spent 340 and Office earned 300.
- A discounted total was set against "the original $1,101.44" of an earlier turn, converted at that turn's rate.
- The workbook named "total amount france usd" kept that heading over the Europe-in-GBP and Belgium turns that
  modified it.

What changed, all in the chat orchestrator:

1. The model is shown a verified output currency as `currency` on the trimmed tool result, and the prompt says when a
   figure has one: that field, a currency filter, a column that names one, or the currency the question asked for.
   Otherwise the figure is a bare number and no unit is guessed.
2. `_grounded_presentation` removes a currency sign written against an amount unless the engine satisfied a currency
   calculation for the answer, or that sign is in the user's message, the engine question or the result the model was
   shown. It removes the sign and nothing else; a currency written as a word is left to the prompt. Signs are Unicode
   category Sc, so the orchestrator holds no currency table beside `engine/currency_intent.py`. Replacing the whole
   sentence with the bare scalar was rejected: it discards a correct sentence over one character.
3. The prompt says that a result listing some of a top N does not say where each one ranks, even when one row came
   back, that a rank is named only when the rows list the whole ranking, and that figures from earlier turns stay out
   of a reply. This one is a prompt rule with no deterministic check behind it, so its live test is a rate (at most 1
   promotion in 4 replies).
4. A new analysis is named for what is measured and how it is grouped, without places, dates or currencies. The engine
   cuts a name over 40 characters mid-word and adds a hash ("top customers products never bo c9272891"). The tool
   schema now states the limit, the model still exceeded it on the promotions demo's second analysis, and so the chat
   cuts a new name at a word boundary before the engine sees it. An existing analysis keeps its stored name.

On claude-sonnet-5, with the same engine results and the model's text read before the guard: a currency named for the
plain price in 6 of 8 replies before and 0 of 8 after; a name holding the place or currency in 8 of 8 and 0 of 8; the
second of a top 2 called the top in 19 of 24 replies before (and given a guessed rank in 4 more) and in 0 of 24 after.
These are wording changes. No graded answer moves.

The first gate run on these rules found their cost. Told to add no sign of its own, the model also stopped writing the
sign it was entitled to: "In US dollars, your total budget for the German entries comes to 37,471.50." and "comes to
70,401 in US dollars." were right and failed the gate, whose comparator (`regress/browser_gold.py`) binds a converted
value to the currency written against it. With the conversation that produced them replayed, 16 of 20 replies left the
amount without its currency. The prompt now says to write the currency right beside the amount, and
`_grounded_presentation` adds the verified ISO code after the amount that states the answer when neither a sign nor
the code is already against it ("comes to 70,401 USD."). Measured end to end on the same three conversations: 0 of 36
unpaired. The comparator is unchanged.

## A busy engine is reported as busy; the chat does not resend (2026-09-30)

Three conversations were asked at once during the gate. The engine serves one question at a time per instance and
admits a waiting request for `QUEUE_TIMEOUT_SECONDS` (15 s), so two of the first six turns came back as 503
`{retryable: true}`, "Engine is busy; retry shortly". The chat replied "Let me try that again." and retried nothing: a
terminal engine outcome ends the turn. The prompt now says there is one attempt per question and that the user is
asked to send it again. On claude-sonnet-5 a retry was promised in 5 of 10 replies before and 0 of 10 after.

The chat does not resend the call itself. A request rejected at admission has already minted its conversation and
appended its dataset ops, and the 503 does not return the conversation id, so a resend would mint a second
conversation and append the ops twice. Queueing belongs to the engine's admission, and that is unchanged here: a longer
window has to be measured against the 180 s engine call and the 240 s turn (a decomposition's p90 is 140 s), which is
the capacity item already open for the owner.

## An engine clarification that an earlier turn settles is answered from it (2026-09-30)

The payment-commissions follow-up "how much commission came from cards?" missed in both existing-conversation Chrome
passes. The model sent it as typed, as rule 3 says for a complete question. The engine read it literally: it proposed
"total commission_percent" and dropped "came" and "cards". The clarification became the reply, although "total
commission amount for card payments" two turns earlier had said what the user means. The dataset marks the line
`chat:` because the conversation, not the engine, resolves it, and the engine side is unchanged.

Replayed on claude-sonnet-5 with the engine's clarification, the miss depends on the transcript. It happened in 0 of 8
turns on the fresh pass's transcript and 1 of 8 once the question had been asked before (1 of 12 in the replay after
the first gate). It happened in 5 of 8 on the smoke transcript, which already held one relayed clarification. With
that exchange removed from the same transcript it happened in 0 of 8.

The engine's clarification of a follow-up sent in the user's own words now goes back to the model once, as
`status: ambiguous_wording` with the engine's `clarify`, in a round that can call the tool. When an earlier turn
settles it, the model sends one complete question in that turn's words. `_verbatim_standalone` is skipped for that
question, since the user's words alone are what the engine could not read. Otherwise the model asks the user. The
reply is then grounded as a clarification, so it may add no number. The bounds are structural:

- there is one offer per turn, and none for a first question or a question the model already rewrote;
- the result after the offer is terminal;
- the same words sent again are not sent to the engine, and the clarification stands.

After the change, the smoke transcript missed in 0 of 8 turns: 4 answered the clarification from the earlier turn and
4 rewrote the follow-up directly. The fresh transcript missed in 0 of 8. Clarifications that nothing earlier settles
were asked of the user in 8 of 8 turns each: "total amount in GBP" after a count (convert or filter) and "total amount
by region" when the data has no region. One looser reading was measured and kept. After only "how many card payments
are there?", the follow-up was resolved to the commission amount in 7 of 8 turns. In 5 of those the model rewrote it
directly, as rule 4's example already teaches, and in 2 of 3 offers it answered the clarification. The engine's own
reading there was a sum of percentages.

## Production keeps one warm engine instance and never scales out (2026-10-01)

Since revision 00123 (2026-09-29) production had run with `min_instances = 0`, against the documented default of 1.
After an idle period the first question waited for a 3-5 minute model load, and the chat gave up at its 180 s engine
call: "I couldn't reach your data just now". With min 1 restored, a question still waited 3.5 minutes on an instance
that Cloud Run started for it while three others were ready, and failed the same way. The service now runs exactly
one instance (min 1, max 1). An instance answers one question at a time (`WORLD_LOCK`), so a second simultaneous
question gets the engine's "busy" after its 15 s admission window instead of a cold start. The service bills CPU
always allocated, so the warm instance costs about $450 a month at list prices; that is the cost production carried
before the drift.

## The workbook says what each sheet is, and a name is not a filter (2026-10-01)

Driving every shipped dataset in Chrome (80 fresh turns, 80 passed) found presentation defects the numeric grading
does not see. Each one is fixed where it is produced:

- Column badges. The deterministic emitter names stage columns `<table>__<column>`, which `engine/provenance.py`
  never matched by table, so a `combined` sheet's uploaded columns were badged as calculated, payment-commissions'
  own commission rates as European Central Bank data, and an uploaded order ID as Wikidata. A qualified column now
  takes its table's record.
- Step text. Every per-row calculation is a `convert` step, and the rail described each one as an ECB conversion. It
  says so only when a column of the step is traced to the exchange-rate reference.
- The flat tab strip repeated names ("combined", "combined"); a shared name now carries its branch, else a number. A
  decomposition's question was read as twice. A computed decimal filled its cell ("19040.46312152585994148"); it
  shows three decimals with the exact value in the tooltip. A highest value was badged EXTREMES. Two "city" lookups
  matched yes/no columns and stayed beside a count that used no reference data; a lookup the finished derivation
  never joined now leaves the workbook. Uploading your own file kept the demo question in the box; it is cleared.
- Workbook names. The prompt says to leave filter values out, and the model kept them in 12 of 12 names on replay:
  "products not bought by paris customers" headed the Lyon answer and "orders count paypal" the Email answer. The
  chat now removes from a new name the words of any uploaded cell value the question names, and a name left with
  only an aggregate word takes that value's column ("document_count"). The final pass then showed the PayPal
  question created as "orders count" beside the returned-orders count, which the engine stored as "orders count
  2". A name says what is measured and how it is grouped, so a new analysis named like an existing one differs
  only in its filters; the chat sends it as that analysis's next revision, as the prompt asks for another filter.
- Replies. On claude-sonnet-5 with the same engine results: "1082.41" without a thousands separator in 5 of 6 replies
  before and 0 of 6 after; a remark that the currency is unknown in 2 of 8 and 0 of 8; a same-shape question modified
  its analysis in 5 of 6 and 6 of 6. In a conversation with earlier turns, answers began "Rechecked it —" or
  "Confirmed —" (8 of 8 in the customer-orders gate conversation); the presentation round is now told to answer on its
  own, which took a transcript that already held such replies from 4 of 8 to 1 of 8.

## A one-number answer reaches the model as the reply writes it; a currency is named once (2026-10-01)

The final gate pass answered "total amount in Belgium in US dollars" (365.631) with "$365.63 in US dollars" and a
Europe total with "£1,914.18 in GBP". Replays showed the model also misreads the raw scalar. As the first question of
a conversation it wrote the Belgium total as "$365,631.00" in 10 of 30 replays on the production prompt, reading the
dot as a thousands separator. A prompt sentence saying the dot is a decimal point (`65597c1`) cut that to 2 of 30, but
the gate then found formfacade-leads answering "70,401 USD": after an earlier "$37,471.50", the model wrote the
whole-dollar Europe total 70401 as "$70,401.50" in 16 of 20 replays (0 of 20 before). Each time the grounding check
replaced the reply with the bare number.

The chat now hands the model a one-number answer as the reply writes it. The tool result carries `value` ("365.63",
"70,401.00", "263.96"): thousands grouped, money to the cent, any other fraction above one to two decimals. The prompt
says to use it exactly, adding only its currency. On replay neither case was misread (0 of 18 each), and the fallback
writes the same string ("365.63 USD", not "365.631 USD"); the workbook keeps the exact value. The prompt also says to
write the currency once, beside the amount. A reply may still say the amounts were converted ("£1,914.18 after
converting everything to British pounds"), which tells the user how the total was reached.

## A streamed reply cannot end on a stale prefix (2026-10-01)

The final fresh pass saved the first reply of a neartail-catering conversation as "Your rest". The chat streams a reply
to RTDB through `engine.trace.StreamBuffer`, whose full-state flushes run on a background thread, and `close()` waited
2 s for a flush in flight. A slow first write landed after the authoritative final write, the node kept the prefix,
and the browser saved it when the turn settled. `close()` now waits for the flush in flight (up to 30 s, inside the RTDB
client's own timeout) before the final write. The browser also takes the `/chat` body's reply, the turn's final text,
over the streamed one, and saves a settled turn again with it.

## A join the foreign keys contradict is never served; an evidence leaf keeps the left grain (2026-10-01)

The final fresh gate pass answered complex-promotions' second question with six customer-product pairs instead of three.
The orchestrator worded the evidence leaf "For each customer, list every product name they have ever bought". In the
production upload order the 7B joined `products ON orders.order_id = products.product_id`, skipping the `order_items`
bridge the discovered foreign keys state. The query ran and matched no row. With the neutral likelihood sentinels a
runnable proposer-only member outranks every search member, so it was served, and the anti-join it fed removed nothing.
The search never builds such a join: it joins only along foreign-key trees.

Eligibility now also checks joins (`engine/sql_grounding.py`). Two equated columns of tables the foreign keys connect
are mis-joined when the foreign keys do not make them one key and either both are foreign-key columns, or one is a key
and the two share no value in the request's data. Joins between tables no foreign key connects, shortcut joins through
a shared parent key, role keys whose values overlap, and attribute joins stay eligible. Replayed on the frozen
2026-09-29 DEV run, the rule makes 1 of 1,022 importable served winners ineligible (car_1 #151, which becomes correct)
and changes no other winner. A broader variant that counted any surrogate-key name as a key, without value evidence,
also changed #546 and #548, but it rejects a legitimate role join (`orders.ship_to_id = customers.customer_id`)
whenever discovery misses that key, so it was not adopted.

With the misjoin gone, the leaf's best-ranked reading listed product names only, the anti-join could not tell which
customer bought what, and the plan failed. An anti-join's evidence leaf is now chosen when its merge is built: the
first of its contract-compatible readings, in arbiter order, that keeps every left-side dimension
(`engine/decomposition.py`). The customer-and-product reading ranked next is served. With the local 7B in the
production upload order, the prompt and the second question now return the expected three and two pairs.

Open: the 7B decode depends on what the process decoded before (llama.cpp reuses the KV cache of a shared prompt
prefix), so one leaf decoded both ways on this machine. A reset before each decode makes it deterministic but
re-evaluates every prompt, which needs its own latency and Spider measurement.

## A question mark is never a world value (2026-10-01)

Exploring neartail-orders in Chrome, "how many orders came from Lyon?" answered 0, and "What is the total amount in
Paris?" was declined as matching no rows. `engine/data/word_country.json` lists `'?'` among the continents, and the
world value matcher (`KnowledgeTableQuery._find_value`) looked for every listed value in the question as written. A
question ending in "?" therefore also filtered `continent = '?'`, which no row holds. The eval.txt questions mostly
end without a question mark, so the release gates never asked one on a sheet with a city column. A value with no
letter or digit now names nothing a question can say; the data file is unchanged.

## A verb or an adverb says what the rows did, not which rows (2026-10-01)

The same Chrome exploration asked a dozen natural questions of the demo sheets, and the coverage gate
(`KnowledgeQuery._uncovered`) declined half of them although most of their queries were right. Every content
word the query does not show is checked against the world, and a fuzzy match at 0.6 finds a town for almost any
word: "sold" 0.93, "paid" 0.90, "comes" 0.78, "leads" 0.85. So "which item sold the most units", "how many
documents are still pending", "how many deliveries weigh more than 3 kg" and "how many leads came from France"
were each declined over the verb or adverb.

A word the tagger reads as a finite or base-form verb, or as an adverb, is now covered
(`engine.closed_class.action_words`): it says what the rows did or how. Participles stay checked ("orders were
returned" names a state, and a dropped state is a dropped filter), and so does any verb the data holds as a value
("Sold"), and the payment and listing states the existing prose rule decides. The noun a count cue governs is now
the run's last noun, so "how many leads came from France" counts leads; the rows another aggregate is taken over
("the average score of the leads") are covered the same way; and comparatives join the comparators the gate never
treats as places. Ten of the twelve questions are now answered correctly. "How many orders were paid in EUR?" is
still declined, because "paid" is a constraint the data records nowhere, and "which customer placed the most
orders" serves one of a five-way tie.

A second batch of eleven found two more families: a light verb's participle ("how many payments were made by card",
covered like a finite verb, since make, do, have, take, give, get, go, come and put carry no state of their own) and
a graded adjective ("what was the most expensive event", covered when the query orders or compares). Nine of the
eleven are answered; "average amount per event" groups by event, and "transfers signed in August" is declined
because a month filter is not planned. Passive participles of other verbs ("orders were placed in Paris") are still
checked, as a lifecycle state the data lacks should be.

A third batch of seven found two more: a measure word was checked for a nearby town before the measure test, so
"which category brought in the most revenue" was declined over "revenue", although the docstring says a measure word
is covered when the query aggregates; the measure test now comes first. And a participle on the measured column
("the total quantity purchased") is covered, except the payment and listing states. Six of the seven are answered;
"which client has the biggest contract" compares values in mixed currencies as raw numbers. Across the three
batches, 25 of the 30 natural questions are now answered correctly. The serving gate does not run in the Spider
evaluation.

The release gate's exploration found one more: "which country has the most leads?" was declined over "leads",
although the query ranked the countries by `COUNT(*)`; "highest number of leads" passed because "number of" is a
count cue. The noun after "most", "fewest" or "least" is now the counted noun when the query orders by `COUNT(`, as
after "how many". The case also showed that the graded-adjective rule read the demonym in "the most German leads" as
a grade, so a query that never filtered Germany passed the gate; a named entity is no longer a graded adjective.

## A stated currency is never shown by its private name (2026-10-01)

After "This is in euros." dataset semantics adds a private column, `__currency_for_<hash>`, that holds the stated
code. The 2026-09-08 rule keeps it out of both route sources, but two presentation paths still showed it, both seen
in the Chrome gate on an existing formfacade-leads conversation. The composed host streams a "Looking up <column>"
slide for every text column it does not route, so the chat's status line read "Looking up
__currency_for_0af96a8ed622a394…". And the workbook split the derivation alias `responses____currency_for_…` at its
last `__`, so the calculated sheet showed a "currency_for_0af96a8ed622a394" column under a "responses__" chip.

`ComposedKnowledgeQuery._world_lookup` now skips the column with the same predicate,
`dataset_semantics.is_synthetic_currency_column`. The workbook names the column from its server-authored provenance
(`asserted`, operation `measure currency`, input `responses.budget`): "budget currency", under the "responses" chip
with the conversation glyph. The emitted SQL and Python keep the real name, as the executed program must.

## A converted total is computed per the column the question groups or ranks by (2026-10-01)

Exploring the customer-orders sheet in Chrome after the release gate, a currency target turned every grouped or
ranked total into the wrong shape. "which city has the highest total amount in US dollars?" was answered with the
converted total of every city, 9,540.93, as if it were the answer; "total amount by country in US dollars" counted
the orders per country and was declined ("not every set-operation branch produces a scalable numeric aggregate"),
and the chat explained the decline as missing exchange-rate data; "total amount by tier in US dollars" was declined
for its grain. The world owner (`KnowledgeTableQuery.serve`) had one converted shape, the registered scalar
calculation, and a world attribute named with a total fell to the projection that counts rows per value.

The world owner now reads the uploaded column a SUM is grouped by ("by tier", "per city", "for each customer") or
ranked by ("which city has the highest …") and lowers the converted SUM per that column, ranked or not, through
`lower_world_query`'s existing grouped and ranked stages; a world attribute named with a total ("by continent")
gets the total per value, converted when a currency is named. The calculation evidence records the grain, so the
grain check accepts the grouped answer and still declines one figure over every row. An AVG with a currency target
stays declined: the conversion stage computes a SUM. The Python materializer flattened every table whose attribute
a row carried, and the total per uploaded "city" shared its name with the knowledgebase "city" table; it now
flattens only the tables the view's shape names, so the group value survives in both programs.

In production the owner's saved "tier" reference sheet sits beside the orders' own tier column, and "total amount by
tier in US dollars" was declined as ambiguous (2026-10-02). When more than one sheet has the column, the measure's
own sheet now answers; the reference column is the same grain through its key.

## A world type or a place the query never realized is never answered past (2026-10-01)

The same exploration on the bank, restaurant and hospital sheets found answers to a different question served as
answers. "which country has the most deposits?" was answered "JPMorgan Chase", the top bank; "total amount by
country" on the catering sheet was one total over every restaurant; "which bank has the most deposits in Europe?"
ranked every bank in the world. The coverage gate exempts world type nouns ("country", "cities") because they
usually name the rows ("total amount for cities in France"), its fuzzy place check knows only countries and cities,
and it served the answer whenever no rephrasing was found.

A world type the question asks for ("which country", "by country", "per city") is now a dropped constraint unless a
column of that name or the query itself realizes it, and a word that names a country, continent, city or state
exactly is dropped when the query never filters on it. Either one clarifies even without a rephrasing. These
sheets answer a country filter on the entity ("total deposits for banks in Switzerland") but not its country as a
dimension or a continent; those questions are now declined rather than answered wrongly.

"what percentage of orders are from Paris?" listed the Paris customers (2026-10-02). A share is realized only by a
division, and shares are not planned on own data yet, so "percentage", "percent", "share", "proportion" and
"fraction" are dropped unless the query divides, and the clarification offers no rephrasing: "what share of the
total amount comes from Paris?" used to be offered "total unit price".

## A count per world value counts the rows of each value (2026-10-02)

"how many attendees per country" on the workshops sheet answered 6, the number of countries: the world owner read
every COUNT with a world column as "how many countries". A count whose world word follows "by", "per" or "each" now
takes the projection that counts the rows holding each value (Germany 3, Portugal 2, …); "how many countries are
the customers in" still counts the distinct countries. `tests.test_world` checks both on the same sheet.

## A response lost between the chat and the engine is asked for again; the question runs once (2026-10-02)

In the Chrome gate on revision 00134 the category-gaps follow-up was answered by the engine in 65 s (HTTP 200,
64 KB, revision 2 committed, the rows streamed to the workbook), yet the reply read "Sorry, I ran into a hiccup
pulling that up just now. Could you send the question again in a moment?". Replayed 46 times on claude-sonnet-5 with
the engine's answer, the presentation never said that; replayed with the connection dropped before the response, it
said "something went wrong on my end trying to pull that up. Could you send the question again in a moment?" in 5
of 5. The model was shown a failure, and the engine's request log holds a 200 with the whole 64 KB body, so the
response was lost between the services. The chat logged nothing about it.

The engine client now repeats a request once when the transport fails, with the same jobId, after one second. It
does not repeat a read timeout (the engine is slow, and asking again waits again) or any HTTP answer, so a busy
engine is still reported as busy (2026-09-30). The engine answers a repeated jobId from the response the first
request produced (`engine.request_limits.ResponseReplay`): it claims the id for the verified principal right after
authentication, before a conversation is minted or dataset ops are appended, records the response before writing
it, waits while the first request still runs, and keeps finished responses for five minutes, at most 64 of them
and 64 MB. The question runs once, and the 2026-09-30 reason not to resend (a second conversation and ops appended
twice) does not arise. A repeat whose first request ended without a response runs as a new request. The browser's
own re-drive of a direct question already reused its jobId, and it now waits for the first answer instead of
running the question again. The timing lines now name a lost response: `engine_transport_<error>_ms` on the chat
side and `replayed_ms` on the engine side.

## A decomposition node id is a readable name (2026-10-02)

The node ids a decomposition proposes name the sheets, sections and merged columns the user reads. For "only use
the top 2 customers" the model named its leaves "c", "p" and "ev", and the workbook showed "c combined", "c top
results" and "ev result" and a result column "p_sum". The tool schema now says each id is a readable snake_case
name of what the node holds, and validation rejects an id shorter than three characters with that reason, which the
model corrects within its proposal budget like any other rejection.

## A converted total is named for its currency (2026-10-02)

The registered conversion names a converted total `total_usd`, and the totals the world owner groups or ranks
(2026-10-01) were headed `sum`: "which city has the highest total amount in US dollars?" showed `city | sum`. The
world lowering now names a total converted at the knowledgebase rate `total_<currency>`.

## A number the question states is not part of an analysis name (2026-10-02)

The name a new analysis takes already drops the cell values the question names (2026-10-01). The gate's shipping
question "How many deliveries weigh more than 3 kg?" created "deliveries over 3kg", which would head a later "more
than 5 kg" answer. A word of the proposed name that holds a number the question states is a threshold, a cutoff or a
date, and is dropped with the comparison before it: "deliveries", "top customers by spend", "revenue".

## A repeated question answered with the user's data from memory is recalculated (2026-10-02)

A message that repeats an earlier question word for word is a recalculation when the model answers it with a number
from the transcript (2026-09-24). In the existing-conversation gate, "how about customers from Lyon?" was answered
"the products that haven't sold are Alpha, Beta, Delta, and Omega" in two seconds, with no engine call and no rows:
a list has no number. The reply to a repeated question now counts as recalled when it states a number or a value of
the uploaded data, and the correction round forces the query call as before; "You're welcome!" to a repeated "thanks"
still stands.

## A non-geo entity's country is a dimension and its continent a filter (2026-10-02)

The bank, restaurant and hospital demo sheets answered a country filter on their entity ("total deposits for banks
in Switzerland") and nothing else: "which country has the most deposits", "total deposits by country", "which bank
has the most deposits in Europe" and "how many transfers in Canada" were declined since the coverage gate stopped
answering past a world word (2026-10-01). The one non-geo owner (`KnowledgeQuery._nongeo_plan`/`_serve_world_type`)
now binds the entity's country as a filter, as the dimension of a grouped or ranked total, and through
`knowledgebase.country.continent` as a continent filter, and ranks an uploaded column inside such a filter; every
shape lowers through `lower_world_query`'s existing stages, and the grain is recorded in the calculation evidence
through the one record the place joins use (`KnowledgeTableQuery._record_computation`). A sheet with its own place
column still answers places through it, so there the question must name the type. The world operand reader
(`EncoderQuery.read_op_all`) reads a superlative of quantity as the top total of the column it names ("the most
deposits") or the row count of the sheet it names ("the most banks", "the most orders"), and a count of what a
numeric column already counts as that column's total ("how many transfers"); the place path ranks a row count per
world value the same way ("which country has the most orders" listed every country's count).

## An average converts every row before averaging (2026-10-02)

"average amount in US dollars" was declined: the world lowering forced SUM whenever a rate applied, the rate
bindings accepted only a SUM, and the currency check recognized only a converted SUM. A rate is a row factor, so an
AVG of the converted rows realizes it as a SUM does: the world owner converts and averages (`average_<currency>`),
`_output_realizes_plan` and the currency check accept SUM or AVG of the converted rows, and the own-data calculation
expander keeps the base candidate's AVG around a registered row factor; it had served SUM(amount * rate_to_usd) AS
total_usd for "average order amount in US dollars". A converted COUNT or MAX is still not certified.

## One label per entity (2026-10-02)

Q213 carries a later "Czech Republic" row in `knowledgebase.words` beside the source's "Czechia", and Q148 "People's
Republic of China" beside "China". `_qid_labels`, the one qid label source of every trail and answer, built its map
from every row, so the row order chose the label: the world owner said "Czech Republic" where compose said
"Czechia". It now takes the primary row's label, then the one the source record names itself, then the earliest.

## A top-1 ranking returns every tied row (2026-10-02)

"which customer placed the most orders?" ranked five customers tied at three orders and served one; "which country
has the highest average rating?" served Ireland or Czechia, both at 5. The owner chose every tied row over a
clarification. The served member of the one selection (`PoolSelection.candidate`) keeps every row tied with the
first on a top-level ORDER BY ... LIMIT 1 (`sql_ast.keep_ties`, FETCH FIRST 1 ROW WITH TIES, rendered with RANK()
because SQLite lacks it); the shared plan's sort stage carries the ranking terms (`SortedView.ties_on`) to both
emitters and the runtime sort; the world ranking and compose's top 1 keep ties the same way. Ranking, arbiter
features and the pool are unchanged: only the served answer keeps the ties. A larger LIMIT is the number of rows
asked for, and a subquery's LIMIT 1 stays one row. Spider's gold uses LIMIT 1, so a tie its database holds now
grades strict-wrong where the arbitrary row used to match; the fresh whole_db run reports the transition.

## A follow-up renames an analysis without its dropped filter (2026-10-02)

Analyses created before names lost their filters kept them: in the 2026-10-02 Chrome gate's existing conversations,
25 of 60 follow-ups showed "Reasoning steps for orders in paris" over a Lyon answer. The owner chose renaming on the
next follow-up over a one-time migration of stored names. A `modify` whose stored name holds a word the question no
longer asks for (a word of a value of the uploaded data, a currency, or a name the last question capitalizes) is
sent with that word removed, and the engine renames the analysis in place: same id, links and revision history,
the next revision's sheets under the new name. The engine accepts only the stored name's own words, in order and
fewer of them, and leaves a name another analysis of the conversation holds to that one.

## Dates and months filter a date column (2026-10-02)

"How many transfers were signed in August?", "How many leads were submitted after August 10, 2026?" and "How many
contracts were signed before July 10, 2026?" were planned without their date and declined over the month name.
`engine/sql_dates.py` reads a month with an optional day and year after an optional cue (before, after, since, from,
until, till, through, on, in, during) and turns it into typed comparisons on a date column: a dated phrase compares
the date with its first day or the day after it, and a month without a year compares the month of each date
(`DatePart`, the shared plan's MONTH, read from the date's ISO text by both programs). A phrase claims its tokens, so
"August 10, 2026" is not the number 10 or the year 2026; a day without a year ("after August 10") filters nothing and
the coverage gate asks about it. The coverage gate reads the months a query realized from the same comparisons.

## An own-data share is a typed-AST ratio (2026-10-02)

"what share of the total amount comes from Paris?" served the Paris total and "share of total amount by city" each
city's total; the gate declined both over the dropped word. Routing keeps every own-data question with the typed-AST
planner, so a share is a typed ratio there, not a compose plan: the kept rows' SUM or COUNT over the same aggregate
of every row the query reads (`sql_ast.share_of`; "what percentage of orders are from Lyon" counts the rows it would
list), ranked above the total it divides. It lowers into the shared plan as the kept rows' reduction crossed with the
whole's reduction and divided, so both programs run it in linear time.

## A view's label keeps its logical name past a 63-byte cut (2026-10-02)

A decomposition names each leaf's views under the root analysis slug, and a name past PostgreSQL's 63 bytes is cut
and given a hash (`engine.analysis.analysis_view_name`). The execution record read a view's logical name back from
its physical name, so under "top_customers_never_bought_top_products" a step was labelled "top products b 2f5cf8f2".
A plan records each renamed view's logical name (`AnalysisPlan.logical_names`); decomposition records the leaf and
merge names, the stage manifest carries them to both emitters' record, and physical names, SQL and Python are
unchanged.

## A stated date is served and compares on PostgreSQL (2026-10-02)

The engine build's live suite (the 7B on a disposable seed) showed what the hermetic tests could not: the arbiter
ranked the 7B's undated reading of "total transfers signed in August" first, and the coverage gate declined it. The
one served selection keeps to the readings that realize the dates the question names when one does
(`select_ranked_candidate`'s `date_satisfied`), a serving fact the evaluator records and arbiter training replays; old
label pools lack it and must be regenerated before a replay. The SQL program wrote a date as `DATE '2026-07-10'`,
and PostgreSQL has no `text < date` for the TEXT column an upload stores dates in; the literal is now an untyped ISO
string, which compares with a DATE column too. The parser reads ordinal days and "between <date> and <date>", and a
lone month that is a value of the data ("the first name April") stays that value.

## A non-geo entity resolves to the exact nearest entity of its type (2026-10-02)

A cell with no exact name in `knowledgebase.words` resolves to the nearest entity of its type by embedding. The lookup
went through the HNSW index, which is approximate and applies the type filter after its scan, so whether it returned
a row depended on the graph a restore builds and on the planner: on the build's fresh seed "Mayo Clinic" stayed
unresolved and the US hospitals totalled 32 instead of 46. It now sorts the exact distance over the rows of its type,
bounded by the `(type, qid)` B-tree; on production it returns the same match at similar latency. `knowledgebase.words`
holds no embedding for any of its 3,114 banks, so a bank name resolves only by its exact name.

## A date range, a list of months and a quarter are one span (2026-10-02)

A probe of the date reader on common phrasings found wrong answers, not declines: "from March to May 2026"
and "between March and May 2026" filtered May alone, "between July 1 and July 10, 2026" July 10 alone, "in
August and September 2026" both months at once (no row), and "after the 10th of August 2026" all of August.
`engine/sql_dates.py` now reads a range's two ends as one span ("between A and B", "from A to B", "A through
B", "A-B", "from 5th to 10th August"), lends a year one end names to the other (across a year's end when the
months run backwards), reads consecutive listed months and quarters as the span they cover, and reads "the
10th of August". A lone "from" is the period it names ("orders from August 2026"); "from A onwards" reads as
"since". A list with a gap ("January and March") and an impossible day compare nothing, so the coverage gate
asks rather than answering for no rows. Relative dates ("last month") are still unread.

## A column equal to two values is no query; share words are aggregates (2026-10-02)

A probe of own-data phrasings found answers that were confidently wrong. "The total amount from Paris and
Lyon" filtered `city = 'Paris' AND city = 'Lyon'` and answered nothing. No row holds both, and none of
Spider's 8,034 gold queries conjoins two such values (Spider writes "the continents Asia and Europe" as OR).
The base search still emits the conjunction, because the set and disjunction expansions build OR, UNION and
INTERSECT readings from it. A union candidate now reads it as either value, the search drops the conjunction
before ranking, and `engine/sql_grounding.py` grounds no such query, so a proposer's conjunction is never
served either (`sql_ast.contradictory`). The disjunction rewrite also gained the SUM cue it lacked ("the total
amount from Paris or Lyon" listed the amounts).

A share word with no other aggregate cue is now one (`sql_expansion.share_cue`). It sums the measure it names
within three words ("percentage of amount by city") and otherwise counts the rows ("share of orders by city"),
and the search and the ranker read the same cue. The search, its expansions and the ranker had three copies
of one word splitter, none of which kept "%". They now share `sql_expansion.words`, where a "%" that follows
no number is the word "percent". A plural noun right before "over <n>" compares each row ("orders over 50"),
as every Spider question of that shape does, instead of reading as "more than 50 orders". None of Spider DEV's
8 share-word questions is affected (each word names a column there).

## No local model writes SQL; Gemini is a labelled fallback and the only LLM (2026-10-02)

**Decision (owner).** The project's objective is interpretable, deterministic answers built from named
dimensions. A model that writes SQL token by token contradicts it, however its text is gated. So:

- Removed: the XiYanSQL-QwenCoder-7B Q4_K_M proposer (`engine/xiyan_sql_proposer.py`, its fetcher,
  contract, GGUF and `llama-cpp-python`), the fitted linear arbiter (`SQLArbiter`,
  `engine/data/sql_arbiter.json`, proposal merge, endorsement and likelihood features), and their
  pipelines (`training/proposer/`, `training/rank/`, the SQL selection bundle). The earlier 0.5B LoRA
  SQL proposer was already retired; its adapter remains only in the published weight revision.
- Selection (`engine/tables.py:TableQuery.select_query`) serves the deterministic search's best-ranked
  candidate that executes and is grounded; a date the question names, a registered calculation or a
  named money total can prefer a later eligible one. `PoolSelection.record()` reports why.
- Only when no candidate is eligible and the operator enabled Gemini (`EXTERNAL_LLM_ENABLED`),
  `engine/sql_fallback.py` may ask Gemini once to reword the current question. The search alone builds
  and validates SQL; Gemini cannot propose it. The answer carries `fallback`, `served_by` and a `model`
  string that say so, and the workbook status line repeats it. The request is stateless and uncached,
  and coverage checks both the original question and the rewrite. Decomposition leaves never take the fallback.
- Gemini on Vertex AI (`engine/llm.py`) is the only LLM provider: chat, `/api/converse`, reference
  generation and the fallback. Anthropic is removed.

**Why not keep the 7B and label it.** It served about 60% of Spider answers (626 of 1,034 on
2026-10-01), so the essay's claim that Prereasoner never generates SQL by next-token prediction was false
for most own-data answers, and the arbiter that chose between it and the search had been fit on a
different, retired model. It also set the engine's cost: 8 vCPU / 16 GiB, a ~20 s median decode, a
5–7 minute cold load, and long waits behind the engine's one-request-at-a-time lock.

**Cost, measured** (`spider/results/RESULTS.md`, 2026-10-02). Spider DEV `whole_db`, engine alone,
fallback off: 354/1,034 strict for the removal alone, 380 with the same day's search fixes, against 866
with the 7B. The search pools a correct query for 544 questions, so ranking, not the removed model, is
the next deterministic lever. Median prediction time fell from about 20 s to under 2 s per question.

**How to apply.** Raise accuracy by extending the typed search (`engine/sql_search.py`); CLAUDE.md now
forbids a SQL-generating model or a learned ranker over model output. The fallback fires only when
nothing runs; most wrong readings still run (a `SELECT *` almost always does), so it is not an accuracy
lever and must not be widened without the owner.

## A contained text compares the lowered values in both programs (2026-10-02)

The search had no substring reading: "the contestants whose names contain the substring 'Al'" and "a song
having 'Hey' in its name" dropped their filter (Spider DEV), and 'Al' became the state code 'AL'. The typed
AST gains `Lower(column)` (`engine/sql_ast.py`) and the shared plan gains `LIKE` in both emitters, with a parity
test. A contained text compares `LOWER(column) LIKE '%text%'`: lower-casing both sides makes SQLite (whose LIKE
ignores ASCII case), PostgreSQL (whose LIKE does not) and the Python program agree. "substring", "letter" and
"the word X" ask for it even where the text is a whole value of the data; after "contain", "include" or "in its
<column>" a whole value stays an equality ("the documents that contain the paragraph text 'Brazil'"). It
compares the text column that holds the text, the nearest named first (commits f572eb6 and ab5093a).

## Larger sheets: 50,000 rows a tab, and the add-on reads the active tab first (2026-10-02)

A customer's 30,000-row Subscriptions sheet was refused with "too large to analyze in one request", and
Gemini in Sheets answered it. The limits bound the per-question upload, not the database or SQL. Each
question sends the whole workbook, and the engine loads it again. So the limits are raised to measured
headroom:

- 50,000 data rows a tab, up from 10,000.
- 8 million characters a table and 20 million in all, up from 2 and 6 million.
- 30 MiB request bodies for the engine and the chat, under Cloud Run's 32 MiB.
- 8 MiB CSV files and 16 MiB workbooks in the browser.
- 500,000 cells in one add-on read, up from 250,000.
- A 45 s worker parse timeout.

`engine/request_validation.py` and `web/public/lib/upload-limits.js` state them once each, and the add-ins
take theirs from the latter.

Measured on a 30,000-row, 11-column sheet of the customer's shape:
- The planner loads it in 1.0 s and selects in 3.9-4.7 s per question on a workstation CPU.
- The full serving path through the local Cloud SQL proxy answered in about 30 s per question in the
  default mode, mostly in round trips through the proxy.
- The Python program keeps its own 10,000-row limit, and `auto` runs SQL above it. Verify mode, which needs
  both programs, still refuses such a sheet.

The Sheets add-on reads the active tab first, and that tab must fit. Another tab that does not fit is left
out, and the sidebar names it, instead of the whole spreadsheet being refused. A tab does not fit when it
breaks the row or column limit, or would push the cell total past the cap beside the tabs already read. The
customer's sheet had six tabs.

One engine instance still serves one question at a time, and a large sheet holds it longer. Rows stop
costing per question only when a sheet is uploaded once and every question runs on the stored tables. The
add-on's own limit lives in Apps Script (`sheets-addon/Code.js`). It reaches users only after a `clasp
push` and a new version set in the Marketplace App Configuration.

## A non-geo answer says which names it could not match (2026-10-02)

A non-geo world answer ("total transfers for hospitals in the United States") resolves each uploaded entity name
to a knowledgebase entity of its type, and skipped every row whose name resolved to nothing. An unmatched US
hospital lowered the total without a word. `KnowledgeQuery._serve_world_type` now reports those rows
(`engine/knowledge_query.py:unmatched_rows`). It counts them only among the rows the uploaded-value filters keep,
compared as the shared plan compares them, by its own operators. It reports the first five distinct names, and the
rest as a count.

- When the unmatched rows are at most half of the rows the answer could count, the engine answers. The response
  carries `unmatched` (count, out of, names) and a `warnings` sentence, which the rail shows. The MCP tool output
  and the chat pass `unmatched` to the reply, which states the count in one clause
  (`orchestrator/system_prompt.py`).
- When they are more than half, it declines with a clarification naming them, before any bridge is written. A
  total over a minority of the user's rows is no answer to their question, even with a caveat.
- The shared plan and its trail are unchanged. The lookup sheet shows the matched rows, and the warning says which
  rows it could not show. An outer lookup would have kept them on the sheet, but a ranking by the entity's country
  would then need a step dropping the rows with no country, a no-op sheet whenever every row matched.

Both entity lookups now break ties deterministically. 87 hospital names name two QIDs in production, and the exact
lookup took whichever row came first. They order by the name's primary entity, then the lowest numeric QID
(`length(qid), qid`). The exact-nearest lookup breaks a distance tie the same way. The geo city lookup
(`engine/entities.py:_city_bridge_sql`) ranks a shared name by the row's country, `is_primary` and population. It
has no final tie-break after those, and is left for its own change.

## A total by month groups by the year-month (2026-10-02)

"Total amount by month" and "monthly total amount" were planned as one ungrouped total, "how many orders per
month" grouped by customer, and "which month had the highest total amount" returned the overall total. The coverage
gate declined them over "month", so they were honest declines, but monthly totals are among the most common
business questions.

A month now groups by the year-month of a date column, the text '2026-08' (`DatePart('year_month')`). It is not the
month of the year, so August 2025 and August 2026 stay two rows. Business data spans years, and merging every
August would add up periods the user never named as one. A question that wants the calendar month across years can
name it ("in August"), which filters as before.

- One cue, `sql_dates.period_grouping`, serves the search and the ranker: "monthly", or "month" after by, per,
  each, every, which or what. "The month of August" names a month.
- A column's own name is that column. A text `month` column groups as itself, and "the avg. monthly searches"
  asks for no month.
- The date column is the one the question names, else a date of a table the query reads, else the schema's one
  date.
- The months read in calendar order unless the question ranks them.
- Both emitters compute the key once in the plan (`FunctionValue` `YEAR_MONTH`), from the date's ISO text, as
  SQLite, PostgreSQL and the Python program all can. The coverage gate reads "month", "months" and "monthly" as
  realized only where the SQL groups by it.

## 30,000-row Sheets workbooks fit the read cap (2026-10-02)

The add-on already skipped oversized neighboring tabs, but a valid active worksheet could still be rejected
at 500,000 cells. A 30,000-row customer sheet with 18 columns contains 540,018 cells including its header, so
it exceeded that limit even though it stayed within the row limit and the parser's text bounds.

Raise the shared live-grid ceiling to 1,000,000 cells in the Sheets add-on, Excel reader, and upload importer.
Keep the per-sheet row/column limits and 8 MB/20 MB converted-text limits as separate guardrails. Regression
coverage reads a 30,000 × 18 customer-shaped grid and still refuses an active grid above one million cells.
