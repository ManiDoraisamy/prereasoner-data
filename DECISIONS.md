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
