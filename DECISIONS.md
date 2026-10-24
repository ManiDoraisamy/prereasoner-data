# Decision record — the open-source consolidation

This repo was consolidated from an iterative research codebase (twenty backend generations, a
separate frontend repo, three Cloud Run services). Every structural choice below was made
deliberately during that consolidation; this file records what changed and why, so reviewers
don't have to reverse-engineer the reasoning.

## Count outputs, ranking occurrences and table-owned full names (2026-10-13)

The independent review of the committed October 12 build and the completed October 13 package preserves the
existing typed-AST owners and the unchanged encoder bundle. Questions opening with "how many" or "count" must
show a count, not merely contain an aggregate in a WHERE, HAVING, ordering or source subquery. `query_contract._shows_count`
checks output expressions in their scope, including projected scalar queries. A SUM of the explicitly requested
stored-count field remains valid through the shared `sql_expansion.counted_measure_columns` reader; numeric
column aliases resolve to their base tables. A SUM of another field or an unrelated AVG/MAX supplies no count.

Adjacent numeric-field expansion reserves the occurrence recognized by `sql_extrema.ranking_cutoff`, the same
reader construction and checking use. Reusing a cutoff as an equality silently filtered away the intended top
rows. Independent occurrences of the same numeric value retain their own predicate roles. Full-name construction
is suppressed only by a FullName field in that same table, preserving first/last-name answers when unrelated
tables expose FullName. Registered serving and checker contrasts cover these behaviors.

These are bounded correctness repairs, not complete population/scope/output proofs. They introduce no new AST
operation, planner, ranker, evaluator, model bundle or SQL generator. The fresh full DEV result and matched saved
hosted references are recorded only in `spider/results/RESULTS.md`; the detailed review and remaining binding plan
retain their existing `release-review/spider-review-20261011.md` and `release-review/sql-accuracy.md` owners.

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
- The data-use notice the OAuth verification describes stayed word for word before the first question,
  until the owner removed it on 2026-10-04 ("A minimal sidebar" below).
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

## A minimal sidebar: starters, the thread and the composer (2026-10-04)

The owner found the Sheets sidebar cluttered:
- an intro line;
- a data-use paragraph;
- a "Questions for this sheet" heading over misaligned cards;
- a sync line ("Synced just now", "Updated 4 minutes ago");
- an import-warning box ("Unnamed columns A were kept…");
- an error notice claiming the answer was returned when the question had failed.

The cards were misaligned because Google's add-on stylesheet sets `button + button { margin-left: 12px }`.

The sidebar now shows:
- Empty: three starter questions and the composer, nothing else.
- After a question: the thread, with "+ New chat" above it.
- Failed: the question stays in the thread with one error sentence, and goes back into the composer.

Supporting rules:
- The hosted page no longer loads Google's stylesheet and styles itself.
- Import warnings still travel with the tables to the engine, but are not shown.
- An unreadable or expired earlier chat is not announced. The next question starts a new conversation,
  keeping the turns on screen and the history the assistant reads.
- A turn is saved for the sheet only once it belongs to a conversation, and a failed save shows nothing.

The shared starter component (`web/public/lib/sidebar-suggestions.js`) has no heading, loading text or
failure text, and each host hides it once a question is asked (`setActive`). The web conversation page
always holds a question, so it shows no starters. The Excel task pane follows the same rules.

Privacy stays on the durable `/privacy` page (Limited Use and AI/ML), which Google also links from the
add-on's consent screen and Marketplace listing. Any OAuth re-verification must describe the sidebar
without the in-product notice.

## Failures users can act on, and a database the code can serve (2026-10-04)

A review of the 2026-10-03 changes found:

- **A ten-hour chat outage.** The engine image needed chat migration 11 (`chat.request_job`), which
  production never received. Every question with a `jobId` returned 500, so every chat question failed.
  The migration and grants were applied by hand. Now:
  - The engine refuses to start while the database lacks a migration its code needs
    (`engine/server.py:require_current_schema`, `db.sync.app_migrations.pending_migrations`). Cloud Run
    then keeps traffic on the previous revision.
  - The deployment opts in (`require_current_schema`). The grants give the serving role read access to
    the migration ledgers.
  - A Community upgrade runs migrations and grants even when its seed is already in place.
- **Engine failures shown as the answer.**
  - Users saw "internal server error", the engine's internal address, a 502 page or an exception name.
    `engine/answer_presentation.py:error_reply` now writes one sentence per kind of failure, and passes
    on only text the engine wrote for the user.
  - A clarification quotes the question it proposes.
- **A deleted conversation.** It answered 500 to every later question, and the Sheets sidebar kept
  sending the dead id. `/chat` now answers 404 `conversation_not_found`, and the sidebar and the Excel
  task pane ask once more as a new chat, keeping the thread and history.
- **Retry records.** These hold an answer's rows. They were kept for a day and survived conversation
  deletion. They are now kept for ten minutes, never replayed after that, and deleted with the user's
  conversations. The in-memory `ResponseReplay` the durable record replaced (2026-10-02) is deleted. Its
  tests now run the serving class over an in-memory stand-in for `chat.request_job`.
- **Statement timeouts.** A timeout statement sent inside an aborted transaction stopped SQLAlchemy's
  `ROLLBACK TO SAVEPOINT`, so the Python program's SQL retry failed. A lock now waits the request's
  budget rather than five seconds.
- **The suggestion service.** It received hidden tabs' names and headers, and allowed one call per
  process across all users. Now it describes visible, populated tabs only and allows four calls at once.
- **Total rows.** The importer kept a total row as data unless every other cell was a number, so
  "Total, FR, 3" (a regional subtotal) or "Total, 150, USD" was counted twice without a word. A row
  led by a total label now goes to the summaries table whatever else it carries
  (`web/public/lib/workbook-import.js`, shared by uploads, Google import, Sheets and Excel).
  - Summaries tables give way before a workbook is refused. Five tabs with a total row each made ten
    tables, over the eight a request may carry, and the Sheets sidebar has no picker. Every data region
    is kept, and a region whose total rows were left out says so in its import warnings.

## The completeness check reads names, compared values and the search's grain (2026-10-04)

The same review found the completeness check (`engine/query_contract.py`) refusing answers the previous
release served, and in one case serving a wrong one. Each fix reads the question's structure rather
than adding words to a list:

- **A field name holding an aggregate word.** The check deleted "avg" from the question before reading
  it. On the owner's keyword sheet, "the total Avg. monthly searches for the Keyword X" then made the
  filter column Keyword the requested total, and every candidate was refused. The check now uses the
  search's own reading (`sql_rank.analyze_question`, `sql_expansion.spelled_names`). An aggregate word
  that only spells a field's name asks for nothing. Another word that asks ("total") keeps its target.
- **A value the query compares.** "How many responses have Newsletter No?" and "How many tasks are Not
  Started?" were refused as unmet exclusions. A value the query filters on is data, so a cue inside it
  is ignored. "not Done" still needs its `!=`.
- **The requested grain.** Every column whose name appeared anywhere after "by" had to be a group key.
  "The average Price by Category, sorted by average Price" refused `GROUP BY Category` and served
  `GROUP BY Category, Price`. Now:
  - The grain is a field named in full, up to the clause's end, so "sorted by" orders and asks for no
    grain.
  - A name two tables share (a join key) counts once.
  - The search's own group window was tried first: it drops "id" from names, so "each stadium" asked
    for both tables' `Stadium_ID`, and four Spider DEV answers were lost.
- **Violation order.** It no longer depends on set iteration, so the same question gets the same
  message.

**Answers built from cells.**
- A blank cell was answered with an empty reply. It now says no value was recorded.
- A cell holding `[text](https://…)` showed as a live link. It now shows as text.
- `/api/converse` answers a body of the wrong shape with 400 instead of 500.
- Its unused Gemini rate gates are gone: the route has called no model since 2026-10-03.

**A "yes" to an offered question.**
- A clarification ends with the question the engine proposes ("Try asking: “total Amount in
  Germany”"), and users answer "yes". The chat model sees no earlier replies, so "yes" had nothing to
  accept.
- The offered question is now read back from the engine's own clarification text
  (`answer_presentation.offered_question`, beside `clarify_reply`) and given to the model as
  `offered_question`. It is engine text from the question and the schema names; no other reply text
  reaches the model.
- The live orchestrator suite sends it as written.

**An acknowledgment.** A first round that is not small talk is made to call the engine, so that an
earlier answer is never repeated from memory. Small talk was "hi", "hello", "thanks" and "thank you".
"thank you so much" and "ok, great" therefore became engine questions and came back as clarifications.
A message made only of acknowledgment and greeting words (`orchestrator._acknowledgment`, a closed
set) is now answered by the model. "yes", "sure" and "go ahead" stay out, because they accept an
offer. The live suite answers "thank you so much!" in words, with no engine call.

**One schema graph per question.**
- Problem: on the 4,521-row bank-marketing sheet, production spent 4–10 s of each question outside
  every timed phase. Locally a question took 5.9 s; 4.5 s of it was repeated work. The schema graph,
  which indexes every cell value, was built four times: by the search, selection, the coverage
  record and the calculation evidence. The completeness check rebuilt the words of every cell for
  each candidate.
- Fix: `TableQuery.serve` now builds the graph once and passes it down. The cell words are cached
  per graph (`query_contract.cell_words`). The same question takes 2.6 s, and the answers are
  unchanged.

**A replay record that cannot be kept** no longer turns a computed answer into a 500. The answer goes
out, and a repeat of that request runs once its lease lapses.

**A re-sent direct question.** The web page's direct path rebuilt its 90-second re-send after the stream
had named the conversation. The engine answers a repeated jobId only for the same input, so every
re-send was refused 409. It now repeats the body it first sent.

**Replies read as answers.** Since the presentation model was removed (2026-10-03), the renderer's
text is the whole reply.
- An answer of several columns read "- country: Switzerland; sum: 1550", one line under the same
  total written "1,550", and the top city in US dollars read "total_usd: 3495". A row is now one line
  with readable names ("customer name", "total USD"). A quantity the engine computed is written as a
  one-number answer is: thousands grouped, cents in the verified currency. Provenance marks those
  columns (`measure: true` on aggregate and arithmetic outputs). A value taken from the data, such as
  a year or an ID, is written as it is.
- A clarification's `reason` is reply text. "no rows matched, so there is nothing to SUM" and "the
  selected planner supplied no typed calculation evidence" were whole replies. Each is now a sentence;
  the check's own reason stays in `unmet`, for traces and the chat model.
- **The calculation gate replaces only a dropped-words clarification.** "total budget in Africa",
  asked in US dollars, matched no rows, and the gate replaced that clarification with its own: a
  clarification carries no typed calculation evidence. A clarification now stands unless it is about
  words the query dropped, usually the calculation's own phrase ("in euros"). The same rule keeps a
  decomposition request intact under a currency question.

**The Sheets sidebar says what it is doing, and reads a slow sheet once.** On a six-tab, 8.9 MB
workbook the sidebar stayed blank for ten minutes while it read every cell, its starters waited for that
read, and each question read the whole workbook again (about five minutes) before Prereasoner saw it.
- From its first moment the sidebar shows the reading status ("Reading 6 tabs…", the time taken, and a
  note after 20 seconds), then preparing and restoring. The starters come with the headers.
- A question asked while the sheet is read waits for that read instead of reading again.
- A sheet whose read took 15 s or more keeps its last read for later questions. Once a chat exists, the
  header shows New chat and "Syncing…" or the read's age ("Synced 5 mins ago"); a click reads the sheet
  again. A sheet that reads faster is still read before every question, so an edit is in the answer.
- These run in the hosted sidebar against the published Apps Script (v30); no new script version.

**Kept: an unread word refuses the reading.** The check still refuses a runnable query whose question
has a word it cannot read, then asks Gemini to reword. That widened the 2026-10-02 rule ("the fallback
fires only when nothing runs"). Spider DEV `whole_db` with the fallback off fell from 497 strict at
`60a55a3` to 241 at `6530bbc`, with 402 answered (`spider/results/RESULTS.md`).

The alternative was measured and is not promoted. In it, an unread word ranks the readings, and only a
reading that repeats its filter's value is refused. It scored 435 strict with 894 answered on Spider DEV
(192 wins, no losses). The planner suite then showed what Spider has no case for:
- "total amount for restaurants in United States", on a sheet without a country, served the total of
  every restaurant instead of leaving the country to the world join;
- "total Amount for premium customers" served the unfiltered total.

Spider holds no world questions, so its gain cannot weigh against these silent wrong answers. A word the
search cannot read keeps refusing the reading until the check can tell a world dependency or a
qualifier from noise wording.

Decomposition leaves still skip the check. The shipped compound demos depend on that: their leaf
wording ("units sold") is refused by the check today.

**Long headers keep their end.** A header past PostgreSQL's 63 bytes kept only its start before
" [column X]". A Forms grid's items ("...of our service? [Speed]" and "... [Support]") differ only at
the end, so they became one repeated field, and every question about either was asked "Which repeated
field should be used". A long header now keeps its start and its last 16 bytes around an ellipsis
("How satisfied are you with the fo…service? [Speed] [column B]"). `engine/column_names.py` and
`web/public/lib/workbook-import.js` name fields the same way, and a test holds them equal.

Saved analyses are snapshots, and recalculating re-plans from the question, so neither depends on the
old names. A conversation's currency claim on a column renamed this way stops applying and must be
stated again; that needs a monetary column whose header is over 63 bytes.

## Tabs of one layout are read as one, and "the total Amount" totals Amount (2026-10-05)

The owner's Stripe workbook has six tabs: three subscription exports with one layout (NT, SI and FF, 11,500
rows each, with Plan, Currency and Amount) and three report tabs (Product, Currency, Subscriptions, Total
Amount). Its starter "What is the total Amount broken down by Plan and Currency?" answered "Prereasoner
couldn't answer that right now." after four minutes. The engine ran 242 s (115 s executing readings that
no key joined), and the chat service gave up at 180 s. Three causes, each fixed in its owner:

- **"total" was read as part of another column's name.** The reports' Total Amount made "total" a word
  that qualifies "Amount" as theirs, so the exports' Amount, named by three tabs, was never a mention. A
  word that asks an aggregate before a measure's name may be the aggregate's (`sql_search._column_mentions`).
  The measure stays a mention, scored below a column whose whole name the question spells, so "total
  amount" still reads a Total Amount column wherever that column can answer.
- **Six tabs crowded every word's options.** Each word keeps four options, and with no key joining the
  tabs every draft mixed them. Tables that are copies of one layout are now searched as one
  (`SchemaGraph.layout_copies`, `SQLSearcher._unasked_copies`). A copy has three or more columns with the
  same names, and no key references it. Two-column lists such as customers' and orders' names, and code
  lookups that keys reference, are different things. The search keeps the copy the question names, else
  the first that holds a value the question states, else the first sent; the Sheets add-on sends the
  active tab first.
- **The completeness check refused the reading.** The question spells the reports' Total Amount, and the
  check required that field. SUM(Amount) now satisfies it: the aggregate the first word asks for, over the
  field the rest of the name names (`query_contract.constraint_violations`, `sql_expansion.AGGREGATE_CUES`).

**A long answer lists its first rows.** The total by Plan and Currency has 160 rows, and the reply said "the
result and its reasoning are shown in the workbook". The Sheets sidebar has no workbook, so no number was shown.
An answer of more than ten rows now lists its first ten (fewer when rows are wide, within 2,000 characters) and
says how many there are ("The first 10 of 160 rows.").

**The answer says which tab it read.** A one-table answer that other tables could give ends "From NT. SI and
FF could answer this too; name one in your question to read that one instead." (`layout_copies` in the
response). Those tables are copies of its layout, or near copies (one layout holds the other's, as an export
with one more column does) that hold every column it read. A table that only shares a column, such as
suppliers' country beside customers', is not named, and nothing is added when the question names the table. Combining the copies (a UNION of the three exports) would answer a different question
("across all tabs"), so the note offers the others instead.

**One derivation per request.** On the same workbook a request built the planner's schema and the schema
graph four times each, and FK discovery read every cell in each layer that ingested the tables. Within one
request (`relations.request_memo`) each is now derived once (`relations.memoized`) and each caller gets its
own records. The narrowed graph filters its parent's value index, each distinct cell is normalized once,
and `sql_schema.canon` is cached. World typing skips columns of numbers, on which the router abstains. The
knowledge-base membership lookup leaves out values made only of digits (dates, amounts, numeric ids), which
were most of the ~100,000 values each export sent. Four Wikidata place names are digits ("416"); no
column of them is a column of places.

Measured locally against the production database through the Cloud SQL proxy (its round trips are slower
than Cloud Run's): the question went from 99.7 s with no answer to 52.3 s with the right one. 23 s of that
is SQL, 17 s of which loads the six tabs on the conversation's first question. The live scale gate
(`tests/test_scale.py`) now serves this workbook's question, checks the totals against sums computed from
its rows, and allows 120 s; it took 38 s.

Still open: the add-on sends the whole workbook with every question (8.9 MB through Apps Script), and for an
own-data question, compose builds a world lookup and a plan that the delegate then replaces. Where a key links
a report to its export, a reading could sum the report's totals over the export's rows; that is addressed in "A
total over rows its joins repeat is served only when every reading is one" below.

**Testing the six-tab workbook in production (2026-10-05) found four more defects:**
- **The note named tabs canonically**: "From nt. si and ff could answer this too". The engine sees only canonical
  table names. It now reports them in `layout_copies`, and the chat service writes each one as the request named
  it (`request_validation.display_names`, beside `canonical_table_name`). The names reach only the reply, so the
  model feedback, the traces and the conversation's source hash are unchanged.
- **An unreadable question replied with the planner's words**, "planner: no valid AST candidate". Every
  `planner:` error now reads "I couldn't find a way to answer that from your sheets. Try asking again with the
  column names as they appear in the sheet." It is not the busy reply; asking again unchanged would not help.
- **A web upload over about 5 MB answered the demo.** The home page hands the sheets to the analysis page in
  sessionStorage, which holds about 5 MB, and the failed write was swallowed, so the analysis page ran its demo
  tables and demo question. Sheets that do not fit now wait in IndexedDB under the tab's key
  (`lib/shared.js` `SHEET_HANDOFF`): the home page, the Google Sheets picker, the analysis page and reopening a
  conversation. When they cannot be read, the page says so instead of analyzing the demo.
- **"How many subscriptions are there by Status?" had no reading.** "subscriptions" named the report tabs'
  Subscriptions column, which no key joins to the exports' Status, and every grouping took both. A projected
  column no key joins to a group's table is no longer grouped with it unless the question names it after "by";
  the search anchors on what the question asks of the rows, so such a column drops out; and the completeness
  check reads a counted noun as the rows counted when only unreachable tables hold the field it names
  (`SchemaGraph.reachable` serves both).

**Why "Prereasoner is busy right now".** Production runs one engine instance, and it answers one question at a
time (`WORLD_LOCK`); a question that waits 15 s gets the busy reply. At 23:19:45 UTC on 2026-10-04 a question
ran 242 s on the previous engine (the six-tab total, 98 s of it executing readings no key joined), and the
questions asked behind it got "busy". The same question now takes about 50 s on its first ask. Running questions
concurrently (a second warm instance, a longer admission wait, or lifting the lock for own-data questions) is
the owner's cost and capacity decision; nothing in that configuration changed.

## A total over rows its joins repeat is served only when every reading is one (2026-10-05)

Where a report tab lists each product once, discovery keys the exports' Product to the report's, and the
best-ranked reading of "What is the total Amount broken down by Plan and Currency?" summed the report's Total
Amount over that join. Each report row appears once per subscription it matches, so a product whose
subscriptions total 20 read 40. Joined on a column that holds one value in several rows, a row of the other
table repeats, and a SUM or AVG that reads only repeated rows counts each of them once per match
(`engine/sql_grounding.double_counted`). It is read per SELECT from the request's join values, so it needs no
key direction: a composite key is one combination, aliases resolve to their tables, and repeats carry outward
through the join tree.

**A preference, not an eligibility rule** (`sql_rank.select_ranked_candidate`, after the date preference):
such a member is served only when every eligible member is one. Summing a parent's column over its children is
sometimes the question, as with a course's credits over the classes that offer it, or a station's latitude
averaged over the trips that start there. 49 of the 6,953 Spider train gold queries the importer reads do it
(none of the 1,026 dev ones), so a veto would turn such answers into refusals. A ranking penalty would need a
weight above every other feature, since the report reading led the right one by 10.3 points.

**COUNT, MIN, MAX and DISTINCT are left alone.** Over one join, COUNT of any column counts the joined rows
COUNT(*) counts, so preferring another COUNT changes no number; 6 train gold queries count child rows that way.
MIN, MAX and DISTINCT aggregates do not change with repeats.

**Measured.** Spider dev (`whole_db`, Gemini off, clean checkout of `7b05b8b`): 243 strict, 310 lenient and 408
answered, as before, and 1,031 of 1,034 examples have the same SQL. Two of the three changes average the cities'
population a question names instead of each country's once per city (one gains lenient credit); one loses lenient
credit ("How many paragraphs in total?" now counts templates, as no eligible reading counts paragraphs without
summing a template's version number once per paragraph). The planner suite's regression test serves the report
workbook's totals from the exports' Amount: 20 and 7, where the report reading gave 40.

## A word the search cannot read is asked about, and the rewrite answers in seconds (2026-10-05)

A customer asked the Sheets add-on "keyword volume for home inspection checklist" over four Keyword Planner tabs.
The answer is one cell, 5,000 average monthly searches. The add-on replied after more than a minute with "I need one
more detail before I can answer that. Which interpretation should I use?", offering nothing to choose. Production
timing: 57.1 s in the chat service, 52.5 s of it in the engine, and 30.6 s of that in the selection's Gemini
fallback. Three causes, each fixed in its owner:

- **The rewrite thought until it timed out.** The search links words to columns by their names, so "volume" read
  no column, and selection asked Gemini to reword the question. Gemini's rewording was right ("What is the Avg.
  monthly searches for the Keyword 'home inspection checklist'?"), but at the model's default thinking (about
  1,200 thought tokens) it took 12 to 14 s locally and outlasted the 30 s client timeout in production. The
  rewrite now asks for LOW thinking, the model's least (about 560 tokens; MINIMAL is refused), and stops at 20 s
  (`question_rewrite.REWRITE_THINKING`, `REWRITE_TIMEOUT_SECONDS`, `llm.generate_text(thinking=...)`). Measured
  on a synthetic copy of the workbook, the question is answered in 9 s, 6 s of it Gemini's. The rewording is
  also told not to name a table the question does not name. It had added "in the Inspection table", so the
  planner's own choice among tabs never applied.
- **The coverage gate turned the planner's error into a clarification.** After the rewrite failed, the planner
  returned "no executable AST candidate". `KnowledgeQuery.serve` then checked its words against no SQL, found
  "volume" dropped and naming a Wikidata item, and returned a clarification with no reason and no proposal. A
  clarification or an error now leaves the gate as it came.
- **A clarification with nothing to choose.** When no runnable reading reads the whole question, and the
  best-ranked one reads some of its tables, columns or values, the reply names the words it left unread and
  offers the columns of its tables they could mean: "I couldn't tell which column “volume” means. Did you mean
  Avg. monthly searches, Competition (indexed value), Top of page bid (low range) or Top of page bid (high
  range)?" (`tables.unread_clarification`, `query_contract.read_question`). A quantity word (`QUANTITY_WORDS`,
  now shared by the gate and the planner) is offered numeric columns, and dated copies of a measure ("Searches:
  Oct 2025") only when no undated one is left. A question none of whose tables, columns or values were read,
  such as one in another language, keeps the generic reply. The renderer names the dropped words of any other
  clarification that has neither a reason nor a proposal.

Not changed: the search still cannot read "volume" without Gemini. The encoder's column vectors put "volume"
nearest Currency (0.43), and "search volume" nearest a dated Searches column. A synonym list would be the
question-specific patch this repository refuses. Still open: about 20 s pass in the engine before selection on a
conversation's first question, and with the value "home inspection checklist" in two tabs the reading came from
Inspection rather than the active Checklist tab. Checklist's added column ("shortlist") keeps it out of the other
tabs' layout group, and words inside the value ("inspection") tip the encoder's table signal.

## A sheet is uploaded once; a question names it (2026-10-02, shipped 2026-10-05)

Every client sent every table's CSV with every question. The Sheets add-on and the Excel add-in often sent it
twice, once to restore or sync and once to `/chat`. The chat service passed it on to each engine call, and
the engine parsed it again and rewrote the stored copy.

Now a client uploads a conversation's sheets when they change. `POST /api/conversation/sync` stores them,
starting the conversation when there is none, and returns their `source_hash`. A question then sends
`conversation_id` and `source_hash` instead of `tables`:
- `/chat` reads the stored copy once per turn for its own checks, named values and dataset-op headers. It
  caches the copy by user, conversation and hash, so a cached copy never answers a user the engine did not
  authorize.
- Each engine call names the sheets instead of carrying them.
- The engine checks ownership and the snapshot on every call. It parses a snapshot once per instance and
  reads its rows only when it has not parsed it.
- A replaced snapshot answers 409 with the stored hash, and the client uploads again.
- An unchanged source is no longer rewritten.

The web app, the add-ins and the chat service always name their sheets. External MCP clients may still
send `tables`, which the engine stores with the conversation exactly as a sync would. So there is one
serving path: the engine always serves a conversation's stored snapshot.

This removes the per-question transfer and parse. It does not remove the per-question planning cost: the
planner still reads every row in memory. A sheet of millions of rows needs the per-question work to be
bounded, a separate change to the search and selection.

**Shipped 2026-10-05, after a customer's eight-tab Subscriptions workbook spent 30 to 60 s sending 9 MB of cells
through Apps Script twice on its first question** (restore, then `/chat`; the engine call began that long after
the sheet read), and sent them again with every later one. The branch waited three days, and in that time main moved the Sheets sidebar to the hosted
`web/public/office/sheets/sidebar.js` and added a replay record per `jobId`. So the revival also:
- **Ports the sidebar and gates it on the script.** Hosting deploys at once, but Marketplace installs run the
  published Apps Script version. The sidebar uploads once only when `getSidebarContext` returns
  `uploadOnce: true`. With an older script it restores and asks with the cells, as that script expects.
  This compatibility path serves one consumer, the published v30 script. Remove the old branch of
  `sidebar.js:submit`/`restore` once the Marketplace serves a script that says `uploadOnce` (the owner
  publishes it; target 2026-10-12).
- **Restores without cells.** The new script sends none. The sidebar trusts the conversation's stored hash only
  when it equals the `syncedSourceHash` its saved state recorded with its last upload, so a copy another
  sidebar replaced is uploaded again. The engine still accepts cells from the older script.
- **Checks a named snapshot before claiming the `jobId`.** A replaced snapshot answered 409 after the replay
  record had taken the `jobId`, so the client's one re-send with the new hash met that record and got 409
  again, and the web app's retry had no bound. The engine now answers 404/409 first, and every client
  retries once.

## A question the upload reads whole needs no world lookup (2026-10-05)

When the learned compose gate fires on an own-data question (a grouping, a top-N, a sort), the compose host
builds a compose plan first, and that plan starts by looking the upload up in the world: every text column of
every tab is typed and its values checked against the knowledgebase. Production timing on a customer's
eight-tab workbook: `world_lookup_ms` 32,212 (typing 14,390, membership 14,802); 8,643 on a four-tab one. For
an own-data question compose cannot own the plan anyway: `compose_owns` needs a NECESSARY world dependency, a
world attribute or value the upload lacks.

So the host asks first. The search probe it already runs for compound questions
(`decomposition.search_probe`, no execution) now also returns its pool. If some reading in that pool reads every
word of the question from the upload (`routing.reads_upload_whole`, the completeness check's
`query_contract.read_question`), no word can create a world dependency, and the question goes to the delegate
with no compose plan and no world lookup. The `[timing]` line counts `world_lookup_skipped`. The Spider evaluator
asks the same predicate before its compose attempt; Spider tables hold no world facts, so its scores cannot
change.

A question with a word the upload does not read still builds the compose plan, because that word may name a
place, a world type or a world measure. "how many users wanted neartail for Greater than 6 months" is one: the
lookup still runs for it. A narrower lookup for such questions (only the tables a reading uses, or only the unread
words) is not done here. A place named in several words reaches the reading as several unread words, and a
word-level check would wrongly skip the lookup for it.

## The Sheets read sends formats per column and errors where they occur (2026-10-05)

The add-on's first read of a sheet returned three grids of the same size through `google.script.run`: the values,
a number format for every cell, and an error flag for every cell. On the synthetic six-tab Subscriptions
workbook (three exports of 11,500 rows by 22 columns, three report tabs) that was 24.53 MB: values 9.87, formats
10.04, flags 4.63, almost all of the flags false. A customer's eight-tab read took 120 s on its first question.

`sheets-addon/Code.js:readGrids_` now sends each column's format once, with the number cells whose format
differs (`columnFormats`, `{f, x: [[row, format]]}`), and the `[row, column]` of each cell holding a formula
error (`errorCells`). Only a number cell's format changes what the import reads (a date, a duration, a
percentage), so a text or blank cell's format is not sent; counting them would make a mostly blank date
column list every dated cell. The same workbook reads 9.87 MB, 59.8% less (58.3% when blank cells carry no
date format), and the importer's sheets and import metadata are byte-identical for both encodings. Import time
in the sidebar is unchanged.

`web/public/lib/workbook-import.js:gridWorkbook` reads both encodings. The Excel pane hands the worker the
per-cell formats and errors Office.js returns, in the same browser with no transfer to shrink, and the
published Sheets script v30 sends them too, so the per-cell form is not a compatibility path. The order of
release matters: a new script with an old importer reads dates as serial numbers
(`sheets-addon/tests/addon.test.js` fails that way), so Hosting ships the importer before the owner publishes
the script.

## A pooled reading's step budget follows the tables it reads (2026-10-05)

The customer's eight-tab Subscriptions question spent 110 s executing candidate readings (`pool_execute_ms`
110,429 over 46 readings in two pools, the search's and the Gemini rewording's). Most of them joined two exports
on a value most of their rows share, such as Currency, so their row count is the product of the tabs' rows. Each
ran to the flat budget of 100,000,000 SQLite VM steps before it counted as not running, about 2.4 s in production.

`engine/sql_rank.execution_op_limit` now gives a pooled reading 20 steps for each cell of the tables it reads, at
least 10,000,000 and at most 100,000,000. Measured with every pooled reading's steps recorded: the 14,079 readings
of Spider's dev questions all run, the heaviest in 1.1M steps (cities joined to their countries' languages, 39 a
cell, under the floor); the 440 readings of every demo dataset's prompt and follow-ups take at most 0.04M; on the
synthetic subscription workbook at 50,000 rows a tab, the largest the add-on reads, the heaviest pooled reading
takes 1.55M (1.4 a cell), hand-written key joins, an anti-join and a many-to-one sum at most 1.14M, and every row
listed in order 2.5M. No reading that runs today stops. A join of two 11,500-row exports on their currency stops
at 10.1M steps.

`TableQuery.select_query` also runs both of its pools on one in-memory copy of the request's tables and runs each
distinct query once: the rewording's pool copied every table again and reran the queries the first pool had run.
The copy inserts each table with one `executemany`, with the same content.

On this desktop, a pool of twelve such joins and the reading that answers: pool execution 10.4 s plus a 0.6 s copy
before, 1.3 s with the copy now, at 11,500 rows a tab; 9.8 s plus 2.7 s before, 5.5 s now, at 50,000. The same
reading is chosen. The 110 s in production should fall about tenfold for tabs of that size; the `[timing]` line
will show it.

## A duration compares how long rows lasted (2026-10-05)

"how many users wanted neartail for greater than 6 months" read "6 months" as the Interval value 'month' and the
number 6 as a threshold on Quantity, and "how many subscriptions lasted more than 6 months" counted the products
with more than 6 monthly subscriptions (`WHERE Interval = 'month' GROUP BY Product HAVING COUNT(*) > 6`): no rows,
where three subscriptions lasted longer. The search had no notion of how long a row lasted.

A comparison word, a whole number and a unit ("more than 6 months", "over a year", "at least 2 weeks", "less than
30 days", "6 months or more", "6+ months") is now a duration (`engine/sql_durations.py`). It claims its words, so
its number is no threshold and its unit no value, and it compares `DateSpan` (`engine/sql_ast.py`): the days from a
row's start date to its end date, or to the date the question is asked while the row has none, over the unit's
length. A span runs from a column whose name says it starts to the column of the same table whose name says it
ends and carries the same other words ("Start Date" to "Ended At", "Trial Start" to "Trial End"); a table without
an end column, or a phrase that ends in "ago", runs to the question's date. A window ("in the last 6 months") is no
duration.

The days come from each date's ISO digits by the Julian day number formula in integer arithmetic, one SQL text
SQLite and PostgreSQL both run and the Python program computes alike (`SpanValue`, `SPAN`), so no engine's calendar
arithmetic decides how long a month is: SQLite shifts Aug 31 by six months to Mar 3, PostgreSQL and Python's
dateutil to Feb 28. A month is 30.4375 days and a year 365.25. Only columns whose every value is a calendar date take part,
so the PostgreSQL rendering has no cast that can fail. A query that does not compare the span a question states
violates its constraints (`engine/query_contract.py`), and the coverage gate covers a duration the SQL compares.

The customer's question now reads its duration and asks about its other two words: "I couldn't tell which column
"users" and "neartail" mean." "neartail" occurs only inside product names ("Neartail - Startup") and "users" names
no column; reading those is a separate change. A span is a filter only: "the average subscription length" is not
read yet. The rendered span is long (about 2,400 characters of SQL each), the cost of one text both engines run.

## Questions run in parallel and the engine scales out (2026-10-06)

This replaces "Production keeps one warm engine instance and never scales out" (2026-10-01). Production ran one
8 vCPU / 16 GiB instance that answered one question at a time (`WORLD_LOCK`); a second question waited 15 s and got
503 "busy", as 2 of the 400 production questions logged from 2026-10-04 19:31 to 2026-10-05 18:11 UTC did. That
decision rested on the retired 7B's 3-5 minute cold start, which is now about 70 s, and on the lock.

The lock guarded state the requests shared on the one planner object, and taking it out alone would have been
unsafe: an audit found ten races, five across conversations. The conversation's Postgres schema and the world
query's connection lived on the shared planner, so one question could upload into, compute over and close the
connection of another conversation. Every request used one resolution connection, where one request's blocking
advisory lock stalled the rest and its deadline set another's statement timeout. The trace stream slot sent deep
code's events to whichever request set it last. The question's text, its claimed-span copy and the typing buffer
were shared too. The Qwen tokenizer serves two truncation lengths (48 and 128 tokens) and rewrites its length on
each change. The encode cache's hit and move-to-end, and the route cache's test and read, were two steps another
request could split (KeyError). Generated programs set their escalation of "Multiple rows returned with
uselist=False" inside `warnings.catch_warnings()`, which restores the process-wide filter list, so the first program
to finish lifted the other's.

What belongs to one request is now request-local: `engine/request_state.py:RequestLocal` keeps those planner
attributes in a ContextVar, `KnowledgeReasoner.serve` opens a fresh scope per request and closes the request's own
resolution connection when it ends, and `engine/trace.py:request_stream` scopes the trace emitter. The tokenizer
call (`engine/encoder.py:tokenize`) and the encode cache take short locks, the route cache is read once, the
escalation is installed once for the process, and the firebase app is initialized under a lock. Requests on one
conversation still run one at a time (a local lock stripe and a conversation advisory lock), because a request
rewrites that conversation's bridge tables. `tests/test_concurrent_requests.py` runs two requests at once for each
race, and each test fails on the old code for its race; `tests/test_geo.py` section (F) asks the same question in
two conversations at once on one planner, three rounds, against Postgres.

Cloud Run now runs two to four instances of 4 vCPU / 8 GiB and sends each at most two requests at once
(`infra/main.tf`, `min_instances` 2). It adds an instance at 60% of that limit or 60% CPU. Four instances of two
requests hold at most 24 database connections (a request holds up to three), within the 47 the `db-g1-small`
instance serves beside its reserved ones.

Parallel questions on Cloud Run decided those numbers (private copies of the new image, three streams of the demo
questions and the six-tab workbook, 94 requests, each compared with the answers given one at a time). Questions in
one process share the CPU and the interpreter lock: three at once on one instance answered 1.14 times as many per
minute as one at a time, each 2.3 times slower (median 8.6 s against 3.75 s). While one heavy question filled that
instance's CPU, Cloud Run refused other requests with 429 "The request was aborted because there was no available
instance": 2 of 94 with three streams, 28 of 94 with two. With two warm instances of two requests each, the three
streams were answered 94 of 94, all the same as one at a time, median 5.3 s, and Cloud Run started a third
instance under the load. Capacity for users at once comes from instances, not from parallel questions in one.

When no instance can take a request, Cloud Run's 429 never reached the engine. The chat's engine client asks
again, with the same jobId, after 2, 4, 8, 16 and 30 seconds, about a minute in all, so a user waits for an
instance instead of reading "busy" (`mcp_server/engine_client.py:_send`, every engine call). The engine's own 429
is JSON (a principal's rate or budget) and goes back at once.

4 vCPU was measured before the switch, on two private copies of the production image (b95e71b): the 91
demo-dataset questions and follow-ups took 397 s against 387 s on 8 vCPU (median question 3.75 s against 3.53 s),
the six-tab subscriptions workbook's three questions 41.8/20.4/38.4 s against 36.5/21.9/39.9 s, with the same
answers on all 94. The 4-vCPU copy peaked at 3.4 GiB and 64% CPU, and both started in about 70 s (57.7 s of it
loading the world reasoner). Two warm instances cost about $460 a month at list prices, what the one 8 vCPU
instance did; each added instance bills about $0.32 an hour while it runs, and Cloud Run keeps an idle one up to
15 minutes.

The build gates run the image in Docker at the same 4 CPUs, on a 32-core worker, and there it is about 2.5 times
slower than at 8: the offline regression's median case took 1.6-1.7 s against 0.6-0.7 s, the six-tab question
35 s against 16 s. The old and the new code measured alike on one machine (median 583/625 ms against 743/586 ms),
and on Cloud Run 4 vCPU cost the 3% above, so compare build latencies only across builds of the same size.

Two questions in one instance share its CPU and the Python interpreter lock, so each is slower than alone until
Cloud Run adds an instance. A cold instance's first parallel requests may each build a lazily loaded model once.

## A chat turn is one Gemini call, at LOW thinking (2026-10-06)

A chat turn is one Gemini call: its tool call carries the question to send, the analysis action and its slug (and
dataset ops when the user states a fact). Of the 400 production turns logged from 2026-10-04 18:04 to 2026-10-05
18:11 UTC, 356 (89%) made one call. 38 (9.5%) made two: the engine answered "decompose" and the model wrote the
split, or the engine could not read a follow-up and the model had one chance to rewrite it from earlier turns.
6 (1.5%) made three, a split rejected and corrected. Those later calls answer the engine's reply, so no first call
could have produced them.

The time went to thinking. The chat round set no thinking level, so Gemini thought at its default, and a one-call
turn spent 5.1 s in Gemini at the median, 10.5 s at p90 and 27 s at p99. The round now asks for LOW thinking
(`orchestrator.MODEL_THINKING`, passed through `engine/llm.py:AsyncGeminiClient.generate`). On the first round of
the 40 demo prompts and follow-ups, LOW answered in 1.7 s at the median against 2.9 s, 2.2 s at p90 against 4.2 s,
and 3.0 s at worst against 34.4 s. It sent the same question and action on all 40, and thought 0 tokens at the
median against 168.

The live orchestrator suite (41 checks, stub engine, live Gemini) passed three runs at each level, in 186-225 s at
LOW against 283-313 s. One earlier run at LOW missed one check, a "yes" after an offer the model never sees: the
intent context carries the user's questions and an engine "Try asking" offer, not the assistant's prose. That run
sent "in GBP for the whole of Europe?". Repeated ten times, LOW sent a total in Europe in GBP 9 times; the default
sent one 10 times, 8 of them with "including France and other European countries" appended.

## The hermetic suites install spaCy's tagger, and none of them skips it (2026-10-06)

Public CI's `Python lint + hermetic suites` job failed on all 44 pushes from 2026-10-03 10:51 UTC to 2026-10-06.
The last green run was `c4a9a32`.

`fce2889` ("Use schema-only Gemini rewrites for unread questions") put the completeness check's unread-word test on
every served selection. That test reads spaCy's `en_core_web_md` tagger (`engine/closed_class.py`), which
`requirements-ci.txt` never installed. So:
- `tests.test_sql_ast`, `tests.test_query_contract` and `tests.test_compose` failed with `ModuleNotFoundError: No
  module named 'spacy'`, or with its consequence: a served selection that returned the error and no result.
- Seven tests in `tests.test_calculations` and `tests.test_deterministic_emitters` checked
  `tests.spacy_model_installed()` and reported SKIP, so CI never ran the coverage behaviour they cover.
- Local runs, where the engine's packages were installed, passed throughout.

`requirements-ci.txt` now installs spaCy 3.8.13, click and `en_core_web_md` 3.8.0 at the engine image's pins.
thinc 8.3.13, blis 1.3.3 and numpy 1.26.4 match its lock as well. Both CI locks were regenerated with uv 0.9.26.
The unchanged input regenerated byte-identical first, so the lock diff is spaCy's tree alone.
`tests.spacy_model_installed()` and its seven gates are deleted: a hermetic run without the tagger now fails where
it used to skip.

Measured on a fresh Python 3.11 venv from `requirements-ci-windows.lock.txt`, on clean worktrees:
- At `c0b1f31`, the same 3 suites fail as in CI.
- With this change, all 38 hermetic suites pass, the seven tagger tests included.
- The cold install (no pip cache) took 161 s on this desktop with either lock (network-bound), and the venv grew from
  460 MB to 657 MB.
- pip-audit finds no known vulnerability. It cannot audit `en-core-web-md`, which is not on PyPI; it already could
  not in the engine lock.
- The pip that Python 3.11.0 bundles (22.3) refuses the lock's `google-auth[requests]` extra under
  `--require-hashes`, before and after this change; pip 25.2 and CI's install it.

## "All inspection checklist" is every keyword holding it, and a keyword volume's total is a volume's (2026-10-06)

The customer whose keyword sheet failed on 2026-10-05 reported it still did not work reliably. In the Sheets add-on,
over four Keyword Planner tabs:

1. "keyword volume for home inspection checklist" answered 5,000, which is right.
2. "All inspection checklist?" was read as "What is the Avg. monthly searches for the Keyword 'inspection checklist'?"
   and answered 500, for the one keyword that is the phrase rather than the keywords that hold it.
3. "total" answered "The requested total for 'Keyword' could not be computed from that field; check for
   nonnumeric/error cells or choose a numeric field".

The conversation was replayed on a synthetic copy of the sheet (the four tabs' layout and the screenshot's visible
rows; the owner's sheet was not read) through the chat orchestrator and a local engine with live Gemini. Six causes,
each fixed in its owner:

- **The chat dropped "All"** (`orchestrator/system_prompt.py`, rule 4). It sent "keyword volume for inspection
  checklist" in 7 of 24 samples. Rule 4 now says a word the follow-up adds is part of the change and stays. 12 of 12
  samples kept it, and the live orchestrator suite checks it.
- **"all" before a value meant nothing to the search** (`engine/sql_search.py`). A whole value after "all" or
  "every" that one row holds while other values hold it inside them now reads as LIKE '%value%'. "all" over a
  single row is the sign that a wider set is meant. "all Paris orders", where Paris is several rows' city, stays an
  equality.
- **A whole value right after "contain" was still one value.** In "the keywords containing 'inspection checklist'",
  the text is now one that values hold, quoted or not, compared as the sheet writes it. Spider's "the documents that
  contain the paragraph text 'Brazil'" keeps its equality, because a column's words stand between the two.
- **"containing" left every substring reading unread** (`engine/query_contract.py`). The completeness check counted
  the asking word as unread, so no LIKE reading the search has built since 2026-10-02 was ever served: the 11 Spider
  DEV questions that say "contain", "include" or "substring" all went unanswered. The words that ask for a text
  (`sql_search.substring_requests`, the search's own reading) are read once the query compares it. The contract
  now requires each asked text to be compared with LIKE, so a rewording that drops "all" is refused, not served.
  The text must be compared in the column the question names right before "contain" or "include" ("a city
  containing the substring 'West'", "the state whose name contains", "all Keyword containing"), or in the
  column whose value "all" quantified. Without that rule, reading the cue served Spider DEV 970 a reading that
  searched the street for "a city containing the substring 'West'"; its four candidate columns had scored alike,
  and the first is now scored above the rest.
- **Gemini reworded what the search had already read** (`engine/sql_prompt.py:prompt_question`). Seeing only names,
  over tabs named Inspection and Checklist, Gemini reworded "keyword volume for all inspection checklist" into keywords
  of the Inspection tab containing 'checklist' (5 of 5). With the phrase quoted, it wrote keywords "matching" it or
  the one keyword (8 of 8), both refused by the contract. The prompt now quotes the phrases the search reads as values
  and spells one it reads as held inside a column's values as "Keyword containing 'inspection checklist'": 8 of 8
  kept it. The spelling uses only the user's words, so Gemini still receives no cell value the question does not state.
- **"total keyword volume" totaled the Keyword column** (`engine/sql_rank.py:analyze_question`). The aggregate's
  target was the nearest column named after "total", so every reading was refused, and so was Gemini's right
  rewording ("the sum of Avg. monthly searches for all Keyword containing 'inspection checklist'"). A text column is
  now an aggregate's operand only where its name ends the phrase the aggregate word begins ("the total of the
  Amount"). "keyword volume" totals a volume, and "average student age" averages ages.
- **"Avg." in a column name averaged 38 keywords** (`engine/sql_expansion.py:asked_cues`, now shared by the search
  and the ranker). The rewording "What is the Avg. monthly searches for all Keyword containing ..." averaged the
  keywords' averages: 1,115.79. An average a spelled column name holds never asks for one by itself, because an
  average of rows' averages is not their average. A spelled total, maximum or minimum still asks when no other word
  does ("the total amount in Paris").

Measured:
- **The conversation.** Before the change, the local replay answered 5,000, then a clarification, then the same
  error. On the final code, 5 replays answered 15 of 15 turns as expected: 5,000, then the 38 keywords holding the
  phrase, then 42,400. Two of those replays sent the plural "inspection checklists".
- **Spider DEV** (`whole_db`, Gemini off). Strict 243 → 247 (4 wins, no loss), lenient 310 → 315, answered
  408 → 414. One newly answered question is wrong (944, an extra column; see `spider/results/RESULTS.md`).
- **Suites.** The hermetic suites, the live orchestrator suite (42 checks) and the 11 live engine suites pass. In
  `tests.test_datasets`, every dataset prompt and standalone follow-up passes; its 14 `chat:` follow-ups are left to
  the browser gate.

Not changed:
- The search still cannot read "volume" without Gemini.
- The list "All inspection checklist?" returns names no keyword unless the rewording asks for one. Adding the filter
  column to every substring listing would change Spider's projections. (Changed the same day for listings of numbers
  alone: see "A listing of numbers names the rows its text filter picked".)
- A conversation's first question still spends 10 to 17 s in world lookup and typing before selection: a word the
  upload does not read ("volume") still builds the compose plan (see "A question the upload reads whole needs no
  world lookup").
- With 'home inspection checklist' in two tabs, the reading may still come from Inspection rather than the active
  Checklist tab.

## A listing of numbers names the rows its text filter picked (2026-10-06)

On production `bc216ff`, the customer's keyword conversation answered every turn right on a synthetic copy of the
sheet. "All inspection checklist?" returned the right 38 rows, but the reply read "- 5000", "- 5000", ... "The first
10 of 38 rows." The search listed the Avg. monthly searches alone, because a listing drops the column it filters on
(30 Spider readings had listed the column they filtered, 2026-10-02). When a filter keeps several values of a text
column, those values are what tell the rows apart.

- **The rule** (`engine/sql_search.py:_rows_named`). A listing that shows only numbers (numeric columns other than keys,
  or row arithmetic) shows first the text column its filter keeps several values of. That filter is a LIKE pattern, NOT
  LIKE, `!=`, IN over two or more values or NOT IN, or an OR of one column's values. "the Avg. monthly searches for all
  Keyword containing 'inspection checklist'" now lists each keyword beside its searches. So do "... for home
  inspection checklist or roof inspection checklist", "amounts for orders not from Paris" and a top 2.
- **What stays.** These are unchanged:
  - a filter on one value, which every row holds, so it names no row;
  - a listing that already shows a text or key column;
  - aggregates, groups, DISTINCT and a single row (LIMIT 1);
  - numeric filters and set operations.
- **After the ranking, inside the search.** The ranker scores the question's words, and the label is not one of them,
  so adding it after the ranking changes no choice. The label is part of the readings `select_query` chooses from, so
  serving, decomposition leaves and the Spider evaluator see the same query. The reply renderer
  (`engine/answer_presentation.py`) could not do this: it renders the rows the engine returns, and those held no
  keyword.
- **Why only these listings.** The `bc216ff` entry left this list unlabeled because adding the filter column to every
  substring listing would change Spider's projections. That holds for listings of names: all 14 Spider DEV
  listings under LIKE show a text column. Spider DEV's gold holds 21 listings of numbers alone under a text filter.
  Every one filters on one value ("the flight numbers of flights leaving from APG"), and none uses a filter that keeps
  several values. "Avg. monthly searches for Low competition" still lists bare numbers: the column that would name
  those rows is one the question never mentions, and Spider asks for such lists bare.
- **The "X or Y" average** (`engine/sql_constraints.py`). The constraint expansion's direct OR readings took their
  aggregate from the raw words. So "Avg. monthly searches for home inspection checklist or roof inspection checklist"
  averaged the two keywords (2,525): the spelled-name rule of `bc216ff` had not reached that path. It now reads
  `sql_expansion.asked_cues`, as the search and the ranker do. "total ..." still sums (5,050) and "average ..."
  still averages.

Measured:
- **Tests.** `test_a_listing_of_numbers_names_the_rows_its_text_filter_picked` fails on `bc216ff` because the list has
  only the number column. The extended `test_an_average_a_column_name_spells_is_the_column` fails there with the "or"
  question's AVG. Both pass now, as do `tests.test_sql_ast` (196) and the hermetic `tests.run_all` (38 suites).
- **Spider DEV** (`whole_db`, Gemini off). Same answers: 247 strict, 315 lenient, 414 answered, and no example's SQL
  changed (`spider/results/RESULTS.md`).
- **Not run.** The live demo-dataset suite and the Chrome gate run at release.

## A shorthand continues the latest question, not the one before it (2026-10-07)

The Chrome gate's existing-conversation pass missed one turn the same way in four gates (gx11, gx15, gx16, gx17). It
passed once, in gx13 on `4c54f37`. In the long customer-orders conversation, the gate re-asks "total amount in France
in US dollars" and then sends "in GBP for the whole of Europe?". The chat sent "average amount in Europe in GBP", and
the reply was the average, 146.67 GBP, instead of the total of about 1,917. A fresh conversation answers the total.

- **Cause** (`orchestrator/orchestrator.py:_intent_context`). The chat model sees no transcript. Each turn it gets the
  analysis catalog and the last two user questions, which were sent as an unnamed list. Here the list was ["which
  country has the highest average amount in US dollars?", "total amount in France in US dollars"], and the model
  continued the first.
- **How it was found.** The conversation was rebuilt from the gate captures: the demo dataset, the turns and replies,
  and the catalog in the engine's order. The chat turn then ran with live Gemini against a stubbed engine that
  records the question the chat sends. The average was sent in 21 of 22 samples.
- **What did not fix it.** Two prompt changes did not help: rule 4 without its "highest average rating" example gave
  the total in 0 of 12 samples, and rule 4 naming "the user's latest question" gave it in 1 of 12. Sending only the
  latest question gave the total in 8 of 8, but it would lose a currency an earlier question set: formfacade-leads'
  "How about all of Europe?" needs the "Whats in USD" turn before it.
- **The fix.** The two questions are named `question_before_latest` and `latest_question`, on the same turns as
  before. On the rebuilt conversation, the total was sent in 10 of 10 samples. Where the latest question is the
  ranking of averages, the average is still sent in 10 of 10, as rule 4 asks. The leads follow-up still carries USD
  in 10 of 10, and a fresh conversation still gets the total in 10 of 10.

Measured:
- **Tests.** `test_the_intent_context_names_which_question_came_last` pins the named context, and
  `tests.test_orchestrator_unit` passes (48). The live `tests.test_orchestrator` passes 44 of 44, including the new
  pair: the total after a re-asked total, and the average right after the averages.
- **Not run.** The Chrome gate's existing pass, which found the miss, runs at release.

## A release review: negation scope, nearness, decimal commas and join keys (2026-10-07)

A release review of `106aefc` listed 21 behavior findings (B01–B21), 10 performance findings (P01–P10) and 3 on
accuracy evidence (A01–A03). Each was checked against the code. Five give a wrong answer, with nothing to say so,
on an ordinary question or upload; they are fixed here. The rest are agreed and scheduled below.

- **B04: a negation reached past a value.** "orders not Done in France" was served as `status != 'Done' AND
  country != 'France'`, because a negation word excluded any value within the three words after it, even past
  another value. Now it excludes the first value after it and each value that "and", "or" or "nor" joins to that
  one (`sql_search._negated_matches`). A negation word inside a value belongs to the value ("Not Started"). The
  completeness check refuses a query that excludes a value the question keeps, or keeps one it excludes
  (`sql_search.value_polarity`, read by `query_contract.constraint_violations`).
- **B11: a nearness word took the question away from the upload.** Any "near", "around" or "closest" sent the
  question to the settlement lookup before the upload was read, and whatever that returned became the answer.
  "orders near Paris" over an orders sheet came back as the cities near Paris. The lookup now takes only a
  question that asks for places: every word before the nearness word must be a place noun or a word that asks
  for, counts or sizes places (`KnowledgeReasoner._asks_for_places`). Every other question goes to the composed
  path. The lookup's answer passes the calculation gate like any other route's. A reference place with no
  longitude is skipped. Places at the same rounded distance are ordered by exact distance, then QID.
- **B02: a decimal comma read 100 times too large.** `parse_decimal` dropped every comma, so CSV cells "1,50",
  "12,34" and "2,00" totalled 1,584. A comma is now read only where it groups digits, in threes or in Indian
  lakh and crore pairs (`numeric.GROUPED_DIGITS`). Any other comma leaves the cell as text, and a text column is
  not summed. `sql_schema` and `sql_search` each kept a copy of the number pattern; both now import
  `numeric.NUMBER_TEXT`. The compose threshold reads its number with the same grouping, so "over 1,000, by
  city" still compares 1,000. The browser importer had the same flaw when a workbook's bytes are CSV text:
  SheetJS drops the commas, so "1.234,56" became 1.23456. The importer now keeps the text and leaves it to the
  engine's parser.
- **B07 and B13: join keys were read differently from the join.** Foreign-key discovery ignored case, while the
  join it planned compares keys exactly. Orders keyed ABC were linked to customers keyed abc with confidence
  1.0, and "total amount by customer name" came back with no rows. Join grounding read keys through float, so
  2^53 and 2^53 + 1 counted as one key, and a total over their payments looked double counted. Both now read a
  key with `relations.join_value`: text exactly, numbers by exact magnitude. A customers column holding both ABC
  and abc is now a key, so each order joins its own customer. Saved-reference selection no longer needs its own
  exact-value check, because discovery does that now. Its warning, for a reference that matches only when case
  is ignored, comes from running the same discovery over case-folded copies of the tables.

Agreed and scheduled, not in this release:
- **B01** (leading zeros and literal quotes lost at parse) needs typing that knows each column's role, in the
  ingestion owner. It changes the values of every upload, so it needs its own measured change.
- **B03 and B05** are gaps in the completeness check: it accepts `Amount > 10` for "equals 10", and an ascending
  order for "top 2". The search builds the right comparison and direction for these questions. A stricter check
  changes selection, so it needs its own Spider measurement.
- **B06** (a literal `%` or `_` in a substring becomes a wildcard) needs escaping in the AST, in both emitters
  and in the coverage check. **B15** (a real row named Total is set aside as a summary) needs structural
  evidence in the importer.
- **B08–B10, B12, B14 and B16–B21** cover FX carry after a stale feed, answer provenance on save, the snapshot
  gap after the upload lock, half-even division rounding, non-Latin mentions in the pre-gate, Office workbook
  identity, retry budgets, build and install identity, and release evidence. **B19** turns an explicit
  `conf=0.0` into 1.0; nothing passes zero today. **P01–P10** will be taken in order of measured cost. The
  evidence practice of **A01–A03** (a fresh `whole_db` run with per-example transitions) is followed here.

Measured:
- **Tests.** Each fix has a regression test that fails on `106aefc` for the reported reason:
  - B04: `test_a_negation_excludes_the_value_after_it_and_the_values_listed_with_it` and
    `test_an_exclusion_is_the_one_the_question_makes`.
  - B11: `test_a_nearness_word_reaches_the_place_lookup_only_for_places`. On `106aefc`, "orders near Paris" is
    answered by the settlement lookup.
  - B02: `test_a_comma_is_read_only_where_it_groups_digits` and
    `test_a_decimal_comma_amount_is_not_totalled_as_hundreds`. On `106aefc` the total is 1,584. The importer
    check in `web/tests/workbook_import.test.js` gets 150 and 1.23456 on `106aefc`.
  - B07 and B13: `test_join_keys_are_read_as_the_executed_join_compares_them`. It fails on `106aefc` with
    either the released discovery or the released grounding.
- **Suites.** These pass: `tests.test_sql_ast` (199), `tests.test_query_contract` (35), `tests.test_compose` (29),
  `tests.test_master_ingest` (13), `tests.test_numeric_storage` (5), `tests.test_enrichment` (36), `npm run test:web`,
  ruff, bandit and the dependency locks. The hermetic `tests.run_all` passes 37 of 38 suites. The exception is
  `tests.test_release`, whose link check reads every Markdown file under the root: an untracked review file there
  links to absolute paths. Over the tracked Markdown, the same check finds no missing link.
- **Unchanged elsewhere.** On the six multi-table demo datasets, discovery finds the same 9 foreign keys. No cell
  in the shipped demo CSVs or in Spider's 160 databases reads differently. Discovery on a 37,000-row three-table
  workbook takes 0.18 s, against 0.21 s on `106aefc`.
- **Spider DEV** (`whole_db`, Gemini off). Same answers: 247 strict, 315 lenient and 414 answered, and no
  example's SQL changed (`spider/results/RESULTS.md`).
- **Not run.** `tests.test_datasets` and `tests.test_geo` need the live database, and its proxy was not running.
  They run at release, with the Chrome gate.

## Release review, revision 2: mixed keys, percentage cells, cut references and whole numbers (2026-10-07)

Revision 2 of the release review rechecked `5f8c57c`. It found B02 and B13 fixed and reported seven new
findings (N01–N07): three where the B04, B07 and B11 fixes did not cover the whole failure, and four new ones.
Each was checked against the code and reproduced. All seven can give a wrong answer or a wrong record with nothing
to say so, so they are fixed here. So are seven findings the first entry scheduled, each a contained fix in its
owner: B08, B09, B12, B14, B17, B18 and B19.

- **N01: a key column holding numbers and text.** Discovery read each cell on its own, so orders keyed `1.0, 1.0,
  2.0, ABC` were linked to customers keyed `1, 2, ABC`. A column holding any text is stored as text, so the join
  compared '1.0' with '1' and kept 1 of the 4 orders. Keys are now read a column at a time, as the schema stores
  the column (`relations.join_keys`, through `numeric.stored_as_numbers`, which `TableQuery._schema` also uses):
  by exact magnitude when every filled cell is a number, otherwise by each cell's exact text. A column of numbers
  never matches a text column. `relations.join_value` is removed; join grounding reads keys the same way.
- **N02: a percentage cell was read as its stored fraction.** Excel stores 20% as 0.2, and the importer wrote 0.2.
  The rate rule divides a percent-named column by 100, so "total price including tax" applied 20% as 0.2%. A
  numeric cell with a percentage format is now written as the percent it shows ("20%", "7.5%", "-5%"), from a file
  and from a Sheets or Excel grid alike (`NUMBER_FORMAT.isPercent`). The engine reads "20%" as 20. The same
  fraction without a percentage format stays a fraction.
- **N03: the place lookup still took questions it could not answer.** Its word check read ASCII letters only, so
  "売上 near Paris" had no words before "near" and went to the settlement lookup. It also let through kinds and
  orders of place its query does not select ("capitals near Paris", "the smallest towns near Lyon"). Words of
  every script now count, and only the place nouns and size words the query honours pass
  (`knowledge.PLACE_QUESTION_WORDS`).
- **N04: an exclusion read through LOWER escaped the polarity check.** The check read only a bare column compared
  with a literal on its right, so `LOWER(status) = 'done'` passed for "orders not Done in France". It now reads a
  column bare or through LOWER, on either side of the comparison, and in literal IN and NOT IN lists
  (`query_contract._value_comparisons`). It also refuses a query that leaves an excluded value in, unless a negated
  subquery makes the exclusion. For this, `sql_search.value_polarity` returns one set of readings per excluded
  value.
- **N05: an evaluator checkpoint ignored the databases and most of the model bundle.** A changed Spider database or
  threshold file kept a `--resume` checkpoint valid. The contract now hashes each database the picked examples read
  and the whole validated bundle (`full_eval._input_paths`, `artifact_provenance.validate_weight_bundle`).
- **N06: a number in a question was split.** The word splitters of questions, cells and the completeness check cut
  "1,000" into 1 and 000 and "2.5" into 2 and 5, so "orders over 1,000" found no query. A number is now one word
  (`numeric.NUMBER_WORD`), and a grouped number compares as its value (`sql_schema.canon`,
  `sql_expansion.parse_number`).
- **N07: a saved reference was cut to a key-order prefix.** A request reads at most 5,000 rows of a reference,
  which may hold 50,000. Discovery saw only the first 5,000 keys, and the table kept only those rows. With 5,001
  products, and orders naming the first 100 and the last, "total amount by category" lost the last product's 2
  orders without a warning. Discovery now reads every saved key. A reference over the budget keeps every row the
  working tables name, then fills the budget in key order, and a warning says how many rows it left out. A
  reference naming more rows than the budget is not used, and a warning says so (`master._request_rows`).
- **B08: a rebuild made an old FX rate current.** The carry window ran from the build day, so a January 1 rate
  rebuilt on February 1 stayed valid through February 8. A foreign rate now carries 7 days past its own last
  print. EUR identity rows still lead the build day.
- **B09: an old answer was saved as current.** `save_state` replaced a snapshot's `sourceHash` with the
  conversation's newest hash. An answer computed before another tab's upload therefore looked current. The
  snapshot keeps its own hash, the response returns it as `answer_hash`, and the browser marks the answer stale
  when it differs from the conversation's.
- **B12: one division, two roundings.** SQLite's decimal helpers rounded half to even and left AVG unrounded, while
  DIVIDE rounded half up, so 1/2097152 differed in the last place. Every division now goes through
  `numeric.decimal_divide`.
- **B14: a question naming a non-Latin column read as chat.** The pre-gate split questions and names into ASCII
  words, so "売上の合計" over a 売上 column was answered as conversation. Words of every script count now, and a
  non-ASCII column name counts wherever the question contains it, since such scripts are written without spaces.
- **B17: connect retries ignored the shrinking budget.** The connect timeout was read once, so three attempts on a
  2-second budget could each wait 2 seconds. Each attempt now reads what is left, and no backoff starts past it.
- **B18: a local runtime identity hashed no files.** It scanned `engine/engine`, so without `K_REVISION` or
  `GIT_COMMIT` it was the hash of an empty map. It now scans the engine package and raises if it finds no source.
- **B19: an explicit zero FK confidence became 1.0.** Only an absent confidence defaults to 1.0 now.

Agreed and scheduled, not in this release:
- **B01, B03, B05 and B06**, for the first entry's reasons: each changes selection or the values of every upload,
  so each needs its own measured change.
- **B10** (the gap between the upload lock and execution) needs execution bound to the upload's transaction, and
  tests that interleave real PostgreSQL transactions. **B16** (the workbook identity of an unsaved Excel document)
  needs a real Office host. **B20** (atomic bundle replacement) and **B21** (a release gate bound to full
  identities) change installation and release tooling, not answers.
- **P01–P11** will be taken in order of measured cost. N07 adds to P10: discovery now reads every saved key.
- A column named as a fraction but formatted as a percentage now reads 20 for 20%, and the rate rule, which
  trusts the name, applies it as 20. The engine does not yet take a unit from "%" cells; that belongs with B01's
  typing by column role.

Not a blocker for this release:
- **B15** (a row named Total set aside as a summary). The importer moves a row starting with Total, Subtotal or
  Grand total below the data into a summary table, and a warning says so. Real totals rows are far more common than
  an entity named Total, and keeping them as data would double every sum. Formula evidence would sharpen the rule;
  that is the importer change scheduled in the first entry.

Measured:
- **Tests.** Each fix has a regression test that fails on `5f8c57c`. The failures match the reported defects:
  - N01: `test_join_keys_are_read_as_the_executed_join_compares_them`. `5f8c57c` links orders keyed 1.0 and ABC
    to customers keyed 1 and ABC.
  - N02: `web/tests/workbook_import.test.js` (0.2 on `5f8c57c`), with
    `test_a_percentage_cell_applies_the_percent_it_shows` for the computed amount: 173.75, where the 0.2 cells
    give 150.24.
  - N03: `test_a_nearness_word_reaches_the_place_lookup_only_for_places`.
  - N04: `test_the_excluded_value_is_excluded_whatever_wraps_its_column`.
  - N05: `test_spider_results_fingerprint_the_databases_they_read_and_the_model_bundle`.
  - N06: `test_a_number_in_a_question_is_one_word_however_it_is_written`.
  - N07: `test_a_reference_over_the_row_budget_keeps_every_row_the_request_names`, with
    `test_a_reference_naming_more_rows_than_the_budget_is_not_used_and_says_so` and
    `test_a_chain_through_a_cut_reference_reads_the_rows_its_kept_rows_name`. `5f8c57c` totals 200 of 202 orders.
  - B08: `test_rebuilding_a_stale_feed_does_not_make_its_rates_current`.
  - B09: `test_a_saved_answer_keeps_the_source_it_read`, plus the saved-answer check in
    `web/tests/workbook_reference.test.js`.
  - B12: `test_one_division_rounds_the_same_in_both_backends`.
  - B14: `test_a_question_naming_a_column_in_any_script_is_about_the_data`.
  - B17: `test_postgres_connect_retries_wait_only_for_the_budget_left`.
  - B18: `test_runtime_identity_without_a_deployment_names_the_engine_sources`.
  - B19: `test_an_explicit_zero_foreign_key_confidence_stays_zero`.
- **N07 served end to end** (the production selection over a mocked store, then the planner):
  - With the last key named, 202 of 202 orders are totalled (`5f8c57c`: 200).
  - With only keys past the prefix named, all 202 orders are totalled (`5f8c57c`: 200).
  - With a rare tail key named 80 times, 100 of 100 orders are totalled (`5f8c57c`: 20).
  - With a 50,000-row reference, it is used and 202 of 202 orders are totalled. `5f8c57c` drops the reference
    and returns no rows.
  - Selection over the 50,000-row reference takes 0.27 s (median of 5, mocked store), against 0.02 s on
    `5f8c57c`, which dropped the reference without loading it. The real catalog read now carries up to 50,000
    keys of a reference instead of 5,000.
- **Unchanged elsewhere.** On the six multi-table demo datasets, discovery finds the same 9 foreign keys. The 9
  shipped demo workbooks import byte-identically.
- **Suites.** The hermetic `tests.run_all` passes 37 of 38 suites, among them `tests.test_sql_ast` (201),
  `tests.test_query_contract` (36), `tests.test_calculations` (175), `tests.test_compose` (30),
  `tests.test_master_ingest` (16), `tests.test_numeric_storage` (6), `tests.test_enrichment` (37),
  `tests.test_source_sync` (29), `tests.test_conversations` (24) and `tests.test_request_limits` (26). The
  exception is `tests.test_release` (41 of 42): its link check reads every Markdown file under the root, and the
  untracked `release-review/.md` links to absolute paths. `npm run test:web`, ruff, bandit, the dependency
  locks, compileall and `git diff --check` pass.
- **Spider DEV** (`whole_db`, Gemini off). Same answers: 247 strict, 315 lenient and 414 answered, and no
  example's SQL changed (`spider/results/RESULTS.md`).
- **Not run.** `tests.test_datasets` and `tests.test_geo` need the live database. The reference store's real
  PostgreSQL reads were mocked; the catalog's only change is its row limit.

## Where a served question's time goes (2026-10-08)

A baseline for the performance work (P01–P11 of the release review), from production's own `[timing]` lines
(`engine/request_timing.py`; chat and engine) for 2026-10-06 and 07: 816 engine requests and 737 chat turns, the
Chrome gate's included. Each engine line partitions its total into spans' self times. Medians are over the
requests in which a step ran.

The chat turn: median 9.0 s, p90 16.1 s.

| Step | Median | p90 | Runs in |
|---|---:|---:|---|
| Chat model (Gemini): the standalone question and the engine call | 2.0 s | 5.0 s | every turn; 89% need one call |
| Engine request (network to it 0.02 s) | 5.0 s | 12.3 s | every turn |
| Reply rendering and save | 0.15 s | 0.2 s | every turn |

The engine request, in execution order (own-data questions; median 4.4 s):

| Step | Span | Median | Share | Runs in |
|---|---|---:|---:|---|
| Request handling: auth, parse, response | unattributed | 0.28 s | 6% | all |
| Postgres connect | `pg_connect` | 0.46 s | 8% | all |
| Sheet upload into Postgres | `upload` | 0.01 s | <1% | 89% |
| Column typing, first sight of a sheet | `typing` | 2.1 s | 7% | 18% |
| Encoding: tokenizing and the encoder | `encode` | 1.2 s | 22% | 73%; the rest hit the cache |
| Compound probe, compose, world lookup | `decompose_probe`, `compose`, `world_lookup` | <0.1 s | ~1% | 30–40% |
| Typed AST search, ranking and SQL construction | `serve` self | 0.56 s (p90 6.5 s) | 27% | all |
| Candidate execution and grounding on SQLite | `pool_execute`, `pool_grounding` | <0.1 s | <1% | 66% |
| Rewrite fallback: Gemini and the second search | `fallback` | 5.6 s (p90 9.4 s) | 1% | 1.3% |
| Postgres statements: final query, upload, typing, world (about 66 a request) | `sql` | 0.7 s | 19% | all |
| Python parity check of the answer | `deterministic_python` | 0.03 s | 1% | 89% |
| Progress streaming to the browser (Firebase) | `rtdb` | 0.46 s | 9% | all |

- A question needing world data takes 10.9 s median, 5.1 s of it in the search step.
- A sheet's first question takes 8.6 s median, against 4.1 s for later ones. Typing (2.2 s) runs only then, and
  encoding takes 1.67 s against 0.78 s (19 texts missing from the cache against 5).
- Encoding a sheet's column names when it syncs would save about 0.9 s, and typing it then about 2.2 s, on that
  sheet's first question only. Both caches live in one engine instance's memory, and production runs 2 to 4
  instances, so the saving needs the results stored with the sheet in Postgres. Syncing when the sidebar opens
  would also start a conversation for every opening. A later question's encoding is its own phrases, which
  nothing can compute before the question.
- Not yet split out: tokenizing from encoding, the search from ranking and SQL construction (`serve` self), and the
  final query from the other Postgres statements. The rewrite span holds Gemini's call and the second search
  together.

Decision: the next performance work, in order of measured cost per question:
1. Reuse Postgres connections: 0.46 s on every request.
2. Stream progress without blocking the request: 0.46 s on every request.
3. Send fewer Postgres statements, including the timeout setting sent before each: about 66 a request (P02).
4. Split `serve` into search, ranking and construction spans before optimizing it.

Pre-computing the encoding and typing waits until the sheet's results can be shared across instances.

## A saved chat shows when the Sheets sidebar opens, and a starter asks itself (2026-10-08)

- **The chat waited for the whole read.** The sidebar asked for the sheet's saved chat only after
  `getSidebarContext` had read every cell, so "Reading 6 tabs… 1:27" stood in for the conversation. The upload-once
  script (v31) restores without the cells, so the sidebar now asks when it opens, alongside the read. The header
  shows "Syncing…" until the read finishes. The v30 script refuses a restore without cells and keeps the restore
  after the read, the compatibility path recorded on 2026-10-05.
  - Measured in Sheets on a 4-tab sheet, from the menu click: the chat showed at 7.5 s, against 15 to 17 s before.
    About 4 to 5 s of that is Google loading the add-on frame.
- **Layout.**
  - The header keeps 14 px above the first question; the shared rail's turn has no top margin.
  - The composer sits on the side panel's own surface, not on a white band.
  - Send is an icon that turns into a spinner while busy, in place of the "↑" character.
- **A starter asks itself.** A starter question only filled the composer, and the user had to press send too. The
  shared component (`lib/sidebar-suggestions.js`) now calls its host's `ask`, in the Sheets sidebar, the Excel task
  pane and the web home page alike. A draft in the composer stays there.

## A question word is read from the tables the query reads (2026-10-08)

From the SQL-accuracy review of 2026-10-08 (W04). A loss ledger of every Spider DEV question showed
the completeness check refusing 489 questions that had a runnable, grounded candidate, 194 of them right. The check
(`engine/query_contract.py:read_question`) and the search had read the schema differently.

- **Scope.** Any column of any table counted as read, so an answer over the students' ages was complete for "the
  average weight of pets", because the pets table has a weight column. A word is now read when it names a table the
  query reads or a column of one. The tables and the columns the query uses are read in the words the search splits
  their names into (`engine/sql_schema.name_words`, now the one splitter): "LifeExpectancy" is "life expectancy".
  The other columns of those tables count only as written, so "official languages" is unread by a count that leaves
  IsOfficial out.
- **Families read by the query's structure.** A name spelled in two words ("high schoolers", Highschooler); a spelled
  number the query keeps ("the two oldest"); order words in a query that orders its rows; a participle whose own noun
  phrase names a compared value ("flights departing from APG"), unless the data holds the participle as a value
  ("orders returned by Alice" over a status holding 'Returned'). The object stops at a relative clause: "staff
  working for the museums that were opened before 2009" is unread over a query averaging the opening year.
- **Counted nouns.** Across several tables a counted noun reads as the rows counted only when it is a field's name
  ("how many subscriptions by Status"). "How many people live in Gelderland" was served as a count of cities when
  any noun counted.
- **Not done.** Reading graded adjectives ("youngest") when a query orders: simulated on the ledger, 19 right and 27
  wrong answers. A cue word that is also a column ("average" and a column Average): it needs projection and grouping
  changes for at most three Spider questions. Grouped answers keep their groups first, though Spider's gold often
  lists the aggregate first; that is a product choice, not a defect.
- Measured (`spider/results/RESULTS.md`): engine alone 247 → 315 strict (69 wins, 1 loss), the share of answers that
  are right 59.7% → 60.1%; with Gemini on 341 → 386 (52 wins, 7 losses, six of them the rewording's run-to-run
  variance), and 110 fewer rewording calls.

The dormant structural-profile search path was removed in the same change: `engine/sql_profile_expansion.py`,
`engine/sql_profile.py`, `spider/probe/ast_profile.py`, `SemanticSignals.sketch_profiles` and the search's
`profile_*` parameters. Serving never passed a profile, so every served query was unchanged (Spider: all 1,034
examples identical).

## The Community install converts currency on its first day (2026-10-08)

The v0.3.2 launch test (CE-GCP-001, a brand-new project) installed cleanly, and the default question, "total amount in
France in US dollars", answered "No rows in your data match this question".
- **Exchange rates.** The seed's ECB rates end a week after it was built (`build_exchange_rate.CARRY_FORWARD_DAYS`),
  and the deployment's daily refresh first runs at 16:30 UTC. A conversion joins rates on today's date, so every row
  dropped. Since installs moved to the seed (`5f10512`, 2026-09-15) every fresh install did this until its first
  refresh; v0.2.23 passed because its seed was a day old. The installer now runs the deployment's one refresh job
  once after the seed import. Running it by hand on the test install gave 1,084.17 USD.
- **The reply hid the reason.** An empty total whose calculation is unmet now says which calculation could not be
  made (`knowledge_query.verify_nonempty`), not that no rows matched.
- **A new account's first requests.** The master lookup and the conversation sync both insert the account's
  principal; the second collided on `principal_id`, which `ON CONFLICT (firebase_uid)` does not absorb, and answered
  500. The insert absorbs a conflict on either key.

## Messy data changes no answer silently (2026-10-08)

A planted-text test (instruction-like text planted in the cells, headers and sheet name of the customer-orders
sheet) found no planted instruction obeyed, and five ways ordinary messy data changed an answer or its reason.
- **An unknown city is said to be left out.** "Brussels (a city in Germany)" resolved to no city, and "total amount
  in Belgium in USD" lost the order with complete coverage and no warning. Both world joins now count the rows whose
  place matched nothing through the one owner, `knowledge_query.unmatched_rows`: the composed world join
  (`compose.py`) and the place joins of the conversion path (`knowledge_tables.py`, from the values its meaning join
  keeps). The reply names them ("1 of 23 source rows could not be matched (Brussels (a city in Germany)) and were
  excluded"), and `unmatched_clarification` declines when they are most of the rows, the rule of 2026-10-02.
  Resolving the cell to Belgium was rejected: its own words say Germany.
- **A named measure is never replaced.** One amount "118 (accounting says 11800)" keeps the column text (the typing
  rule stands), and compose then summed the cities' populations for "total amount of GBP orders in Europe". Compose
  now refuses a total or average of a column the question names whose cells are not all numbers, and the reply
  names the cells (`query_contract.unreadable_measure_reason`), as the own-data planner and the currency check do.
  The currency check had blamed a missing currency column.
- **The rows a question totals are no grouping.** On a sheet not named "orders", "orders" loose-matched the `ordered`
  column and the GBP total became five per-product totals. The noun a question measures or counts
  (`closed_class.measured_rows`, `counted_rows`) is claimed like the sheet's own name.
- **A row with no amount needs no exchange rate.** An empty row declined "which city has the highest total amount in
  US dollars" as 1 of 24 rows without a rate; the rate coverage counts only rows whose measure is filled.
- The chat's Gemini turns run at temperature 0 with seed 0, as the module documents for every call.
- Not done: recording how each place was matched (exact, alias or embedding with its score). It changes the
  persisted bridge's columns and every reader of them, and needs the live database to verify.

## The review of the messy-data fixes: operands, roles and relationships are bound (2026-10-08)

An independent review of 8b8a7ee and 30a8cb1 found the new acceptance rules too permissive, and reproduced each case
on the production owners.
- **F1, a measure replaced again.** The guard against totaling another column fired only when most of the named
  column's cells were numbers, counted among the rows the question keeps; with most amounts malformed, compose summed
  populations again. The operand is now the column the aggregate phrase names (`sql_rank.aggregate_operand`, built
  on the phrase test `analyze_question` uses), and any cell of it that is not a number refuses, however many there
  are. "order" before a noun no longer ends that phrase ("the total order amount"); "order by" still does.
- **F5, one decision.** Serving answered compose's refusal while the Spider evaluator handed the question to the AST
  planner. `routing.route` now returns `Route.CLARIFY` for a refusal, and both call it.
- **F2, alphabetical.** Any ordering read "in alphabetical order", by age or Z to A. It is read only by an order on a
  text field, A to Z unless the question reverses it, and the search builds that order for "in alphabetical order"
  as it did for "alphabetically" (`sql_expansion.ALPHABETICAL_WORDS`).
- **F3, spelled numbers.** Any equal number in the SQL read "the two", so a 2 in an excluded ID read it over a query
  keeping three rows. In a question that ranks the number is the query's LIMIT; after a comparison cue, a compared
  number.
- **F4, participles.** A compared value read "departing from APG" whichever column compared it. A value has one
  relationship to the rows when it selects the same rows whichever column of the query's tables holds it (a
  country's Name and LocalName), and no second foreign key joins its table to another the query reads. With more
  than one, the ranker's travel reading decides (`sql_rank.travel_direction`, `travel_column_role`, now shared):
  "departing" is read only over the source column or the join on it. A participle with no such reading ("orders
  shipped to Paris" over billing and shipping cities) stays unread, and the question is refused or reworded.

## A named measure that is no number is refused on every route (2026-10-09)

The planted-text test, repeated in the browser on the owner's account, failed where the local runs had passed. With
order 101's amount "118 (accounting says 11800)", "total amount in France in US dollars" answered 223.47 and "which
city has the highest total amount in US dollars" Toledo. The account holds a saved reference `ordered` with a numeric
`estimated amount`; the world path's measure reading (`read_op_all`) took it when the uploaded `amount` was text, and
summed it unconverted (the reference has no currency column, so USD read as its unit). The own-data search did the
same for "which city has the highest total amount", keeping `amount` as a grouping column beside a total of the
estimated amounts, which the requested-total check passed as a ranked grouping. Without a reference the world path
counted the rows: "total amount in France" was 5.
- **One rule.** `query_contract.unreadable_operand` names the column a total or average names most fully
  (`sql_rank.aggregate_operand` over every column, longest names first, so "the total estimated amount" still totals
  `estimated amount`) when every column of that name is text and holds numbers and a cell that is no number. A
  column with no number at all (dates, a Yes/No status) is no measure: the release's dataset gate refused "total
  transfers signed in August" for the dates of `signed`, its participle read as the operand. The planner's
  `constraint_violations` rejects every candidate for such a question, `KnowledgeQuery.serve` refuses before either
  world route reads a measure, and the currency check calls the rule instead of its own copy, so an average says
  "averaged".
- **Not stricter than before.** The rule fires only where no route can total the named column, so the refusal
  replaces a row count or another column's total, never an answer from that column. Compose keeps its rule over
  the rows the question keeps; the planner and the world routes read the whole column, as the currency check did.
  An amount column with no number left in it is no longer refused by name on those routes (compose still refuses
  it); the price of reading no participle's dates or statuses as a malformed measure.
- **The reply.** The refusal sentence and its matcher moved to `engine/answer_presentation.py`, which the chat image
  ships, and a refusal naming cells no longer ends "Which interpretation should I use?". One unmatched row "was"
  excluded ("1 of 23 source rows ... were excluded" before).
- Spider DEV whole_db is unchanged: 314 strict / 380 lenient / 523 answered, every served SQL and refusal reason
  identical (`spider/results/RESULTS.md`). The live world suite checks the four browser questions and three
  without a currency, beside a saved reference and alone (`tests/test_world.py`, P).

## A non-geo name takes its nearest only when the spelling agrees (2026-10-09)

A non-geo name with no exact match (a bank, university, hospital, school...) resolved to the nearest
`knowledgebase.words` name of its type at cosine 0.85. That threshold lets an embedding match a name the
knowledgebase does not hold to a different entity. A read-only sample from production (banks, universities and
hospitals, every row embedded, 400 entities each) found the following:

- Of 1,200 labels whose own entity was held out, 557 still matched another entity at 0.85.
- Hospitals, the one type that had embeddings, did so in production for 163 of 300 such names.
- 735 of 1,167 one-letter typos matched their own entity, and 44 matched another.

A nearest name now also has to be spelled alike: difflib's ratio of the two normalized names at least 0.9
(`knowledge_query.nearest_name_matches`). In the same sample all 735 correct typo matches stay, no typo matches
another entity, and 50 of the 1,200 held-out labels still match, names one letter apart from another entity. A
shortened name no longer matches its longer one: 62 of 922 do, against 681 before. It is disclosed as unmatched
(`unmatched_rows`), not counted as whatever entity is nearest. The owner chose this rule over the looser one
(2026-10-09).

On the demo sheets, "Mayo Clinic" took "Mayo Clinic Health System" (cosine 0.91, spelling 0.62). It is now
disclosed, and the hospital-transfers prompt's US total is 32, not 46. "Toronto General Hospital" still takes
"Toronto East General Hospital" (spelling 0.92), a different hospital in the same city, so the rule does not
remove every wrong match.

366,582 `words` rows, all non-geo labels, had no embedding: banks, universities, schools, taxa and more,
registered in bulk outside this repository, since every repository writer embeds its rows. So no misspelled bank or
university name could resolve. `db/sync/build_words.py --embed-missing` embeds them in committed batches,
idempotently. It runs only after this rule is in production; under the 0.85 rule alone, 118 of 300 sampled bank
names and 143 of 300 university names the knowledgebase lacks would have matched another entity. The backfill
does not affect city lookups: for 1,460 sampled city typos the nearest city stays among the 40 nearest names of
the whole index, which the type-filtered HNSW scan reads.

The backfill ran on 2026-10-09, after this rule was released (engine `prereasoner-api-00163-f52`). It embedded all
366,582 rows in about 11 hours (10:09 to 21:20 UTC). The HNSW index grew from 443 MB to 1,110 MB and the table from
1,314 MB to 2,438 MB, more than the `db-g1-small` instance keeps in memory, so later batches waited on disk reads.
Warm lookups are unchanged: a city typo takes 120 to 315 ms and a non-geo nearest name 120 to 470 ms. The first
lookup of a large type after its pages are evicted is slow, though: 12 s for schools (46,231 rows) and 10 s for
hospitals. The live non-geo suite and the hospital, bank and catering demos pass on the backfilled data.

## Constraints are met by the whole query, and a graded word is read in its direction (2026-10-10)

A review (2026-10-09) found unanswered Spider questions mixed false refusals with missing plans. An evaluation-only
census of every pool member of every DEV question located them on `de0cfba`: 324 served right (graded on the raw
SQL), 173 with a right candidate the completeness gate refused, 115 with one the constraint check excluded, 53
ranked below a wrong one, and 369 with none built. Relaxing a check or reading a word blindly would serve far more
wrong answers than right ones (every word read: 129 right, 245 wrong; the gate's unread signal is real), so each
rule below was replayed on the census before it was written, and kept only for at least two right answers per new
wrong one.

- `query_contract.constraint_violations` judges a set query branch by branch for the result's shape and as a whole
  for what the question asks of the rows. Each branch once answered for the whole question, so an INTERSECT of
  "before 1945" and "after 1955", and an EXCEPT of the poker players for "people who do not play poker", were always
  refused. An EXCEPT makes an exclusion, its subtracted branch keeping the values excluded; a comparison is realized
  in an OR of alternatives the question offers, by a HAVING COUNT for a threshold on counted rows ("more than 2 car
  makers"), and inside IN, EXISTS and scalar subqueries.
- `sql_expansion.nearby_operator`: the cue nearest the number sets its comparison ("before 1945 and after 1955"), and
  every comparative before "than" sets it (`comparative_operator`), negated ones their complement ("no less than
  3000" is at least, "not higher than 4" at most), and a comparison continuing the one before ("age above 35 and below
  50") compares the same field. `COMPARATIVES`, `QUANTITY_COMPARATIVES` and `COMPARATIVE_COLUMNS` moved from
  `sql_search` to `closed_class`, the lowest layer, which `closed_class.EXCLUSION_CUES` (no longer reading any negated
  comparative as an exclusion), `sql_expansion` and the search all read; the search builds the corrected comparisons
  too.
- A comparison keeps where it sits (`query_contract._select_queries`, `_scoped_match`): an OR or a UNION realizes
  only alternatives the question offers, a scalar subquery filters no returned row, and a subtracted one (an EXCEPT's
  right branch, NOT IN, NOT EXISTS) realizes its complement, or itself when the question negates before it. A review
  the same day showed the first whole-query check accept a UNION and an EXCEPT for "above 35 and below 50", a scalar
  subquery comparing ids for "people above 35", and refuse "not older than 35" for a missing exclusion; each is now a
  regression case.
- `query_contract.read_question` reads a graded word only when the query realizes it in its direction on a field of
  its kind (`_realizes_grade`), from `sql_extrema`'s superlative cues, `superlative_direction` and `frequency_cue`
  (made public) and `sql_expansion`'s comparatives. A rule reading such words without direction had served 27 wrong
  answers for 19 right (2026-10-08). The search orders an age from the oldest by `superlative_direction`: "oldest"
  was ascending on every field, the date sense.
- The field check: a field's name inside a longer field's name the question also says ("the name of the song" is
  `Song_Name`), unless a table the question names and the query reads holds the field itself ("the name of the
  stadiums"), or inside the name of a table the query reads ("car makers"); and an aggregate word that is also a
  column's name ("Average") is the aggregate when its phrase names another field the query aggregates
  (`sql_rank.aggregate_operand`).
- `sql_rank.CandidateRanker`: a name the question says once binds one field (`one_binding`), so "the name of the
  employee" is not also a shop's name.
- `spider/probe/full_eval.py` labels a refusal by its cause: the completeness gate, the constraints, or no
  candidate. All three once read "no executable candidate".

Measured (`spider/results/RESULTS.md`, 2026-10-10): engine alone 314 → 369 strict (56 wins, 1 loss), Gemini on
385 → 452 (75 wins, 8 losses); expected beforehand +40 to +50 and +20 to +35. Strict of answered rose (60.0% →
62.3%, 57.6% → 60.3%), yet 32 newly answered questions are wrong, most from construction defects the gate had
hidden (a HAVING COUNT over the wrong table, a relationship read from its other side, "fewest" counting groups with
none). Left as they were: column order (a product choice), a LEFT JOIN for "fewest", and the 369 questions with no
right candidate built, where the review's shared reading of operator, operand and population would apply.

## An exclusion subtracts the entities it names, and one aggregate word takes the fields it coordinates (2026-10-10)

The step-1 review's next package: identity-safe exclusion and repeated aggregates. Each rule was measured on Spider
DEV before it was kept, with Gemini's rewordings replayed from one cache so that a run's difference is the engine's.

- `sql_extrema`: a set difference whose projected fields name no entity ("the major and age of students who do not
  have a cat") keeps the entity's key NOT IN the related rows' keys (`extrema:anti-member`); an EXCEPT of (major, age)
  removed a student sharing both with a cat owner (DEV 61). `SchemaGraph.identifies` decides it: a column holding no
  value twice, or a foreign key to one. `sql_recursive` builds its anti-membership EXCEPT only on such columns.
- `query_contract.constraint_violations`: a table joined inside an exclusion only to filter (no column read outside
  the joins, joined to one other table) that the question does not name narrows what the exclusion removes, and is a
  violation. "Templates not used in any documents" subtracted only documents with paragraphs (DEV 316); "employees who
  never received any evaluation" subtracted only those also hired (DEV 282).
- `sql_expansion.numeric_comparisons`: a comparison continuing the one before inherits its field only when no noun
  follows its number; "a grade of over 5 and 2 or more friends" compares friends, not the grade (DEV 911).
- `sql_search`: one aggregate word over two fields its phrase coordinates takes both ("the average distance and
  price"; 41 of 7,000 Spider train questions, 4 DEV); a coordinated filter value is no second field.

Measured (`spider/results/RESULTS.md`, 2026-10-10): engine alone 369 → 371 strict (2 wins, 0 losses); Gemini on, with
the same rewordings, 448 → 452 (4 wins, 0 losses). DEV 911 is now answered and still wrong: it reads the friendship
from the friend's side, which the next change addresses.

## A relation is read from its owner, and rows grouped by a name that repeats are grouped by their key (2026-10-10)

A served-path ledger of DEV's wrong answers after the previous change found one table's questions wrong for one
reason: in `network_1`, Friend(student_id, friend_id) and Likes(student_id, liked_id) both key Highschooler twice,
"high schoolers who have at least 3 friends" tied between the two keys and took friend_id by name order, and grouping
by name merged the two students named Jordan.

- `sql_rank.relation_sides`: a table holding exactly two keys to one other table relates an owner to the rows its name
  names: the key named for the relation ("friend_id" in Friend, "liked_id" in Likes, "friend" in PersonFriend) is the
  other side, the remaining key the owner. `CandidateRanker` reads such a table from its owner's key (feature
  `relation_side`, -1.0 otherwise), or from the other key when the question says its participle ("liked by"). Two
  keys named for neither side (a flight's source and destination airports) relate no owner; `travel_direction` reads
  those.
- `sql_expansion.entity_groups`, now shared by the search, the HAVING expansion (`group_options` and its count
  groups) and the frequency superlatives (which had the rule as `_frequency_groups`): rows grouped by columns that do
  not tell them apart, joined to the rows they count, are grouped by their key too, when that key is unique in its
  table (`SchemaGraph.repeats`, decided once per set of columns, as the search asks it of every grouped candidate). A
  key that repeats belongs to another table ("countrylanguage"."CountryCode" for "languages spoken by only one
  country"), and the search adds the key only beside the name of a table the question says (`sql_search._says_table`,
  "high schoolers" for Highschooler; `sql_schema.is_name_column`, which the ranker now reads instead of its own
  copy), so "by grade" and "total revenue by title" (a job title the employees share; the release build's offline
  regression case caught a first cut that grouped it by employee) stay one group per value.
- `CandidateRanker` scores no group role for a key the answer does not list beside its table's other groups.

Measured (`spider/results/RESULTS.md`, 2026-10-10): engine alone 371 → 384 strict (13 wins, 0 losses); Gemini on,
with the same rewordings, 452 → 466 (14 wins, 0 losses). On 1,000 Spider train questions held out from all of this
work, this change and the two before it take the engine from 215 to 246 strict (33 wins, 2 losses: Spider groups one
question by a name that repeats, and one exclusion now joins through two tables the question does not name). A first
cut lost DEV 758 (the repeated key) and DEV 369 (a listed key that lost its group score); both are regression cases.

## The owner's questions are read as asked: a ranked measure is no grain, a per-row average averages the rows (2026-10-10)

Replaying the owner's own 96 questions from 48 conversations through the production entry point, engine alone, gave
the same answer on `de0cfba` and `eed8d9d` for every one of them: the Spider changes above do not touch the owner's single-table
sheets. The replay found six questions answered wrongly with confidence, which production had answered right before the
SQL-writing model was removed (`12059f1`, 2026-10-02) and the Gemini fallback cannot rescue, since it runs only when
nothing is eligible:

- `query_contract.constraint_violations`: a field an aggregate word takes after "by" ("top 2 cities by total amount")
  is the measure the groups are ranked by, not a grain. The grain check refused every query grouped by city alone and
  served one grouped by city and amount ("Lyon 200, Lyon 160" for Lyon 470, Paris 270). A field after a bare "by"
  ("total amount by city") is still a grain.
- `SQLSearcher._group_choices`: "average amount per order" over a table of orders averages its rows; the search grouped
  by the table's first named column (an average per customer). This holds for AVG, MIN and MAX of the named table's own
  fields; a total per order keeps its groups.
- `ConstraintQueryExpander._count_having_candidates`: "how many orders are over 20" says the number of the orders, as
  "orders over 20" already did, and is no longer customers with more than 20 orders.
- `SQLSearcher._row_values`: a number the question names no numeric field for, after a noun that names a table
  ("orders over 20", "orders are over 20"), compares that table's money measure (`MONEY_MEASURE_COLUMN_WORDS`:
  amount, total, value...), not the unit price the model preferred by a tenth. A first cut preferred any table's money
  measure and turned held-out train 289 ("products whose availability equals to 1") into `amount_payable = 1`; a noun
  of a table without one now prefers nothing. `sql_expansion.COPULAS` is the one list of the forms of "to be" both
  rules skip.
- `sql_expansion.money_total_position`: a number before a money noun counts it. "Customers that have over 2 sales"
  was the sales' total amount.

The chat's Gemini rounds (`engine/llm.py`, `AsyncGeminiClient.generate`) had no timeout. On 2026-10-10, 6 of 362 turns
waited 50-120 s while Vertex's own latency metric for the shared pool showed a median under 2 s and only 200
responses: single requests were held. Each round now waits 20 s and is sent once more after a timeout, a 429 or a 5xx.

Measured (`spider/results/RESULTS.md`, 2026-10-10): Spider DEV 384 strict engine alone and 466 with Gemini's cached
rewordings, held out 246, before and after, with no served query changed. On the owner's 96 questions, 9 answers go
from wrong to right and none get worse; 32 are still refused by the engine alone, 30 of them for "volume" or "monthly
searches" (the "keyword volume" questions asked since 2026-10-06 were answered in production by the Gemini
rewording) and 2 for "the most leads".

## "Clear all conversations" deletes them one at a time (2026-10-10)

`POST /api/conversation/delete-all` answered 500 on every attempt for the owner's account of about 790
conversations: it dropped every conversation schema in one transaction, which held a lock on every table and view
in them until it committed, and Postgres ran out of lock slots (`max_locks_per_transaction` 64 for 50
connections; psycopg2 `OutOfMemory`, "out of shared memory"). The route and `conversations.delete_all_conversations` are removed. The web
app's "Clear all conversations" (`web/public/lib/workbook-conversations.js`) lists the newest chats, shows them, and
deletes them one at a time with each chat's own `POST /api/conversation/delete`; each leaves the list as its deletion
lands, and the first failure stops the run with the rest kept. `trace.delete_traces` deletes one conversation's
traces only; a trace indexed to no conversation expires with the others (`RTDB_TRACE_RETENTION_DAYS`). The admin
dashboard's user deletion and orphan sweep (`engine/admin.py`), which dropped schemas the same way, commit per
schema.

## Starter questions belong to an empty add-in chat, not the web home page (2026-10-10)

The web home page asked `/chat/suggestions` for three starter questions under its composer once signed in. At the
owner's order they appear only in the Google Sheets sidebar and the Excel task pane, while their chat is empty
(`web/public/lib/sidebar-suggestions.js`, loaded by `office/sheets/sidebar.html` and `office/excel/taskpane.html`);
`index.html` no longer loads the module or asks for them.

## "The most" of a one-table workbook's rows counts them per group (2026-10-11)

"Which country has the most leads?" over one sheet named responses was asked what "leads" means, while "how many leads
per country" was answered (an owner's question, 2026-10-01). In a workbook of one table a count counts its rows
whatever the question calls them: `ExpansionSupport.counted_tables` gives the sole table a counted reading, so the
frequency superlative builds `GROUP BY country ORDER BY COUNT(*)`, and the completeness check reads the noun after
"the most" or "the fewest" (`closed_class.counted_rows`, when a noun follows directly; "the most expensive leads"
grades) as the counted rows of a count that orders the groups as well as one that is answered. A field after "the
most" is still ranked by its values, and with two tables an unknown noun is still asked about.

A listed row without a name field keeps its first text field as its display: "list the orders over 20" lists the
orders' customers. Among Spider's "list/show the X" questions over tables without a name field (DEV and train), gold
projects the first text field 104 times and the table's key 76 times, so the display rule stays.

## The review of 2026-10-11: counts of the groups that qualify, an entity's own identity, bound subqueries, every aggregate word, one cutoff reading (2026-10-11)

An outside review (`release-review/spider-review-20261011.md`) reproduced 384 strict on Spider DEV and found five
defects, two of them wrong answers served as complete. Each is fixed in its owner, with the gaps the measurement then
showed:

- F2, `ConstraintQueryExpander.qualifying_count` and `query_contract.constraint_violations`: "how many countries have
  more than 1 car maker" was a COUNT(*) grouped by country, the car makers of each qualifying country; "how many
  customers have more than 2 orders" answered 3, one customer's orders. "How many" answered by groups a HAVING keeps,
  without "each", "per", "by" or "for different" (`sql_expansion.groups_requested`), now counts those groups, each
  by the entity's key where that tells them apart, as a COUNT over the grouped query; a count in each group is a
  violation. Among Spider's DEV and train questions only DEV 161 and 162 and train 6852 have such gold without that
  cue, and all three answer something else than the count asked.
  - A threshold stated on an aggregate a thing has (`ExpansionSupport.aggregate_thresholds`: a holding word such as
    "with" or "whose", the aggregate word, the field, the comparison: "customers with a total amount over 10") is met
    only on that aggregate, not by each row's field; "the total amount of orders over 10" and "total Amount greater
    than 100" still compare each row. `_aggregate_threshold_candidates` builds it over an entity's related table,
    which the search grouped by nothing, and the contract reads an aggregate a HAVING or a FROM subquery computes as
    computed.
  - A number the name of a table follows ("more than 1 car maker") is met only by a count of that table's rows, not by
    its Country key above 1. A number said right after a field whose cells are not all numbers ("cars with horsepower
    more than 150", Spider's text Horsepower) is compared with that field and no other
    (`query_contract._compares_another_field`): once the count per group was refused, the cylinders over 150 answered
    it. "How many different countries" is met by the count of groups.
  - Entities with no related rows ("countries with fewer than 2 car makers", one with none) are counted as "which"
    lists them, through the inner join; Spider's gold does the same for every "fewer than" count.
- F1, `SchemaGraph.identifies` and `identifying_column`: a column referring to a unique key elsewhere was taken to tell
  its own rows apart (with a Majors table, "the major and age of students who do not have a cat" subtracted (major,
  age) and removed a student with another's cat), and missing values were dropped before testing uniqueness. Fields
  tell an entity's rows apart only together and in their own table, a missing value counting as a value
  (`repeats`, memoized per set of columns); the key kept NOT IN holds a value in every row.
- F3, `query_contract._binds`: a comparison inside IN or EXISTS realized the question's wherever it sat, so "people
  whose age is above 35" was met by an uncorrelated EXISTS and by IDs IN a list of ages. It filters the returned rows
  only when the IN compares values of one kind (the same column, a foreign-key pair, a word of their names shared)
  and the EXISTS refers to the outer row; otherwise its scope is `unbound`, like a scalar subquery's.
- F4, `SQLSearcher._aggregate_choices`: a repeated aggregate word collapsed to its first, so "the average distance and
  average price" was refused, with a reply blaming nonnumeric price cells. A repeated word "and" or a list
  coordinates takes the field it stands before, in the question's order; the same word over the same field is asked
  once, and a count asked again in other words ("how many ... the number of") is one count (DEV 89 split it). A
  numeric field the query does not aggregate is "requested aggregate is missing", not a field to repair.
- F5, `sql_extrema.ranking_cutoff`: the search kept one row of "the two people with the largest age" while the
  contract read two. Both read the cutoff here: a number after "top", "bottom", "first" or "last", beside a
  superlative, or before the noun a superlative's phrase ranks (a noun, a joining word, then the superlative: "line 2
  of the address with the most students" names a field, and lost DEV 562 in a first cut).
  - "Excluding Person_ID 5" excludes that row: `nearby_operator` reads "excluding", "except" and "other than" as !=,
    and a key is a number's field where its name introduces the number. No Spider question has either.
  - A ranking whose number names a table shows its rows, not only the field that ranks them: "top 2 products by
    price" listed two prices (`sql_extrema.ranked_table`, in the search's projections and the contract).
  - A field named after its table's name is that table's: "the three accounts with the highest savings balances"
    were ranked by their checking balance once the cutoff was built (held-out train 829).

Measured (`spider/results/RESULTS.md`, 2026-10-11): engine alone 384 → 382 strict (0 wins; the losses are DEV 161 and
162, whose gold counts the car makers in each country); Gemini on, with the same cached rewordings, 466 → 465 (1 win,
the same 2 losses); held out 246 → 249 (3 wins, 0 losses). Not fixed: "the names of items" over items without a name
field lists another table's names (held-out train 789, newly answered); binding "the names of X" to X would refuse
every answer whose names sit in a linked table.

## The second review of 2026-10-11: counts that admit none, bound memberships, cutoffs past 100 (2026-10-11)

The review of the released `3ef82c4` (`release-review/spider-review-20261011.md`, R1-R3) reproduced 382 strict and
found three gaps, each fixed in its owner:

- R1, `ConstraintQueryExpander.without_failing_groups` and `query_contract.realizes_numeric_comparisons`: "countries with
  fewer than 2 car makers" left out the countries with none, because an inner join of the two tables keeps no such
  country; "exactly 0 car makers" compared a car maker's Country key with 0. A count that admits none is now answered
  as the entities NOT IN those failing it ("fewer than 2" is NOT IN those with at least 2), the counted rows' own
  filters inside the NOT IN and the entity's outside. The question contract reads every count threshold a table's
  name follows (`sql_expansion.counted_thresholds`, over `count_thresholds`, moved from `sql_constraints` so both
  read "at most", "between", "or more" alike) and meets it only by a COUNT of that table's rows (or of a table whose
  name extends the noun: jobs held are job_history rows) that admits the same numbers of rows, a NOT IN of the
  others included; one that admits none is not met over an inner join. Two shapes the review's census then showed
  are not met either: a count over joins to tables the question does not name, which repeat the rows counted
  (`_multiplies`: appointments joined to nurses and prescriptions), and groups that include the counted rows' own
  key (`_groups_the_counted_rows`: countries grouped by each city's id). The HAVING expansion now prefers the join
  tree of the tables it counts over a candidate's longer joins that carry no filter. "At most 1" no longer reads as
  "the most" (`sql_extrema.valid_superlative_cue` in the frequency cue and the base search's ordering).
- R2, `query_contract._binds`: a membership test binds the outer row only when the subquery returns the operand's own
  column, one a foreign key pairs with it, one of the same name, or one named for its table and itself
  ("visitor_ID" for "visitor"."ID"); a shared word ("Manager_ID" for "Person_ID"), a computed value ("Age - 39") or
  an unknown form binds nothing, and a set of queries binds when each branch does.
- R3, `sql_extrema.ranking_cutoff`: the cutoff reader capped counts at 100, so "top 101" was refused. A number after
  "top" or "bottom" is the cutoff whatever its size; beside a superlative it must stand as its count ("the 3 oldest",
  "the oldest 3", `_beside`), not a field's value ("room 111 most recently" named a room once the cap was gone).

The release build of `3ef82c4` failed `tests.test_release` because RESULTS.md linked into `release-review/`, which
the release context omits; the link test now treats such a link as broken locally too.

## The review of 6263502: one count per population, read as a formula; a "number" that names a field (2026-10-11)

The independent review of `6263502` (`release-review/spider-review-20261011.md`, S1-S6) reproduced 386 strict and
found the count check still read a HAVING as the comparisons of its conjunction, each COUNT taken for the rows asked.
`query_contract.realizes_numeric_comparisons` now reads the HAVING as a formula of the counts that count the asked
rows (`count_formula`: AND and OR of their comparisons; an OR joining one to anything else says nothing of it), and
tries a count only at the boundaries its bounds make (`boundary_counts`), so the work no longer grows with the number
said (S5: two seconds for a million). A count is of the asked rows only as COUNT(*), or of a column of theirs, a
distinct one telling them apart (S2: a count of distinct countries is no count of car makers); `counted_thresholds`
returns each threshold the question states, two alike being two (S3: "more than 1 car maker and more than 1
airport"), each needing its own count, and COUNT(*) counts neither of two populations it joins. A count admitting none
is met by the counted table grouped alone only where no other table holds what it groups by (S1: the car makers
grouped alone list the countries with one), else by the NOT IN of the groups failing it, whose OR the formula now
reads. A table whose name extends the counted noun stands for it only where the query does not read the counted
table: beside the templates, Ref_Template_Types holds the types they are grouped by, and the NOT IN answering
"template types with fewer than 3 templates" was refused as grouping templates by their own key (Spider DEV 333/334,
found by this batch's DEV run). A largest or smallest value in a membership test binds the rows it keeps only where the question asks for an
extremum or each group is one row (S4, `_binds`). `sql_extrema.ranking_cutoff` reads a number a year could be as a
count right before the superlative ("the 1200 oldest", S6), and as the year after it or before a noun.

Two families the held-out sample showed in the same batch: a count grouped by the key of the one table it reads, which
other tables' foreign keys refer to, counts one row in each group ("the transaction type code that occurs the most
frequently" tied every type at 1, held-out train 370; a contract violation in `_select_violations`); and any
"number" asked for a count, so "the name and phone number of these patients" showed each patient's count (held-out
train 47). `sql_expansion.field_numbers` reads a "number" that ends a field's name ("phone number", "flight
numbers", "the staff number") as the field; `count_requested` takes the field names and leaves those out, and the
search's aggregate cues and the ranker's question roles read the same positions, replacing their two separate
readings (encoder mentions before the number, and any word of any field's name).

## Count requirements are built as the check reads them; where correct plans are lost (2026-10-12)

The check reads each count threshold the question states on its own (`counted_thresholds`), but construction built one
threshold per candidate, so "countries with more than 1 car maker and more than 1 airport" was refused once the check
stopped accepting one grouped join of both tables. `ConstraintQueryExpander._requirement_candidates` now builds, for an
entity table every counted table refers to by a foreign key, one membership test per requirement: the entity's key
IN the counted table's groups meeting it, or NOT IN the groups failing it when it admits none (`_meeting_counts`,
`_failing_counts`, shared with `without_failing_groups` and the grouped count candidates). Each count reads only its own
table, so no join repeats another's rows. No Spider DEV or held-out question states two count thresholds, so the
benchmarks cannot move; the change closes the serving half of the review's S3.

The review's plan (`release-review/sql-accuracy.md`, step 4) asked whether constructor-consumed correct semantic
bindings would make correct plans appear. The evaluation-only gold-column-role reweighting run
(`spider/results/RESULTS.md`, 2026-10-12) moved the raw choice before completeness/finalization 535 → 544 of 1,034
and retained raw pool recall 672 → 673. It changed broad column-role sets, not operation/entity/population/scope
bindings, and base construction does not consume those role scores. It diagnoses the current interface's limited
leverage, not an upper bound on semantic readings or completion of the proposed correct-binding experiment. The
362 retained-pool misses need classification into binding/consumption, missing construction, and pruning; the 227
raw-correct blocked cases need validated serving realizations and witnessed admission. Named-binding improvements
must reach construction to be evaluated fairly.

## Count requirements preserve their explicit Boolean connection (2026-10-12)

Independent review of `f6fbf4d` reproduced an OR request serving the new AND membership plan: countries with more
than one maker OR more than one airport returned Chile, omitting USA and Japan. Both counts were independently
witnessed, so completeness accepted the wrong intersection. `sql_expansion.count_requirement_operator` now reads
the connective between threshold occurrences, excluding each threshold's internal range or "two or more" wording.
The constraint constructor uses that same reading for its typed AND/OR expression. Mixed or unbound coordination
remains unresolved, rather than silently becoming AND.

`query_contract.realizes_numeric_comparisons` additionally checks the Boolean connection between distinct counted
populations. It derives a test from witnessed typed membership/existence predicates and supported set/derived-table
structure, rejects unrelated OR alternatives, and compares the requested and actual Boolean relationship over the
bounded retained requirements (currently at most four). Separate valid count witnesses cannot substitute for this
joint check. This closes the reproduced AND/OR swap; it does not claim a complete natural-language scope parser.

Serving list/scalar OR controls and zero-admitting OR contrasts, plus direct wrong-AND/wrong-OR completeness tests,
failed before the repair and pass afterward. No model, SQL-token generator, alternate planner, or bundle was added.

## Answers shown as asked, words a query realizes read, aggregates over every field they coordinate (2026-10-12)

The plan of `release-review/sql-accuracy.md` measured where correct plans are lost at `3563863` (`spider/results/RESULTS.md`,
"Where correct plans are lost"): 386 served right, 227 refused or out-ranked while a strict-correct candidate was in the
pool, 362 with none in it. This change works through the families those buckets hold, each measured on a census of
every pool member (evaluation-only `census.py`, replayed through the served selection) before the full evaluation.

Recovering correct plans the gate refused or the ranker passed over:

- A word the query realizes though nothing of it spells the word is read (`query_contract._witnessed_words`): a
  travel direction by the column of that direction, a name's part by the field abbreviating it (Fname), "name" by a
  field named as its table is, a verb relating two tables the query reads, a word sharing a stem of five letters or
  more with a table or field it reads ("enrolled" by Student_Enrolment, "independent" by IndepYear; a word of an
  operation excepted), and "one" after a determiner. Two words that begin a read table's name are read ("the high
  school student" by Highschooler), extending the two-word reading already in `read_question`.
- A grouped answer that names a group as a field lists its fields in the question's order
  (`sql_search._in_question_order`); a joined listing whose join repeats its rows gets a DISTINCT twin, preferred by
  the ranker (`sql_rank.repeats_shown_rows`); a field the question names only to filter on is not shown
  (`sql_rank.filter_only`).
- Groups list each value once, so "different" is read by a grouped answer with counts beside its groups; a count of a
  group's own distinct values (1 in every group) is refused, a key its joins equate with the group's included.

Construction and bindings:

- One aggregate word takes every field it coordinates after "of" when the words before "of" are both fields' ("the
  average age of losers and winners"), and a count takes the fields it coordinates ("the number of different series
  names and contents").
- A "number" that begins a field's name with the rest of the name after it asks for the field (Num_of_Ticket,
  Number_products); "Num" reads "number" as "No" did, and a lone "number" names such a field only beside its
  entity's words ("the total number of players" is no match_num).

Refusing wrong answers: a listing answers the rows the question's opening names, after a command it opens with
(`query_contract._asked_tables`): airport names no longer answer "what are airlines that have flights from AHD",
which matched Spider's empty gold by coincidence.

Measured and not adopted (each simulated on the census, or built and censused):

- Admitting, when no pool member can pass the gate, the best-ranked readings beyond the pool that can: 2 wins (DEV 722,
  791), 8 newly wrong. A refusal gets the Gemini rewording in production; a wrong answer does not.
- A field named by its last word alone when no other field has it ("area" of SurfaceArea): no win, one newly wrong.
- An aggregate word's own field after the fields it names ("the average age" past stadium.Average): the freed field
  became a shown or grouped field.
- Synonyms by spaCy word vectors: 7 wins and 14 newly wrong at cosine 0.7. Edit-distance typos: 6 wins and 3 newly
  wrong, firing on real words ("voted", "owned"). Inflections of a schema word: 5 right, 9 wrong.
- "One word, one field" demotion of answers showing more name fields than the question says "name": +3 at most.
- Refusing an answer that leaves out a field the projection window names: 92 right answers refused.

Measured on Spider (`spider/results/RESULTS.md`, same date): DEV 386 → 461 strict (80 wins, 5 losses, three of them
answers that matched an empty gold by coincidence), Gemini on 471 → 529, held out 255 → 278 (28 wins, 5 losses), and a
fresh holdout no change was chosen on 286 → 306 (23 wins, 3 losses). The held-out gain answers more wrongly too (wrong
answers 232 → 261): a word the gate now reads lets a query through that is wrong elsewhere, so the gate's other checks
(a count without its group, a field shown but not asked) are the next work, not looser reading.

## Counts over exclusions and averages, numbers beside fields, fields shown only where asked (2026-10-12)

The census of every pool member after the previous change (evaluation-only, replayed through the served selection)
still held correct plans the search never built for counts, comparisons and listings the question states plainly, and
answers that showed a field the question names only to order or own other fields. Each family below was built with a
positive, a same-shape contrast and a negative case (`tests/test_sql_ast.py`), then censused on DEV and the held-out
sample before the full evaluations.

Construction:

- A count over an exclusion counts the rows kept: "how many dogs have not gone through any treatment" and "the number
  of professionals who have not treated any dogs" (`sql_recursive`: "have not", "has not" and "did not" negate as "do
  not" does, and the exclusion keeps its COUNT(*)).
- A count beside an average comparison counts the rows on its side, by the groups the table's own field makes: "how many
  dogs have an age below the average", "the number of cities in each district whose population is greater than the
  average" (`sql_expansion.average_comparison`, `sql_recursive`); the completeness check reads the compared average as
  computed.
- A number said beside a numeric field's name is that field's value: "high schoolers in grade 10", "the cars with 8
  cylinders", "a cat that is age 3" (`sql_search._numeric_predicate_groups`).
- "In the order of birth date" orders as "ordered by birth date" does (`sql_expansion.ordering_requested`).
- A name's part a field abbreviates links "the first name" to Fname and "last name" to LName in the search as well as
  the completeness check (`sql_expansion.name_part_words`).

Output requirements: a field the question names only in the phrase that orders the rows ("the first name of students
in the descending order of age"), or a field named as its table where the question says that name only after "of", as
the owner of the fields it asks for ("the carriers of devices"), is not shown (`CandidateRanker._order_only`,
`_owner_only`, −8 each, as `sql_rank.filter_only`).

Correctness: a filter moves into a HAVING ("HAVING AVG(Cylinders) = 4" for "the average weight of the cars with 4
cylinders") only where the operand's name follows its aggregate word within three words
(`sql_constraints._explicit_aggregate_constraint`), and a count grouped by a key that itself refers to another
table's rows counts the rows where it is ("the number of likes for each student id", `query_contract`).

Measured on Spider (`spider/results/RESULTS.md`, same date): DEV 461 → 471 strict (10 wins, no loss, wrong answers
unchanged at 216), held out 278 → 304 (26 wins, no loss, wrong answers 261 → 257), Gemini on 529 → 538 (9 wins, no loss).

## Full names, fields asked by their owner, counting words, conditions kept out of the answer (2026-10-12)

The census after the previous change (evaluation-only, every pool member replayed through the served selection) held
right answers refused for a field the gate did not see asked or read, and answers that showed a field or an aggregate
the question names only as a condition. Each family was simulated on the DEV and held-out censuses first, then built
with a positive, a same-shape contrast and a negative case (`tests/test_sql_ast.py`).

Readings the completeness check was missing (`query_contract`):

- "The full name" is a first and a last name of one table, unless a field is named for it (car_makers.FullName): the
  search reads it so (`SQLSearcher._full_names`, for the mentions and the fields listed), and the check reads "full"
  by a query showing both, whatever cell holds the word "full".
- A word the search names a read field by is read (`sql_expansion.column_matches`: "gender" by Sex), but not a word a
  read table or field spells, which stays a name the clarification of an unread word offers fields beside.
- A requested field is used where a longer field's name says it and the question says that longer name together, in
  order ("first name and last name" beside a Dogs.name), or apart where the longer field's table is the one the
  question names as the owner after "of" ("the name of the breed", "the names and ids of all countries") or the rows
  it asks for; a camel-cased name split further needs one of those ("at least 2 models ... the id" is no ModelId). A
  field named as its table answers "the names of orchestras". An aliased self-join's field is its table's.
- A word saying how rows are counted is read by a query counting rows: "times" and "records" after a word of a count,
  and a verb a count follows after "by" ("shared by at least two teachers") by one that groups the rows it counts.
- A question opening "how many" or "count" is answered by a computed aggregate: "how many friends does Kyle have"
  listed the friends.

Output requirements (`sql_rank.CandidateRanker`, −8 each, as the existing `sql_rank.filter_only`):

- A field the question compares before naming it ("projects that require between 100 and 300 hours", "who do not use
  Cash as payment method") or picks the row by after "with the highest" ("the account with the highest checking
  balance") is not shown; "which 2 products have the highest price" still shows the prices (review F5).
- An aggregate said only where the groups are kept ("the studios whose average gross is above 4500000", "with count
  more than 1") is not shown, and the search builds the reading without it (`sql_search._condition_twin`); one the
  question also asks for is shown.
- "The first name and age" lists Fname first: a name's part a field abbreviates is placed at its two words.

The labelled Gemini rewording (`engine/question_rewrite.py`, `engine/sql_prompt.py`), which answers a question only
when the search finds no runnable query for it: of 357 rewordings on DEV, 98 were refused as changing a stated value
or number (recovered from the rewrite cache, evaluation-only `rejected_rewrites.py`):

- 48 because a comparison came back as a symbol ("Horsepower > 150", "Year < 1980"), so the check that the rewording
  keeps the question's operations found no "more than" or "before"; 129 cached rewordings read like SQL. Gemini is
  now asked for a plain English question, each comparison of size or order in words and each column without its
  table. A first version also asked for "equal to" in place of "=": 42 of 55 rewordings refused for an unread word
  then said it, while "PetType = 'dog'" had always been read, so equality is left as Gemini writes it.
- 34 because every cell value the question says as a word had to survive the rewording, and country codes are words:
  'IN' refused "Which region is the city Kabul located in?" and 'ARE' "countries that are republics". The check now
  holds only names the search cannot read (東京); the values it reads are held by the check before it, as they were.

Measured and not adopted: reading a word the data holds only in free-text cells (upper bound +0 DEV, +3/−1 held out);
an ORDER BY ... LIMIT 1 as the maximum asked (no win); a "listed" witness (an "are listed" rule, guarded against
status fields, already exists).

Measured on Spider (`spider/results/RESULTS.md`, same date): DEV 471 → 493 strict (22 wins, no loss, wrong answers
216 → 208), held out 304 → 324 (20 wins, no loss), the fresh holdout against what production served 306 → 329 (24
wins, 1 loss, wrong answers 255 → 258), Gemini on 538 → 576 (51 wins, 13 losses, all but one a refusal of a reworded question). Of 29 newly answered fresh-holdout questions 9 are right:
a word or field the gate now reads lets through a query wrong elsewhere (a count without its group, the direction of a
prerequisite), so the precision of new answers stays the measure to watch.

## A number of a numeric field, all the information, "equal to" and "record" read (2026-10-12)

Questions the census found with no reading built, or refused for one word, in families each checked against gold
across DEV, the held-out sample and the fresh holdout before it was built:

- A "number of" before a numeric field's whole name is that field (`sql_expansion.field_numbers`, which the search,
  the ranker's question roles and the completeness check all read, now from the fields' types): "the average,
  maximum, and minimum number of floors" of Floors, "in ascending order of number of police officers" of
  Police_officers. Gold reads the field in 9 of 9 DEV questions with such a phrase, 19 of 20 held out and 8 of 10 in
  the fresh holdout; a text field's name ("the number of countries" beside Country, 28 questions) and a table's stay
  counts.
- "All the information about hiring", "all the details of employees", "all columns of the albums" ask for every field
  (`sql_expansion.complete_projection_requested`; every one of 17 such questions has SELECT * as gold), and the
  completeness check reads those words by a query showing every field; a superlative states the cutoff such an answer
  may keep ("all information on the airport that has the largest number of international passengers").
- "Equal to" is read by a query comparing for equality ("whose Country is equal to 'France'").
- "Record" after the name of a table or field the query reads names its rows ("from the trip record").
- The owner a question names after "of" decides which longer field answers a requested name; the rows its opening asks
  for decide only where it names none: "the names of the dogs of the rarest breed" is no breed_name.
- A number right after "top", "bottom", "first" or "last" is the cutoff a ranking keeps, not a value of the field beside
  it: "the mountains with the top 3 prominence" compared Prominence with 3 (the fresh holdout's one loss in the change
  before).
- "The full name" lists the first name before the last wherever else the question says a part ("the ids and full names
  ... with a first name that contains the letter T" listed the last name first).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-13): DEV 493 → 497 strict, held out 324 → 335, the fresh
holdout 329 → 334, with no loss on any of them, and wrong answers down on each (208 → 207, 257 → 253, 258 → 257);
Gemini on 576 → 592 (16 wins, no loss; wrong answers 263 → 271, the rewordings "equal to" now lets through). Of 14 newly answered questions across the three, 12 are strict-correct.

## Rewordings keep the operations the engine reads; aggregate words coordinated before a field share it (2026-10-13)

- A labelled rewording (`engine/question_rewrite.py`) must keep the computations the question asks, and a count is now
  the engine's own reading of one (`sql_expansion.count_requested`): "the number of products" beside a field
  Number_products asks for that field, and "the phone number" for a phone. Words of grouping ("per", "by", "group") are
  no longer required of a rewording: the answer check after the reworded search holds the grain the original question
  asks ("requested output grain is missing"), and "miles per gallon", "song names by singers" and "produced by the
  maker" group nothing. "No less than" reworded "at least" keeps its comparison. Spider DEV, 2026-10-13: 30 rewordings
  were refused for losing one of those words.
- Aggregate words coordinated before a field share that field (`sql_rank._heads_phrase`): "the average, minimum, and
  maximum age" averages Age. A field named by the aggregate word itself (a stadium's Average) is not what the word asks
  when its phrase names another field (`sql_rank.analyze_question` aggregate targets): "the average and maximum
  capacities" averages Capacity, and "the highest average attendance" still names the Average field.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-13): DEV 497 → 499 strict, held out 335 and the fresh holdout
334 unchanged, Gemini on 592 → 601 (9 wins, no loss), with no loss on any of them.

## Fields named by another form of their words and by their table's abbreviation; a sort verb opening a question (2026-10-13)

The pool census after the review found the projection the largest construction gap: on the held-out sample 182 of the
433 questions with no strict-correct reading never built the fields gold shows, 117 of them because no reading named a
field the question asks for. The question says such a field in another form of its name's words, or by the words its
table's abbreviation stands for.

- The stem reading the completeness check read words by (a shared stem of five letters or more, no operation's word)
  is one shared reading, `sql_expansion.says_in_another_form` (moved from `query_contract`), and the field linker names
  fields by it (`sql_search._column_link_positions`): "the arriving date and the departing date" date_arrived and
  date_departed, "years of founding" Year_of_Founded, "their directors" Directed_by. A field said so is placed at its
  own word, or the word of its kind after it, so two such fields are two fields of the answer, and the question's order
  reads it there. A word that names a field itself names no other in another form ("the gradepoints" is no Grade); a
  bare "d" ends only a past form ("amenid" says no "amenity"); a past participle says a condition, not a field to show
  ("the buildings managed by 'Brenden'" lists no building_manager).
- A field's first word abbreviating its own table's name is that table's word (`sql_search._abbreviated_table`), as
  "customer" of customer_name is: cust_name, stu_gpa, crs_description, dept_name, apt_number. Such a field, and one its
  table's word names but for its kind and a preposition (date_of_treatment), is named beside its table: "their
  apartment numbers", "the name of the different customers", "the date of treatment"; not the "phone number" of a
  building, "any class" of a student, "the number of apartments" or the "departing date of all the dogs".
- A sort verb opening the question is its command, and the ordering opens at the "by" after it: "Sort employee names by
  their age" lists the names.
- "Number", "all", "any", "each" and "every" in lower case are no cell value: "the number of tracks in each genre" kept
  the track named 'Numbers', "all airports" the IATA code 'ALL'.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-13): DEV 499 → 505 strict, held out 335 → 348, the fresh
holdout 334 → 345, Gemini on 601 → 604, with no loss on any of the three engine-alone sets.

## A nation is a country, a store a shop; one-letter codes and a participle before a year read; a noun fields share counts its entity (2026-10-13)

Families of questions the census found refused for one word, or with no reading built, each read the same way by the
search and by the completeness check:

- `sql_schema.canon` reads "nation" as "country" and "store" as "shop", so every reader of questions, names and values
  agrees: "the names of the nations with the 3 lowest populations", "the minimum and maximum number of products of all
  stores". A "nationality" stays its own word.
- A field of one-letter codes holds the words they stand for (`SchemaGraph._build_value_index`: M/F male and female,
  L/R left and right, Y/N yes and no, beside an unknown U, X or O), and a query comparing the code reads the word:
  "how many female dependents" compares Sex 'F', "left handed players" hand 'L'. Sizes S/M/L hold no "male".
- A past participle before a year the query compares says how that year relates to the rows: "cars produced in 1980".
- A noun several fields share names their entity, and a count of its different values counts the entity's name or key
  first (`SQLSearcher._distinct_counted_columns`): "how many different winners ... were left handed" counts winner_name
  or winner_id, not the winner_hand it compares with 'L' (the first four fields in alphabetical order were offered).
- The search's reading of a code word before a field's name ("left handed", `sql_search._CATEGORICAL_INITIALS`) takes
  its letters from the same table (`sql_schema.CODE_LETTERS`).
- Measured and not kept: "when" asking for a date field of the tables the question names won one held-out question
  and answered three DEV questions wrongly.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-13): DEV 505 → 524 strict, held out 348 → 353, the fresh
holdout 345 → 346, Gemini on 604 → 611, with no loss engine alone and one with Gemini on (639, a field the answer
leaves out: a family of its own).

## "In total" closing a count restates it; a field inside another field's spelled name is not named there (2026-10-13)

Two construction families the pool census of the package 9 code found, each a stray part of an otherwise built answer:

- "In total" after "how many" restates the count (`SQLSearcher._aggregate_choices`): "How many tasks are there in
  total?" counts the tasks and sums nothing. Each such question had added a SUM of a field it never names (a grant's
  amount, a student's advisor, a county's population). A total the question names ("the total grant amount") is still a
  sum.
- A field whose words another field of its table spells whole is not named inside that spelling
  (`sql_search._column_link_positions`, `_longer_names`): "the ranking date" names ranking_date and not the ranking too,
  "each bed type" bedType and not beds. Said on its own, the field is still named.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-13): DEV 524 → 525 strict, held out 353 → 354, the fresh
holdout 346 → 351, Gemini on 611 → 612, with no loss on any set and fewer wrong answers on each.

## A field beside the named field of its entity is not asked (2026-10-13)

The pool census of the package 9 code found 67 DEV questions answered wrongly with a strict-correct reading in the
pool; the largest share differed only in the fields shown, most often an entity's other field shown and grouped beside
the field the question names: "all template ids and the number of documents using each template" showed the template's
type code too, "all document ids and the number of paragraphs in each document" the document's name.

The ranker credits a field as the group a question asks when its table, or a table its key links, is the entity the
question groups by (`CandidateRanker._group_alignment`). It no longer does for a field the question does not ask to show
when the answer shows a field the question does ask of that same entity (`CandidateRanker._named_beside`, over the
tables a field's key pairs link, `_entity_tables`): that field is how the question asks for the entity. A key the
question asks by "id" stays asked ("the full name of each car maker, along with its id").

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-13): DEV 525 → 527 strict, held out 354 → 356, the fresh
holdout 351 → 353, Gemini on 612 → 614, with no loss on any set and fewer wrong answers on each.

## Standard code words require field-kind and AST-bound evidence (2026-10-13)

The October 13 independent review reproduced two wrong-answer paths in the new code aliases. A players table with
only Size 'M' answered "How many male players?" as the number of medium-size entries; Stage F/M did the same. With
Sex and Stage both present, completeness also read "male" from a Stage='M' predicate merely because SQL held the same
literal as the Sex code. The production selector accepted that wrong candidate and returned 2 instead of 1.

- `SchemaGraph._build_value_index` and the existing qualified-code constructor share
  `sql_schema.code_word_matches_column`: M/F needs a sex/gender field, L/R a hand/side/direction/orientation field.
  A single-code capped profile is sufficient only after that field evidence, never by itself. Unknown labels require
  an explicit literal or separately established meaning; the existing Y/N and explicit boolean readings are unchanged.
- `query_contract.read_question` reads aliases from typed comparisons on the bound field, through existing predicate
  extraction. It preserves base-table aliases and LOWER and distinguishes a bare different-case literal. Projection
  literals and codes compared on another field do not prove the reading.
- The registered regression exercises ambiguous Size/Stage/Grade profiles, raw literal codes on those same profiles,
  supported Sex/Gender and handedness, a confounding field beside Sex, alias resolution, and literal case.

This is a bounded correctness repair in the existing owners, not a complete population/scope proof. Fresh Spider
accuracy and paired changes are recorded in `spider/results/RESULTS.md`; fixture hardening is not itself an accuracy gain.

## A field is asked by its name without its kind (2026-10-13)

The completeness check named a field the question asks for only by its whole name, while the search names it by the
words of its name without their kind too (`sql_expansion.naming_words`): "the professional's id, role and email" passed
with the ids alone, role_code and email_address unread as requests. The check now reads both
(`query_contract._select_violations`), so a requested field the answer leaves out refuses it, and the reading that shows
it is served. The question's command is no such word: "Return the most common first name" asks for no return_date
(`closed_class.action_words`; held-out Spider train 156 lost its answer to that reading before the guard).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-13): DEV 527 → 529 strict, held out 356 → 357, the fresh
holdout 353 → 354, Gemini on 614 → 616, with no loss on any set and fewer wrong answers on each.

## A count counts the rows of the table its noun names; a field abbreviated by its table's name is shown; a word said once names one field (2026-10-13)

Three ranking and projection families of the census's wrong answers with a strict-correct reading in the pool:

- The noun right after the count word names the table whose rows are counted (`sql_rank.analyze_question`
  counted_heads): "How many countries are listed?" counts the countries, and a count of another table's field (the car
  makers' Country) is not that count. A table named further on is no counted noun: "How many sections does course
  ACCT-211 have?" still counts the class sections.
- The reading of the fields to show (`sql_expansion.column_matches`) reads a first word abbreviating the table's name as
  that table's word, where the question says the table, as the search's linker does: "the id of students" shows StuID,
  not the students' last names. The abbreviation reading moved from `sql_search` to `sql_expansion.abbreviated_table`, its
  one owner; "the first names of all professors" names no DEPT_NAME of a department it never says.
- Of several shown fields whose names end in a word the question says fewer times, the one whose other words and
  table the question never says is not asked (`CandidateRanker`, unsaid_owner): "the name and the release year of the
  song by the youngest singer" is Song_Name, not also a concert's concert_Name.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-13): DEV 529 → 535 strict, held out 357 → 358, the fresh
holdout 354 → 355, Gemini on 616 → 622, with no loss on any set and fewer wrong answers on each.

## An explicit list names the outputs, their owner and order; a row count drops joins that only multiply it (2026-10-14)

The October 14 review asked for shared output bindings (each requested field's owner, occurrence and output position,
kept through construction and checking) and count-population bindings (count the counted entity, not joined rows).
This is their first slice, built on the existing owners:

- `sql_expansion.output_requests` reads a sentence after the first that opens with a list verb ("List the continent
  id, continent name and the number of countries."): its items in order, an item a count when it begins with one ("the
  number of countries", "how many") whose "number" names no field (`field_numbers`: "the phone number" is Phone).
  `request_fields` realizes each field item by the fields whose own name says it before those that need their table's
  word or the display field's "name"; the items are one owner's unless an item names another, and a foreign key gives
  way to the key it references ("the club id" of a count of coaches per club is the club's Club_ID). `request_slots`
  is the order.
- That one reading is consumed by construction (the search's requested projection and its group-by,
  `SQLExpansion.requested_fields` first among a table's projection options), ranking
  (`CandidateRanker._output_request_features`: a requested field or count left out, or a field no item asks for, -4;
  a requested field counts as asked and as the asked group), the answer's order (`sql_search._in_list_order`) and the
  completeness check (`query_contract._select_violations` reads a label a realized item covers as read).
- `sql_expansion.counted_noun_tables` (the table the noun after "how many", "number of" or "count of" names) is the
  one reading of the counted population for the ranker (counted_heads) and the search: `SQLSearcher._simplified`
  drops from a row count a leaf join to a table nothing reads, nothing counts and the question does not name, which
  only multiplies the counted rows ("How many countries does each continent have?" counts no car makers). A table the
  question names keeps its join: "How many dogs went through any treatments?" counts dogs that have one.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-14): DEV 535 → 539 strict, held out 358 → 359, the fresh
holdout 355 → 358, Gemini on 622 → 626, with no loss on any set. These remain bounded repairs: a count over a join that
multiplies a NAMED table's rows is still COUNT(*), not a distinct count of the counted entity (DEV 966), and a count
threshold's noun ("more than 3 car models") is not yet a counted-population reading (DEV 149).

## The verb a sentence opens with names no table; "the count" in a list; a relationship self-join needs its word; a superlative over the shown field is one value (2026-10-14)

Four ranking and construction families from the certified ledger's wrong answers that had an admissible, independently
certified plan in the pool (DEV, 2026-10-14):

- `sql_expansion.command_positions`: the verb a sentence opens with, after "please", which the tagger reads as a verb,
  is its command and names no table. The search's table linking leaves it out, and the completeness check's asked
  tables skip a leading "please" before the opening command: "Show the names of conductors and the orchestras they
  have conducted" joins no show table of the orchestra database.
- `sql_expansion.output_requests`: a list item that is only "count" asks for the count ("List the continent name and
  the count.").
- `sql_recursive`: a relationship self-join is built only where a word of one of its keys besides the parent table's
  name is said, whole or abbreviated ("Kyle's friends" by friend_id, "the course whose prerequisite is ..." by
  prereq_id); "the airport with code 'AKO'" names no flight between airports.
- `CandidateRanker` prefers a top-1 superlative ordered by the one field it shows ("the age of the oldest dog" by age,
  not birth date), and `sql_ast.keep_ties` serves one row when every shown expression is one the rows are ordered by:
  the tied rows show the same values.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-14): DEV 539 → 544 strict, held out 359 unchanged, the fresh
holdout 358 → 359, Gemini on 626 → 632. The one loss, DEV 202 ("the names of airports in Aberdeen"), is an
empty-result coincidence: Spider's gold compares City = 'Aberdeen' with the stored 'Aberdeen ' and returns no rows, which
the old self-join reading also returned; the served answer now names the airport.


## Repeated explicit field requests retain their output occurrences (2026-10-14)

The production selector served only id and name for "Find all people. List the id, name and id." while claiming
complete coverage. The shared output reader retained all three requests, but `ExpansionSupport.requested_fields` and
`SQLSearcher._requested_projection` deduplicated their fields; `_in_list_order` could order the surviving outputs
without realizing every repeated field.

The existing constructors now preserve each field occurrence. List ordering may repeat a ColumnRef already bound to
the requested slot; it never creates another aggregate from an anonymous count slot. The completeness owner counts
actual output occurrences for repeated singleton field bindings and resolves table aliases. This is bounded field
occurrence hardening, not a complete entity-role, count-population or scope proof.

Registered production-selector controls cover repeated id, repeated name, a singleton repeated twice, the same-schema
ordinary list, aliased incomplete/complete ASTs, and a count that cannot stand for two anonymous count requests. The
regression fails on the frozen pre-repair selector. Repeated typed outputs also pass actual SQL/Python execution
parity, preserving three result positions. Fresh DEV measurements and the distinction between fixture hardening and
measured accuracy gains belong in `spider/results/RESULTS.md`. No SQL/AST generator or model change was introduced.


## A scalar named population counts its qualified rows once (2026-10-14)

The registered dogs/treatments fixture retained its treatment join but counted three treatment rows for two dogs.
Join presence was insufficient: the regression now checks the value, and adds zero/one/many children, repeated
display names, a NULL child foreign key, the reversed population and ordinary no-join counts.

`sql_expansion.scalar_counted_table` binds one population for a simple top-level COUNT(*), excluding grouped, ranked,
aliased, derived and distinct-request readings. The existing search retains the qualification joins but counts a
proven unique, non-NULL identifying column of that population distinctly (`SchemaGraph.identifying_column`).
`count_preserves_population` proves a plain COUNT(*) remains at its named root grain when each joined table matches
at most one row by its joined key. Construction and checking share that proof; qualifying joins need not disappear
or use DISTINCT when they cannot multiply rows. The completeness owner rejects the scalar raw row-count reading
when it counts a different root or a potentially multiplying joined grain.
Identity and cardinality are proved over the current request's table values; they do not establish uniqueness for
unseen future rows. Request data changes rebuild the graph and its proofs.
This is a bounded row-count policy; nullable field counts, composite identities, grouped/threshold counts, operation
occurrences and relation-role scopes still need their richer bindings.

The existing neutral plan represents distinct counting through `AggregateValue.distinct`. Both current emitters
consume it: SQL COUNT(DISTINCT operand), and Python's named `DistinctCountState`, `COUNT_DISTINCT` and finalizer.
Each state owns its set, excludes NULL and initializes empty counts at zero. The one AST lowerer accepts scalar
single-column COUNT(DISTINCT column); unsupported grouped/ranked/sharing AST forms retain explicit rejection.
Actual SQL/Python execution verifies the joined identity count and NULL/empty/group-state controls. No second
planner, lowering route, runtime bundle, SQL/AST generator, training or promotion was introduced. Measurements
belong in `spider/results/RESULTS.md`; fixture repairs are not presumed DEV gains.

## The opening sentence lists its coordinated outputs (2026-10-14)

A correct-binding experiment (offline, evaluation-only; no training) supplied each DEV question's gold select list
through the existing output reading (`sql_expansion.output_requests`, `request_fields`, `request_slots`) and changed
nothing else: served strict 544 → 592 (51 wins, 3 losses), answered 728 → 745, and 82 more questions with a correct
plan in the retained pool; the counted-population binding added nothing. The typed construction builds those plans; the
reading of which fields to show was the deficit. This change reads more of it with the same interface:

- The opening sentence lists its outputs when it asks for two or more, coordinated, by a command or "what is/are" ("What
  are the names, themes, and number of singers for every concert?"). Its list ends where the rows it reads begin ("of
  countries"); "each"/"every" open an item without naming it.
- `request_fields` decides the owner over all items before an item prefers the field its words name exactly: "the name
  and theme for all concerts" is the concert's concert_Name, "the names and areas of countries" Name, not LocalName.
  An item's candidates are the fields whose own name says it, or, where none does, those its table's word or display
  field realizes. A foreign key gives way to the key it references only in deciding the owner; within the owner its
  own key comes before a foreign key ("the id" of accounts is account_id), and a foreign key and the key it references,
  holding the same values, both realize an item ("the grape" of wines). Where tables tie, the table named right after
  the items ("of all the sections"), else one named before the list ("Which head's name ...? List the id and name."),
  owns them; otherwise the items read no list ("the first name and last name for all the instructors"). In the opening
  sentence "of" ends the list only where no item follows it ("the date of account opened, the account name, ...").

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-14): DEV 544 → 555 strict, held out
359 → 361, the fresh holdout 359 → 365, Gemini on 632 → 645.
Two losses remain: held-out 113 ("the id, the date of account opened, ...", read right, lost to a simplified plan that
keeps a fuller draft's score) and Gemini DEV 417 (the answer follows the question's order, the opening year first;
Spider's gold lists the staff number first).

## A list verb after a comma lists the outputs; a field compared only is not the answer (2026-10-15)

The correct-binding experiment rerun on the package above (gold select lists through the same reading, everything else
production) still served 37 more DEV answers (555 → 592). Of its 40 wins, nine are grouped answers Spider lists count
first, a recorded product choice (`sql_search._in_question_order`); this change reads four families of the rest:

- A clause a list verb opens after a comma lists the outputs as a sentence does (`sql_expansion.instruction_starts`,
  shared by `output_requests` and `command_positions`): "Sort all the shops by number products in descending order,
  and return the name, location and district of each shop." The verb is the command and names no table ("For each
  continent, list its id ..." joins no model_list).
- A possessive before an item asks nothing ("List his or her role, street, city and state."), and one opening the items
  names their owner, the one table named before them, where its fields realize every item (`request_fields`): "For each
  continent, list its id, name, and how many countries it has?" is the continent's ContId and Continent.
- An ordering word said with the rest of a field's name is that field, not an ordering: "What is the weekly rank for
  the episode ..." shows Weekly_Rank, where "rank" had ended the words naming the outputs.
- Where an answer shows only fields of a table the question names that it says only to filter on (`sql_rank.filter_only`,
  the ranker's existing −8, now one module function both read), the search adds the listing of that table's field
  naming its rows (`SQLSearcher._display_twin`): "What are the cities whose population is between 160000 and 900000?"
  lists the cities' names, "List all cartoon directed by 'Ben Jones'" their titles. Only a name field shows the rows:
  "orders with Amount over 1000" keeps its amounts.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-15): DEV 555 → 562 strict, held out
361 → 364, the fresh holdout 365 → 367, Gemini on 645 → 651.

- Fresh holdout 597 ("For each party, return the name of the party and the number of delegates from that party.") was
  lenient and is wrong: the list is read and the answer groups by the party's name, merging two parties named
  Democratic, where Spider's gold groups by the party's key. No other answer across the three sets groups an entity by a
  field its rows share.
- An earlier round (`pkg17`) also showed a product as the name of orders ("orders with Amount over 1000") and read "For
  each continent, list its id, name, ..." as the countries'; both are registered regressions.

## "Along with" adds an item; a list of fields and a count counts each group; "listed" asks nothing (2026-10-15)

Of 29 Spider questions (DEV, held out, fresh holdout) continuing a list with "along with", "together with" or "as well
as", 5 were answered right: the words joined nothing, and "of each car maker" ended the items before them.

- "Along with", "together with", "as well as", and "and" before a possessive add an item as "and" does, even past the
  rows the items before them name (`sql_expansion.output_requests`): "What is the full name of each car maker, along
  with its id and how many models it produces?", "the name and date for each race and its track name". "Different",
  "distinct" and "unique" before an item ask no field, and a bare noun added to a count by "and" is counted too ("the
  number of different series names and contents").
- A list asking for fields and a count asks the count in each of those fields' groups, and a "different" before a field
  says each group once, which grouping does; only one inside the count asks a count of distinct values
  (`sql_rank.QuestionRoles`): "Show different publishers together with the number of publications they have" counts
  each publisher's publications, not the publishers.
- The reading showing exactly the list's fields starts ahead of one adding a field to them, where each item names a
  field of its own (`SQLSearcher.search`): "Which owners live in the state whose name contains 'North'? List his first
  name, last name and email." shows no state, while "the names of climbers and the names of mountains" shows both.
- Where no field holds a state, "listed" says the rows are in the data and asks no condition
  (`query_contract.read_question`, formerly only "is/are listed" beside an aggregate): "How many transcripts are
  listed?", "the transcript with at least 2 courses listed". Where a status field holds 'listed' the word stays that
  value.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-15): DEV 562 → 568 strict, held out
364 → 369, the fresh holdout 367 → 374, Gemini on 651 → 658.

- An earlier round (`pkg18`) let the exact list start ahead where two items read one field and lost held-out 269, 977
  and fresh holdout 59 ("the names of climbers and the names of mountains"), and `pkg18b` lost the track name of fresh
  holdout 745 ("the name and date for each race and its track name"); each is a registered regression.

## "Find" opens a list of outputs; an item followed by "of" and its rows is theirs (2026-10-15)

"Find the id, name and age for visitors who visited some museums more than once." showed a museum's id and name beside
the visitor's age, and "Find the customer name and date of the orders that have the status 'Delivered'" showed each
customer's date_became_customer.

- "Find" is a list verb like "list" and "show" (`sql_expansion.output_requests`): an opening "find" with two or more
  items names the outputs, their owner and their order; with one item it lists nothing.
- An item followed by "of" and the rows of one table that realizes it is that table's field, whatever the list's owner
  (`sql_expansion.request_fields`): "the customer name and date of the orders" is the customers' name and the orders'
  order_date. The item still counts in deciding the owner: "the ids and names of each document, as well as the number
  of paragraphs" are the documents' (an earlier round left the item out of that decision and showed a template's id).
  Where no table holds the rows named after "of", the owner of the items decides it.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-15): DEV 568 → 570 strict, held out
369 → 370, the fresh holdout 374 → 376, Gemini on 658 → 661.

- Held out answers 2 questions it refused: 84 right, and 434 ("Find the city and name of bank branches that provide
  business loans") wrong: it shows a customer's name for the branch's bname, and Spider's gold lists the name before the
  city the question asks first.
- An earlier round (`pkg19`) left the item followed by "of" out of deciding the list's owner and lost DEV 370 ("the ids
  and names of each document, as well as the number of paragraphs" showed a template's id); `pkg19b` decides the owner
  over every item and pins only that item's field, and the case is a registered regression.

## "For which" opens no command; "amount of" says a quantity; a place names its people; an item's last word may stand for its field's kind (2026-10-15)

Four readings of the words a question uses, each a family the search or the completeness gate missed:

- A "which" after a preposition opens a relative clause, not a second command (`sql_expansion.projection_window`):
  "Return the nationalities for which there are two or more people" shows the nationalities, grouped, and "the towns
  from which at least two teachers come" the hometowns. The field before it had fallen out of what the question shows,
  and the people were grouped by name.
- "Amount of" says a quantity as "number of" does, read by a query aggregating or ordering its rows where no field of
  the tables it reads is an amount (`query_contract.read_question`): "the total amount of bonus" is SUM(Bonus), "the
  smallest amount of horsepower" the least Horsepower. "The total amount of payments" over an amount field is still
  that field's sum, never a count of the payments.
- A place's name holds the word for what is of it (`SchemaGraph._build_value_index`, read by the gate as the one-letter
  codes are): "African countries" are in Continent 'Africa', "Asian nations" in 'Asia', "European countries" in
  'Europe'. Only a capitalized one-word value ending in "a" or "e" adds the form (+"n", +"an").
- An item whose last word stands where its field's name says only the kind of its values ("number", "code", "date",
  ...), the item saying the rest of the name, is that field (`sql_expansion.realizes_request`): "the cell phone" is
  cell_number. A field whose own name says the item's words is it first.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-15): DEV 570 → 577 strict, held out
370 → 372, the fresh holdout 376 → 377, Gemini on 661 → 662.

- DEV answers 8 questions it refused: 5 right, 96 ("the model of the car with the smallest amount of horsepower") right
  but for one of its tied rows, and 132 and 154 wrong; it refuses 787, which it answered right only leniently. Held out
  answers 3 (491 and 943 right, 171 wrong) and the fresh holdout 3 (593 right, 699 wrong, 922 right only leniently).
- With Gemini on, DEV 154 ("the different years in which there were cars produced that weighed less than 4000 and also
  cars that weighed more than 3000") is now read by the engine, which keeps only the first comparison, where the
  rewording answered it; 762 and 763 ("asian country") are refused: the rewording said "Asia", and the rewording check,
  now reading "asian" as the value, asked it to keep the word.

## The row with the maximum is the row holding it; an extreme listed beside another output is a value; a rewording may say the value (2026-10-15)

Of 11 Spider questions (DEV, held out, fresh holdout) saying "with/has/have the maximum (minimum) ...", none was
answered right: "What is the name of the country with the maximum GNP?" listed every country beside its own GNP.

- An extreme after "with", "has", "have", "had" or "having" and "the" selects the row holding it, as "the highest"
  does (`sql_expansion.selects_extreme`): it asks for no aggregate where the search and the ranker read the question's
  aggregate words (`sql_expansion.asked_cues`), and the completeness contract asks for an ordering in its direction
  instead of a MAX or MIN (`query_contract.constraint_violations`). "The maximum GNP of countries" still asks for the
  value.
- An extreme the question lists beside another output is a value it asks for, not the rows holding it
  (`sql_constraints._scalar_aggregate_candidates`): "What is the total population and maximum GNP in Asia?" is
  SUM(Population) and MAX(GNP). The readings selecting the countries whose GNP equals the largest had pushed it out of
  the pool, and the question was refused.
- A Gemini rewording keeps a value it says itself where the question said a word for it
  (`question_rewrite._preserves_explicit_constraints`): "Asian countries" reworded as "countries in Asia" keeps Continent
  'Asia', which the entry below began to read for "Asian" (it lost DEV 762 and 763 with Gemini on). A one-letter code
  keeps its word.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-15): DEV 577 → 578 strict, held out
372 → 372, the fresh holdout 377 → 378, Gemini on 662 → 669.

- The rewording check reads only the Gemini path: it accounts for Gemini on 762 and 763, the losses of the entry below.
  Engine alone, DEV answers 2 questions it refused (722 right, 168 wrong), held out 2 (329 right only leniently, 75
  wrong) and refuses 406 and 666, both wrong before; the fresh holdout answers 549 and 912 right only leniently and 893
  turns lenient: the row with the minimum is every tied row, where Spider keeps one.

## A misspelled word the query reads is read; "listed" asks nothing beside an address state (2026-10-16)

A census of the selection (every pool member of every DEV question, its violations and the words the completeness gate
leaves unread) found 206 DEV refusals at the completeness gate, 75 of them with the selected reading already right.
Two of their families:

- A word the English vocabulary holds no use of (`engine/closed_class.spacy_model`), five letters or more and one edit
  (a letter added, dropped or changed, or two neighbours swapped) from a word the query reads, is that word misspelled
  (`query_contract.read_question`): "the cartools that are on the TV Channel", "the battles that lost any 'Brig' type
  shipes". A word the vocabulary knows is itself: "the country of all appelations" is no County, and "the course
  names" no field one letter from "name".
- "Listed" asks no condition where no field holds a listing or is named for a status (`query_contract.read_question`):
  "How many transcripts are listed?" was still refused because any field named for a state (an address's
  state_province_county) kept the word a condition. The rule of 2026-10-15 named these questions as its cases without
  firing on them.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-16): DEV 578 → 583 strict, held out
372 → 373, the fresh holdout 378 → 379, Gemini on 669 → 670.

- Gemini on, 612 is now served by the engine, where the rewording answered it the same. The first round of this change
  read a misspelling without the vocabulary test: held out, it answered 10 more questions, 8 of them wrong ("country"
  read as County, a name read as a field); that round is not this measurement.

## A typographic quote or apostrophe is the ASCII one (2026-10-16)

Questions typed on a phone or a Mac carry typographic marks: "Brazil’s population", the letter “a”, ‘Smith’. The
readers of a question parse the ASCII apostrophe and quotes only, so "Brazil’s" said "Brazil" and a lone "s", and “a”
quoted nothing. Selection reads a question with its typographic apostrophes and quotes, the replacement character a
mis-decoded one leaves, and the doubled backquote and apostrophe pairs of tokenized text written as the ASCII marks
(`sql_expansion.plain_quotes`, now `plain_marks`, at the start of `tables.TableQuery.select_query` and `search_pool`).

This is a correctness hardening, not an accuracy gain. Measured on Spider (`spider/results/RESULTS.md`, 2026-10-16),
strict accuracy is unchanged in every set (DEV 583, held out 373, the fresh holdout 379, Gemini on
670), and the fresh holdout answers one more question wrong (742, below).

- The DEV and held-out questions written with typographic marks (DEV 77, 715, 719, held out 202) are answered or refused
  as before: DEV 715 is refused for its possessive (the search read "Brazil's" as "brazil'"), held out 202 for "account
  balance", which names no acc_bal. The fresh holdout answers 742, which it refused, right only leniently: its quoted
  'Smith' is now read, and the answer shows the project's code beside its name.

## A possessive is its word; a word spelling out a field's abbreviation names it; a key named as the table it refers to names that table; only a request said again repeats its field (2026-10-16)

Four families, measured together on `02a2d69` (the review session's `7452cf2` and the entry below):

- The search read a question with its own tokenizer, which kept a possessive: "Brazil's" was "brazil'", so "Give me
  Brazil's population and life expectancies." stated no country 'Brazil' and "the owner's first name" named no owner,
  while the completeness gate read "brazil" and refused every reading. A possessive is its word in `sql_schema.canon`,
  the one comparable form of question words, names and values, and the search's and the recursive expander's
  tokenizers are `sql_expansion.tokens`, the one the gate reads with (the three copies differed only in the
  possessive). Of 34 possessive questions in DEV and the two held-out sets, 26 were refused or wrong.
- A word of seven letters or more spells out a field's or table's three- or four-letter name word that begins it
  (`sql_expansion.spells_out`, read by `says_in_another_form`, the one field and gate reader): "account balance"
  acc_bal, "enrollment" enr, "latitude" lat, "institution" Inst. A stop word abbreviates nothing ("percentage" is no
  Residents_per_officer); a form of the short word ("longest") and an ordering word ("descending") spell out
  nothing; and a word naming a table spells out no field (`entity_words`): "students" is no ENROLL.STU_NUM, the
  abbreviation of a table's name being read beside its table (`abbreviated_table`). 35 held-out questions saying
  such a field's words in full were refused.
- A key named as the table it refers to names that table wherever it sits (`SQLSearcher._simplified`): "the name of
  the project for which a scientist ... is assigned to" showed AssignedTo.Project beside Projects.Name, kept as the
  root table's own rows. 22 of 23 such answers in DEV and the two held-out sets were wrong.
- Only a request said again in the same words repeats its field (`sql_expansion.repeated_requests`, read by the list
  order, the requested projection, `ExpansionSupport.requested_fields` and the occurrence check of
  `query_contract`). The review's output-occurrence repair read two requests one field could realize as one request
  said twice: "List the contestant numbers and names" showed contestant_name twice (Spider DEV 688), and "the names of
  cities, as well as the names of the counties" the city's Name twice, the right answer refused as missing the
  repetition (held-out Spider train 269, 383). "List the id, name and id." still shows the id twice.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-16): DEV 584 → 587 strict, held out
370 → 377, the fresh holdout 383 → 390, Gemini on 671 → 674, with no loss in any set.

- The added wrong answers are questions the engine refused: held out 100 ("Alice's friends of friends", one step of the
  friendship read) and 409 (the stations never above 10 bikes, read as a NOT IN of the wrong rows), the fresh holdout
  156 (a total per customer where the question asks one total) and 818 and 971, right only leniently (the balance field
  shown beside each state's total).
- Against the base before the review session's 7452cf2 (the `pkg23` runs), `02a2d69` measured DEV 583 → 584, held out
  373 → 370, the fresh holdout 379 → 383 and Gemini on 670 → 671: its count-grain repair won DEV 966 and 968 and held
  out 719 and lost held out 412 and 654 (each counts the joined rows, or one filtered by a type, where the repair counts
  each exhibition or song once), and its output-occurrence repair lost DEV 688 and held out 269 and 383, which this
  change recovers.
- A first round of the abbreviation reading without the table-word guard lost held out 58, 557 and 938 ("students" read
  as ENROLL.STU_NUM, shown beside the name); that round is not this measurement.

## "Available", "the results" and "compute" ask nothing; an ordering from old to young starts at the oldest (2026-10-16)

A census of the selection on the held-out set (every pool member, its violations and unread words; 1,000 questions at
`ae1c0a4`) found 94 refusals with the selected reading right. Most are synonyms only a learned reading could resolve
("employees" for Staff, "airplanes" for aircraft) or readings right only by accident (a song's "Badlands" filter left
out); four word families were generic:

- "Available" says the rows are in the data where no table or field is named for availability
  (`query_contract.read_question`): "the files that are available in the format of mp4", "How many available hotels
  are there in total?". Over a table Other_Available_Features the word names that table.
- "The results" a sort verb orders are the answer's rows: "sort the results by the number of followers"; "the least
  number of results" still asks for results. "Compute" is a command, as "calculate" is.
- An ordering from one age end to the other starts at the first one's grade (`sql_extrema.span_grade`, read by the
  search's ordering and by the gate): "Sort all captain names by their ages from old to young" is an age descending,
  "in the order of their date of birth from old to young" a birth date ascending. The search ordered every such age
  ascending; Spider's strict grade compares rows as a multiset, so those answers would have scored without being right,
  and the gate reads the words only for an ordering in the grade's direction.
- "Alphabetic" is an alphabetical word (`sql_expansion.ALPHABETICAL_WORDS`).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-16): DEV 587 → 588 strict, held out
377 → 385, the fresh holdout 390 → 390, Gemini on 674 → 674.

- The fresh holdout answers 789, which it refused, wrong: "Compute the total amount of settlement across all the
  settlements." sums Payments.Amount_Payment where Spider sums Settlements.Amount_Settled. The search names no field by
  "settlement", the word of the Settlements table, so no reading summed Amount_Settled; "compute" was the only word the
  gate left unread.
- Held out 924 and DEV 414 order the ages descending; the search ordered them ascending before, and those answers were
  refused for "old" and "young" (Spider's strict grade, a multiset of rows, would have scored them).

## Relationships as serving discovers them; a measure's unit, a yes/no flag and a verb's doer are read; one closed field is the answer; "corresponding", titles and times are read (2026-10-17)

Families ported from the Codex review's frozen step worktrees (`.codex/worktrees/spider-<step>-2026101x`), each
re-implemented on main's owners rather than applied, and families the census of `6eed24e` ranked (every pool member,
its violations and unread words, on DEV and the held-out set):

- Relationships (the Codex steps relationship-proofs and self-relationships). `spider/probe/full_eval.py` gives
  Spider's declared keys to `TableQuery.ingest(explicit_fks=...)`, which merges them with the relationships it
  discovers from the rows, as serving does; the evaluator had replaced discovery with the declarations, so a partly
  declared database lost every undeclared relationship (flight_2's `flights.Airline` -> `airlines.uid`).
  `relations._name_boost` reads a key's role by canonical words ("caused_by_ship_id", "homeShipId" and "shipid" name
  ships; a bare `id` names no namespace, so overlapping integers prove nothing); a key named for the role it plays
  still references an entity's own named key (`stores.Manager_ID` -> `employees.Employee_ID`, the shipped regression
  gate); and a referenced key naming another role is no target (`membership_register_branch.Member_ID` is no branch,
  holdout2 411). Declared self-references are kept. A declared key blocks a discovered parent for the same column only
  where its name names its declared parent at least as well: Spider declares `actor.Musical_ID` -> `actor.Actor_ID`
  (held out 324), and the key's own word names `musical.Musical_ID`. `ExpansionSupport.inferred_entity_join` reads
  `SchemaGraph.discovered_foreign_keys`, the one discovery rule, instead of its own 95%-unique inference.
- Listing grain (the Codex step property-bags): a key into a catalog the query does not join sets no answer grain
  (`sql_rank.repeats_shown_rows`), so three qualifying cars with models A, A and B are three rows (DEV 97).
- One closed field (the Codex single-field reader): an opening instruction asking for one field, bound unambiguously,
  whose owner phrase names that field's table and ends there (`sql_expansion._one_closed_field`), with nothing
  coordinated after it (`list_closed`), is the answer's one output. The search drops a field no item asks for
  (`sql_search._in_list_order`) and then the join only that field read (DEV 625, 645, 646).
- A measure's unit: `sql_expansion.naming_words` leaves a measure's trailing unit or scale, with an "in" before it,
  unsaid ("the maximum net worth" is Net_Worth_Millions, DEV 1016), unless another field of the table differs only by
  it (length_feet and length_meters).
- Verbs: a word the tagger reads as an auxiliary or a finite verb, said in lower case and unquoted, names no stored
  value (`closed_class.predicate_words`; "players who won" filtered on a surname 'Won', "countries that do not speak
  English" on a code 'DO'); a modal is excepted ("contacts reached in may"). A verb is read by the field or table its
  agent noun names where the query uses one (`closed_class.verb_lemmas`, `agent_noun`): "won" by winner_id, not by
  loser_id. A relation verb's tables are named by the halves of a name written as one word
  (`closed_class.compound_parts`: countrylanguage).
- Flags: a field holding only yes/no codes and named as a flag (`IsOfficial`, `abandoned_yn`) is the condition field =
  its true value where the question says its words without the affix (`sql_expansion.flag_fields`,
  `flag_comparisons`): "How many official languages does Afghanistan have?" counts IsOfficial = 'T'. The search reads
  it among its value groups and the constraint expander alike; the expander's own "official" special case is removed.
  A negated word ("English is not the official language") and a code said after the word state no positive flag.
- Rule words (`query_contract.read_question`): "corresponding" pairs one output with another and is read where the
  answer shows two or more; "names" are read by the title of a table holding no name field; "time" by a date field
  the query reads.

Left as it was: grouped answers keep their groups first. The owner allowed the question's order if it made sense to
users; a table is read group first ("Month, Total", "Pet type, Average weight"), and Spider's official execution
metric ignores column order, so the 13 DEV answers right but for their order stay as they are.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-17): DEV 588 → 632 strict, held out
385 → 405, the fresh holdout 390 → 405, Gemini on 674 → 704.

- Fourteen of the DEV wins (flight_2 215-220 and 233-240) are no capability. Their gold answers are 0 or empty because
  flight_2 stores its airport codes with a leading space (' AHD'), and the engine's answers, joining flights to airlines
  as the gold queries do, are 0 or empty too; several read the wrong airport side. Without them the DEV gain is +30. DEV
  213 and 214 (JetBlue's 100 flights) are real.
- Losses: held out 546 (Products.parent_product_id discovered as a reference to Customers.customer_id, its role naming
  no customer) and 872 (a count of cards joined through the customers the new relationships connect); fresh holdout 488
  and 673 (bike_1) and 24 and 650 (a discovered relationship adds a join the answer does not need); Gemini on 768 (the
  flag condition's field shown beside the language) and 884.
- Newly answered wrong on DEV (12): 186 and 811 compare values the gold queries miss (a city stored as 'Anthony ',
  "north america" in lower case), 230 keeps tied airlines and 792 orders its columns as asked; 245, 246, 768 and 769
  show a condition's field beside the answer, 819 and 821 leave "predominantly" unread, 884 groups the students by name,
  and 922 reads "or" as "and".

## A condition's field is not shown; a count counts what its noun names, a field's values once each; superlatives, years and inflections name their fields; a set query shows the key said; a value is its display field's (2026-10-17)

- Condition fields (`sql_rank.flag_condition_only`, `filter_only`): a yes/no flag the question says only as its
  condition is not also shown ("the official language spoken in the country whose head of state is Beatrix" showed
  IsOfficial's constant 'T', DEV 768), nor a key the query joins on, named as the field of another table it compares
  ("flight numbers of Airline 'United Airlines'" showed flights.Airline, DEV 245, 246).
- Counts of what a key names (`sql_expansion.counted_references`): a count whose noun says a referring key's own word
  and not its table's name counts the referred rows. "How many car models are produced in the usa?" counted car_names
  rows, every car name of a model again (DEV 115, 147, 148, 109), and so did the noun of a threshold ("makers who
  designed more than 3 car models", DEV 149). The gate refuses such a count, the parsimony expander adds its twin
  counting the referred key (`sql_parsimony`), and the grain gate reads a "by" naming who acted ("produced by the maker
  with full name ...") as no grain, through `sql_expansion.by_groups`. A count that ranks groups is credited where it
  counts the rows its noun names at their own grain ("Which year has most number of concerts?", DEV 26, whose tied years
  the answer keeps), and a row number named by a counted table is no measure ("the most number of matches" is no ranking
  by match_num, DEV 461). A field holding the number itself is compared, not counted ("markets with number of shops
  greater than 50", held out 832).
- Counts of a field's values (`sql_expansion.counted_field_values`, removed by the entry below: the formesign-intake
  demo counts rows): a count whose noun names a text field whole, and no table, counts its values once each, in the
  search and the gate ("How many continents speak Chinese?" counted the countries, DEV 751). Of two keys a table holds
  to one other table the join is the one the question names, and the rows a noun names are counted by their key ("How
  many different addresses do the students currently live?" by current_address_id, DEV 577; "customers who hold an
  account" by customer_id, fresh holdout 44, 438; `sql_rank.named_parallel_key`, `count_distinct_key`). A table named
  right after the one a count's noun starts with is the noun's head (`counted_noun_tables`): "movie ratings" counts the
  ratings (held out 989), and a verb taking an object is none ("instructors teach a course", fresh holdout 648).
- Relationships: a key named for a role references an entity's own named key only where the role names no table
  (`relations._name_boost`): Products.parent_product_id names a product and is no reference to Customers.customer_id
  (held out 546, a loss of the previous change).
- Fields named beside a number: a year compares the year field the question names beside it, by a word not every year
  field holds (`ExpansionSupport.numeric_comparisons`; "orchestras founded after 2008", DEV 846), and a short field's
  inflection names it (`sql_expansion.inflects`; "teachers aged either 32 or 33", DEV 389, 390).
- Superlatives (`sql_extrema._superlative_targets`, `superlative_direction`): an adjective naming a dimension ranks by
  the field of it where the question names none (`SUPERLATIVE_DIMENSIONS`; "the longest match" by minutes, DEV 468),
  preferring the rows its noun names, and the gate reads an adjective after "most" or "least" by such a field of those
  rows ordered in that direction ("the most expensive phone" by Price, held out 110; 355 and 956 were refused for the
  same word). It infers nothing where a field's whole name follows it, and a field named away from it is not what it
  measures: Gemini reworded "How much does the most recent treatment cost?" as "the treatment with the latest
  date_of_treatment", which ranked by a dog's birth date, and "the money rank of the tallest poker player" ranked by
  Money_Rank (DEV 565, 569, 976, 977, 667, Gemini on, losses this change caused and fixed before release). A word one
  measure alone holds, said just before the superlative, names it ("the singer who is worth the most" by
  Net_Worth_Millions, DEV 1011), not before "the most times" (DEV 277); a rank's first place is its smallest number
  ("the highest rank of losers", DEV 439); and a field two tables share, named at the superlative, is the one of the
  rows named just before it ("the country with the smallest population").
- Values: a number a comparison reads is no stored value ("regions with two or more storms" holds no storm 'Two', held
  out 68); a word a several-word field's name abbreviates spells that field ("independence year" holds no city
  'Independence', DEV 788, now refused where it filtered on it); a value stored as said is meant before a padded copy of
  it ('AKO' before flights' ' AKO', DEV 199); and a value several fields of one table hold is the one its display field
  holds ('Angola' a country's Name before its LocalName; DEV 709, 713 and 716 compared LocalName).
- Outputs: a listing of one field's values, where the question names no table whose rows they are, is credited for
  showing each value once (`sql_rank.listed_values`; "Show all product colors", held out 644 and 693, still ranks the
  plain listing first); a key shown from a table the question never names gives way to the named table's own key (DEV
  323); a field said with "of" and its own table is that table's ("the distinct name of the mills", held out 142,
  `sql_rank.owner_phrase_fields`); a key every word of whose name is said is what each branch of a set query shows ("the
  ids of documents that contain the paragraph text 'Brazil' and 'Ireland'" showed descriptions, DEV 379, 380, 576); and
  a table named as one of the items to show, joined to another by "and" or "or", is shown by its display field
  (`ExpansionSupport.projection_columns`; "the most common amenity in the dorms" shows no dorm, held out 882): the
  Chrome gate of `608fc98` found the model's decomposition leaf "customer and product category for each purchase"
  refused, and the complex-category-gaps demo answered "I couldn't split this question" on `608fc98` and `6eed24e`
  alike.
- Population sets (the Codex review's population-presence reading): "Which states have both owners and professionals
  living there?" is the owners' states INTERSECT the professionals' (`sql_expansion.population_presence_reading`, built
  in `sql_recursive`, required by the gate; DEV 918, 919).
- "Corresponding" is read only where it says the output it pairs: "corresponding to each", a shown field (or a shown
  count's "number") named after it, or "corresponding to" the table whose fields are shown; a misspelling is the word.
  Read wherever two outputs were shown, it answered held out 111, 223, 400 and 809 wrong.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-17): DEV 632 → 664 strict, held out 405 → 415, the fresh
holdout 405 → 412, Gemini on 704 → 732.

- The Chrome gate of `608fc98` found complex-category-gaps answering "I couldn't split this question": Gemini's
  decomposition leaf "customer and product category for each purchase" was refused, on `608fc98` and `6eed24e` alike. It
  is answered now (a table joined to another item by "and" is shown by its display field).
- Measured as one package (runs `r4`). The rounds before it found losses fixed here: Gemini on, DEV 565, 569, 976, 977
  and 667 (a superlative's inferred dimension); DEV 277 ("awarded the most times"); held out 882 ("the most common
  amenity in the dorms" shown by dorm) and 989 ("movie ratings" counted as movies); the fresh holdout 44, 438 and 648 (a
  count of keys, and "instructors teach a course" read as the teaches table).
- No strict loss against 608fc98 on any set. Wrong answers fall on every set: DEV 166 to 138, held out 229 to 226, the
  fresh holdout 237 to 232, Gemini on 210 to 180.

## A count whose noun names a text field counts the rows (2026-10-17)

The release build of `1cd624d` failed its live demo-dataset suite: formesign-intake's "How many documents are still
pending?" answered 2, the distinct document types pending, where its sheet's rows are the documents sent and 5 are
pending. The entry above made a count whose noun names a text field whole, and no table, count the field's distinct
values (`sql_expansion.counted_field_values`, in the search and the gate) for Spider DEV 751 ("How many continents speak
Chinese?"). The data cannot tell the two readings apart, and the demo's answer is the product's contract, so the reading
is removed and a count of a field's noun counts the rows again. DEV 751 goes back to its wrong answer.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-17), against `608fc98`: DEV 632 → 663 strict, held out 405 →
415, the fresh holdout 405 → 412, Gemini on 704 → 731.

## Participles, verbs and measure nouns are read; "most" before a table counts its rows; a number after "id" is its key; a list shows each field; a bound away from an equality answers nothing (2026-10-17)

- Words the query realizes (`query_contract._witnessed_words`, `read_question`): a passive participle after a copula
  whose subject the query reads, with nothing after it but "in the world", says only that the rows are there, and
  "single" is one ("How many different degrees are offered?", "the languages spoken in the world", "not a single car
  maker"; DEV 173, 519, 732, 750, 759), unless the data's names spell it: "How many regions are affected?" names
  affected_region (held out 601, served as COUNT(*) FROM region before release, now refused). So are a verb relating a
  table named before it to a field the query reads named after it (DEV 603, 971, 973), "of origin" after such a field
  (DEV 640), "first" and "last" before no name or number grading a date as "earliest" and "latest" do (DEV 565, 691), a
  noun right after the one-word name of a numeric field the query reads, which is what the field measures ("the minimum
  snatch score", held out 649, 717, 788, 864), and the other inflections of a name word that is itself one ("years of
  opening" by Year_Opened, held out 921).
- Superlatives and counts (`sql_extrema._superlative_targets`, `sql_search`): "most" or "fewest" before a table's name
  counts that table's rows ("Which year had the most matches?" took the latest year, DEV 462); "recently" ranks by a
  date (fresh holdout 197); a field two tables share is the one whose table is named just before it, in the projection
  and the superlative alike ("the saving balance of the account with the highest checking balance"); a count word inside
  a several-word field name the question spells asks for no count ("in ascending order of room count", fresh holdout
  180); and a noun several fields share is counted by them in the order it names them, its entity's name first, the noun
  being the last word after "different" a field's name holds ("How many different degrees are offered?" counted the
  descriptions, DEV 520, a loss of the Gemini-on run fixed before release).
- Values and comparisons: a comparison symbol before a number is its words and "prior to" is "before"
  (`sql_expansion.plain_marks`, formerly `plain_quotes`; "salary > 80000" compared salary = 80000, fresh holdout 103);
  any comparative before "the average" compares with it (`sql_expansion.average_comparison`; DEV 98, fresh holdout 753);
  a number after "id" is the key of the table named just before it, and no count (`SQLSearcher._key_value_groups`; "the
  document with id 7", held out 336, 414, 435); a value right after a table's name is that table's display field's
  ("albums by the artist 'AC/DC'" compared a track's Composer, held out 386); the agent of a negated verb's object is
  excluded ("channels that do not play any cartoon directed by Ben Jones", DEV 643, 644); "start" is a route's source,
  and "src" and "dst" name its two ends (`sql_rank.TRAVEL_WORDS`); and one value bounded both ways is two branches, a
  conjunction bounding a column below where it bounds it above, or away from a value it equals, matching no row
  (`sql_ast.contradictory`; DEV 856, 857; "employees earning more than the employee with id 163" was served as
  EMPLOYEE_ID = 163 AND EMPLOYEE_ID > 163 before release, fresh holdout 189, now refused).
- Outputs: an answer shows each output a list names where it lists a count beside fields, or fields each its own ("names
  of technicians and the number of machines" counted the names, held out 138; "the claim date, settlement date" showed
  one date before release, held out 920 and fresh holdout 709, now refused), values a list joins being no outputs; every
  branch of a set query scores its projection by the words said of its fields (`sql_recursive._said_overlap`; held out
  278); and "alphabetic" and "increasing" or "decreasing order" order the rows (`sql_expansion.ordering_requested`; held
  out 244, 980).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-17), against `028c8c7`: DEV 663 → 685 strict, held out 415 →
433, the fresh holdout 412 → 434, Gemini on 731 → 738.

- DEV 27 ("What is the year that had the most concerts?") is a tie: 2014 and 2015 hold three concerts each. The answer
  now ranks the years by their concerts and keeps both, as DEV 26's did before; `028c8c7` ordered by the year itself,
  and Spider's LIMIT 1 happens to keep 2015.
- Wrong answers this change makes of questions refused before: an aggregate word that is a numeric field's whole name, a
  measure noun after it, is still read as the aggregate ("the stadium with highest average attendance", DEV 18, 19; "the
  highest total score", held out 664, 803), and "first-grade students" is read as a superlative (held out 923). Of the
  wrong answers the measured change first made, those of a participle a table's name spells (held out 601), an equality
  bounded away (fresh holdout 189) and one date of two listed (held out 920, fresh holdout 709) are refused before
  release, and DEV 520 is answered right.

## A superlative or aggregate word that names a field grades what follows it; the people counted are a population; a member is its table's row (2026-10-17)

- Cue words that are fields' names (`sql_expansion.cue_names`): a word asking a superlative or an aggregate that is a
  numeric field's whole one-word name (a stadium's Highest and Average, a body builder's Total) grades the field named
  right after it and names no field of its own, in the search's mentions and the gate's requested fields ("the stadium
  with the highest capacity", "the highest average attendance"; Spider DEV 18, 19, 43); before a noun no name of the
  data holds, where a superlative or an ordering grades it, it is the field and asks no aggregate (`asked_cues`, the
  gate's aggregate requests: "the highest total score", "in descending order of total scores", held out 664, 803).
  Before a noun nothing grades it still asks: "the total sales in Paris" totals. "The number of" after a command or
  "what is" asks for a count, as "how many" does (`query_contract._OPENS_WITH_COUNT`): "Find the number of concerts
  happened in the stadium with the highest capacity" showed Highest and counted nothing (DEV 43); no answer right on
  DEV, held out or the fresh holdout lacked a count.
- People counted are a population (`sql_expansion.counted_population`): "How many people live in Gelderland district?"
  totals the cities' Population (DEV 728, 767). A count of what a numeric field already counts is its total, as an
  option of the search's count (`counted_measure_columns`, the gate's count requirement already read it so), except
  where the counted noun names a table ("How many purchase orders are listed?" counts them, whatever a numeric "Purchase
  order" holds) or the field is a key ("Count how many appointments have been made" is no total of
  Prescribes.Appointment, fresh holdout 710, a loss the measured change first made, fixed before release). "The fewest
  people" is read by an ordering on Population in the gate; the search does not yet build it (DEV 805), and "the largest
  percentage of people" names no Population (DEV 816, 820).
- "Member" after the name of a table the query reads names its rows, as "record" does
  (`query_contract._witnessed_words`): every "staff member" and "faculty member" question was refused for it (DEV 415;
  held out 785; fresh holdout 76, 343, 567).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-17), against `76837a3`: DEV 685 → 691 strict, held out 433 →
436, the fresh holdout 434 → 437, Gemini on 738 → 744, no loss.

- Wrong answers this change makes of questions refused before, each wrong for another reason: "How many people live in
  Asia, and what is the largest GNP among them?" totals the population grouped by GNP, the gate reading "the largest" by
  any descending order (DEV 723, Gemini on too); and with "member" read, staff and faculty questions that need a set
  query or another relationship are answered ("faculty members who do not advise any student", "the phone numbers of all
  customers and all staff members", held out 627, 918; fresh holdout 31, 86, 888).
- The measured change first also read "people" as Population wherever it was said: "the language spoken by the largest
  percentage of people" showed Population (DEV 816, 820, wrong); it is read only counted or graded before release, which
  also leaves DEV 805 ("the 3 countries with the fewest people") refused.

## An extreme a compared subquery finds, a longer name's part and the rows a count counts are read; a referring key counts the rows it refers to; "how many total" asks one number (2026-10-18)

- Extremes a scalar subquery finds (`query_contract._select_violations`): the row holding "the highest", "the largest"
  or a selecting "the maximum" may be the one a scalar subquery the rows are compared with finds, by its aggregate or
  its first ordering with a row cutoff, where the subquery keeps every row the query keeps (its filters, and the tables
  its joins read): "What are the number of concerts that occurred in the stadium with the largest capacity?" and "How
  many cars have a larger accelerate than the car with the largest horsepower?" were refused for an ordering missing
  from the outer query (Spider DEV 44, 159). The maximum of every keyword is no maximum of those containing 'inspection
  checklist', nor the least populated country of all the one a join to a city keeps (DEV 789, a wrong answer the first
  measured version made, fixed before release).
- A name inside a longer one after "by", "per" or "each" is part of it, and a field's name after its table's names that
  field (the output-grain check): "Count the number of likes for each student id." asks for no other table's id, "For
  each country and airline name, how many routes are there?" the airline's name and no airline code, "How many phones
  belongs to each accreditation type?" no screen mode's Type (DEV 902, 903; held out 243, 655: all refused).
- The rows a row count counts (`query_contract._row_count_table`): a COUNT(*) in the answer, its ordering or its HAVING,
  over joins that each follow a foreign key, counts the rows of the one table no join refers to, and a field of it
  holding no numbers, named as the noun counted (after a threshold's number, "how many", "number of" or "count of"), is
  counted by it: "What is name of the country that speaks the largest number of languages?", "Which counties have two or
  more delegates?" (DEV 740, held out 573: refused for the Language and Delegate fields the counts count). A number
  before a numeric field's name compares it: "cities whose regional population is over 10000000", "employees who earn
  more than 30000 in salary" and "stations that have had more than 12 bikes available" count nothing (held out 912 and
  fresh holdout 9, 51, 488, 673: a loss or a wrong answer the first measured version made, fixed before release).
- Names the gate reads as the field they are: a one-word name the question says as two words ("What is the partition id
  of the user named Iron Man", partitionid, held out 612: refused for the tweets' id); "maximum" or "minimum" before the
  rest of a max_ or min_ field the query reads, as "average" before an avg_ one's ("the products with the maximum page
  size A4", max_page_size, held out 666: refused for an extreme it does not ask), while "the maximum page size" of
  products the query does not read is still asked (held out 625); and a key whose references lead to an asked table
  holds that table's keys ("List the id of students who attended some courses?", the attendance rows' student_id, which
  refers to the registrations' and they to Students', held out 506: refused for showing attendance fields).
- A key that repeats in its table, referring to another table's rows, tells those rows apart, not its own table's
  (`sql_rank` count_distinct_key): "Show the number of cities in counties that have a population more than 20000."
  counted the cities' distinct County_ID, the counties (held out 349).
- The table a list names right after its items owns them where they tie, by its name or the noun that ends it
  (`sql_expansion.request_fields`): "What are the names and ids of all makers with more than 3 models?" tied the
  countries' and car_makers' fields and read no list, so a maker's name alone was shown (DEV 150).
- "How many total" asks one number (`sql_expansion.asked_cues`, `counted_measure_columns`): "How many total tours were
  there for each ranking date?" is the total of tours, the count tours holds, and was answered with a count of tours
  beside that total (DEV 480); of a noun naming no numeric field it is the count.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-18), against `5cc3d95`: DEV 691 → 696 strict, held out 436 →
441, the fresh holdout 437 → 438, Gemini on 744 → 751.

- Wrong answers this change makes of questions refused before: "How many total tours were there for each ranking date?"
  shows each date's total, the reading asked, in the groups-first column order the gold reverses (DEV 480, Gemini on 479
  and 480); and Gemini on, a rewording of "the transcript which shows the least number of results" is answered with the
  earliest transcript's date (DEV 573): "most" and "least" are read without the count ordering they ask being verified,
  and the gate before this change accepts the same rewording and query.
- The first measured version (runs `w2`) lost fresh holdout 9 and 51 and answered fresh holdout 488 and 673 wrong,
  reading a number before a numeric field's name as a count of rows; runs `w3` are after its fix.

## People are persons; a table a read key refers to, "located" by a place field and "greater than" what is no number by its comparison are read (2026-10-18)

- "People" are persons (`sql_schema.canon`): "How many different cities do people originate from?" names the Person
  table and "Return the age of the person with the greatest height." the people table (fresh holdout 743, 973: both
  refused for the word naming the table). `sql_expansion.POPULATION_NOUNS` holds canon's form, so the people a
  population counts are still read.
- A table a key of a table the query reads refers to, said by its whole name, names the rows that key refers to
  (`query_contract._witnessed_words`): "Find the number of accounts with a savings balance that is higher than the
  average savings balance." counts the SAVINGS rows, whose custid refers to ACCOUNTS (held out 994: refused for
  "accounts"). A table no key the query reads refers to is not read so.
- "Located" and "situated" are read by a place field the query reads, one whose name holds a word of
  `closed_class.PLACE_WORDS` (city, country, region, address and the like): "In how many different cities are banks
  located?", "What is the number of distinct cities the stations are located at?" and "Which region is the city Kabul
  located in?" (fresh holdout 518, 609, Spider DEV 710: refused for "located"). A count of the banks reads no place, and
  "located" stays unread there.
- "Greater than" something that is no number ("whose age is greater than any engineer") is read only by the comparison
  the query makes in its direction, not wherever it is said: once "people" was read, "Find the name of people whose age
  is greater than any engineer sorted by their age." listed the engineers (fresh holdout 655, a wrong answer the first
  measured version made, fixed before release).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-18), against `ad53dc1`: DEV 696 → 697 strict, held out 441 →
442, the fresh holdout 438 → 442, Gemini on 751 → 751.

- A wrong answer this change makes of a question refused before: "Find the number of people who is under 40 for each
  gender." (held out 132) shows each gender before its count, the reading asked, in the groups-first column order the
  gold reverses.
- Verifying "most" and "least" by what the search reads them to grade, a count or total of rows or a measure they name,
  was measured over the questions of each set saying either (128 DEV, 120 held out, 125 fresh holdout) and not kept: it
  refused DEV 228 and 695, which the engine answers right by coincidence, ordering by another field ("the airport with
  the least flights" by a flight's FlightNo, "the contestant who got least votes" by contestant_number), so 72 → 70
  strict (held out 40 → 40, fresh holdout 43 → 43) for 116 → 111 wrong answers; a variant also reading "least" counts
  lost DEV 228, 461, 695 and fresh holdout 847 (72 → 69, 40 → 40, 43 → 42; wrong answers 116 → 113).

## A negation reaches its clause; living, corresponding and a trip's preposition are read; a difference keeps the rows it names (2026-10-18)

- A negation excludes the first value within seven words of it in its clause, not only within three or after "by", and
  "never" negates (`sql_search._negated_matches`, read by the search and the gate's value polarity): "Find the name and
  flag of ships that are not steered by any captain with Midshipman rank.", "customers who are not living in the state
  of California", "Return the names of conductors that do not have the nationality USA.", "Which models are lighter than
  3500 but not built by the 'Ford Motor Company'?", "customers who never ordered product Latte" (Spider DEV 171, 172,
  827, 833; held out 341; fresh holdout 376, 447, 619, 726 and others: the value read as kept, and the difference or
  inequality that answered refused). A clause break or another value between them ends the negation. Over the three
  sets, every value the wider scope newly excludes is one the gold query excludes.
- A verb saying where something is or lives (`closed_class.PLACE_VERBS`: located, situated, live, living, reside) is
  read by a place field the query reads that is no key; one a value is compared in must be of a table named before the
  verb, and a cell of a table the query does not read holding the verb (a song "Living on a Prayer") says nothing of it
  (`query_contract._witnessed_words`): "How many employees are living in Canada?", "What country does Roberto Almeida
  live?", "the total number of people living in the nations that do not use English" (Spider DEV 766; held out 474, 786;
  fresh holdout 70, 429, 500, 795: refused). The country of the customers the employees support does not say where they
  live, and "the different addresses that have students living there" over current_address_id reads no place (Spider DEV
  578, a wrong answer the first measured version made).
- Before the name of a table the query reads, "corresponding" says its rows related to those asked for
  (`query_contract.read_question`): "What categories have two or more corresponding books that were made after 1989?",
  "company names that have a corresponding movie directed in the year 1999" (fresh holdout 111, 223, 434: refused).
  Followed by a field's name it is read only where it pairs a shown output ("its corresponding problem id").
- Where no travel word says it, the preposition before a value a trip's end compares says which end
  (`sql_rank.travel_preposition`): "What is the average distance and average price for flights from Los Angeles."
  compares the origin, "to" the destination (held out 679, fresh holdout 992: the destination ranked first).
- A difference keeps the rows the question names before its denial (`sql_extrema.denial_position`, the position the
  difference builder already read, now shared with the gate): a table named there that only the subtracted part reads is
  refused (`query_contract._unkept_named_tables`: "Return the names of gymnasts who did not grow up in Santo Domingo."
  was every person's name less the gymnasts from there, held out 368), unless it is the name of a field of a kept table
  ("the major and age of students who do not have a cat"), a key the kept part shows refers to it ("the country codes
  for countries that do not speak English" keeps countrylanguage.CountryCode, Spider DEV 778, 779) or it follows "whose"
  ("reviewers whose ratings do not have a date", held out 597). A difference subtracting every row it keeps is refused
  (`_subtracts_every_row`: "the ids of courses without prerequisites" kept the prereq rows less themselves, fresh
  holdout 635, 855), and an exclusion's join to a table the question names in another form narrows nothing
  (`_dangling_unnamed_join` with `sql_expansion.says_in_another_form`: "prerequisites" names prereq).
- "First" or "last" joined to the next word by a hyphen names a kind of row, not a rank: where the question ranks by
  nothing else, a query keeping only its top rows is refused ("the first-grade students who are NOT taught by teacher
  OTHA MOYER" were answered by the lowest grade's rows, held out 485, 923).
- The difference builder reads a question as asking for fields of an entity's rows past "each" as past "the", "all" and
  "every" (`sql_extrema._describes_rows`): a Gemini rewording of Spider DEV 62, "What is the Major and Age of each
  student who does not have a pet with PetType equal to 'cat'?", was answered by an EXCEPT of the (major, age) values
  two students share, once the negation reached 'cat'.

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-18), against `5979e87`: DEV 697 → 702 strict, held out 442 →
447, the fresh holdout 442 → 458, Gemini on 751 → 756.

- Wrong answers this change makes of questions refused before, once the negation reached their values: "Show all
  headquarters without a company in banking industry." (fresh holdout 724) shows the headquarters of companies outside
  banking, with each company, where the question asks for those with no banking company; and "List the document ids of
  documents with the status done and type Paper, which not shipped by the shipping agent named USPS." (held out 5) shows
  status codes: the request for the ids is dropped where "and" joins two conditions after "with"
  (`sql_expansion.list_closed`).
- The first measured version (runs `z3`) lost Spider DEV 778, 779 and held out 597, and Gemini on DEV 62, to the
  difference check and the "each" reading; runs `z4` are after their fixes (a key the kept part shows, a name after
  "whose", `_describes_rows`). Its fresh holdout run was lost to a RunPod host that exposed no SSH and was not measured.

## The names asked for are their table's; a table named only inside a longer name is not joined; demonyms, initials and the information about rows are read (2026-10-18)

- "The names of" a table that holds a name of its own are that name (`query_contract._named_name_owners`): "What are the
  names of the albums that have more than 10 tracks?" showed the tracks' names and "the names of the 3 departments with
  the most courses" the instructors' (fresh holdout 600, 910), and "the name and country of origin for each artist" a
  song's (fresh holdout 582); the owner is the table named after the name word's "of" or "for", or after a later "for
  each", "for every" or "for all", past determiners and numbers. A table named before another ("the album tracks") or a
  possessive ("the singer's songs") is not it, and where a name word follows a possessive or a table's name, or its "of"
  names no table, whose names they are is not read. The field that names the owner's rows answers such a request where
  another table's field is called a name (the albums' titles beside tracks.name), and a self-join's shown field is its
  table's.
- Each output a list names is shown by a field it names, or by a field whose name says its words: "What are the names
  and areas of countries with the top 5 largest area?" answered by the cities' and countries' names is refused.
- A table the query joins to one other and reads nothing from, whose rows the join narrows and repeats, is named only
  where the question says its name outside the longer names of tables and fields the query reads
  (`query_contract._join_named_inside_longer`): "Show all document type codes, document type names, document type
  descriptions." joined the documents and showed only the types some document has (fresh holdout 917), and "the pixel
  aspect ratio and nation of the tv channels that do not use English" subtracted only the channels a series names
  (Spider DEV 639). A query that counts reads every joined row and is exempt ("the transaction type code that occurs the
  most frequently").
- Past a word opening the condition its rows meet ("with", "where", "whose", "who", "that"), "and" joins conditions and
  closes an instruction's list (`sql_expansion.list_closed`): "Find the districts in which there are both shops selling
  less than 3000 products and shops selling more than 10000 products." asks for the districts alone (Spider DEV 294:
  refused), "List the document ids of documents with the status done and type Paper" for the ids.
- A country's cell holds the word for what is of it where its name does not begin that word
  (`closed_class.COUNTRY_DEMONYMS`, read into `SchemaGraph.value_index`): "the average, minimum, and maximum age for all
  French singers", "the singers who are not French citizens" (Spider DEV 5, 1007: refused), unless a field holds the
  word itself (a language 'French').
- A capital initial before a capitalized word is a word of its own (`sql_schema.spaced_name`, now the one camel-case
  split every reader of names makes, seven copies of the expression before): CName is a course's name and DName a
  department's ("What are the course names, ordered by credits?", held out 267; fresh holdout 696, 914), GNPOld "GNP
  Old"; and a field holding a part of a person's name (Fname, emp_lname) names its rows (`sql_schema.is_name_column`).
- A word one measure alone holds, said right after a superlative and nowhere else, names it, as one said just before
  does (`sql_extrema`): "the country that has the largest area" ranks by SurfaceArea (Spider DEV 791). Said as an output
  too, the ordering the output's request builds ranks by it ("the names and areas of countries with the top 5 largest
  area", Spider DEV 800, where the first version's readings displaced that answer from the pool).
- The information of, about or on rows, and all the information after up to two words, is their every field
  (`sql_expansion.complete_projection_requested`): "List the information of all instructors ordered by their salary in
  ascending order.", "What information do you have on colleges sorted by increasing enrollment numbers?" (fresh holdout
  527, held out 422: refused).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-18), against `e7fc8d4`: DEV 702 → 708 strict, held out 447 →
454, the fresh holdout 458 → 466, Gemini on 756 → 762.

- Wrong answers it makes of questions refused before: Spider DEV 80 ("list their ids instead of names" is a count beside
  the students' last names, LName now naming them), held out 658 ("in the order of their names" ordered by a sport's
  name), fresh holdout 880 (the authors' every field, joined to their courses) and 693 (gold's own query, whose rows the
  grader does not match). Answers it now refuses that were wrong: held out 628, fresh holdout 106, 134, 921.
- Fresh holdout 574 is the one loss: a coincidence the gate now refuses (it compared surname = 'Driver' and showed lap
  times, the surnames listed not shown). A first version's losses were fixed before these runs: a self-join's shown
  title read as an alias's (fresh holdout 539, 790), and the readings of "the largest area" displacing Spider DEV 800's
  answer from the pool.

## The pool holds no exclusion the gate refuses for its shape; "never" and what rows do not have are exclusions; a list's "for" names its names' owner (2026-10-18)

- Past its top reading, which stays first for the decomposition probe (`decomposition.compound_candidate`), the search
  keeps no reading whose exclusion or difference the completeness gate refuses for its shape in its pool
  (`query_contract.exclusion_refusals`, the one predicate the gate and `sql_search.SQLSearcher` read: an exclusion
  narrowed by a table the question does not name, a difference keeping rows that are not those asked for, a difference
  subtracting every row it keeps), so none takes the place of a reading the gate may serve: "What are the ids of courses
  without prerequisites?" filled the 25 places with exclusions narrowed by sections, departments and teaching (fresh
  holdout 635, 855, 178: refused).
- "Never" excludes as "not" does (`closed_class.EXCLUSION_CUES`): "the artists that have produced a song in English but
  have never received a rating higher than 8" was answered by the songs rated over 8 (held out 48, a wrong answer a
  first version of the pool change made), and "the average credit score for customers who have never taken a loan" and
  "the mean longitude for all stations that have never had more than 10 bikes available" by readings excluding nothing
  (held out 826, 409: now answered by their differences).
- What the rows "do not have" (or are "without", or "never" have) is absent from every row they relate to: an inequality
  on a table the shown rows reach through rows referring to them is refused (`query_contract._inequality_across_many`):
  "the students who are living in the dorms that do not have amenity TV Lounge" kept every dorm with another amenity
  (held out 72, a wrong answer the pool change made).
- The names a list asks "for" a table are its own, past a determiner or none (`query_contract._named_name_owners`):
  "What are the names and average salaries for departments with average salary higher than 42000?" showed the
  instructors' names (fresh holdout 848, now answered by another reading that is still wrong).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-18), against `037405d`: DEV 708 → 708 strict, held out 454 →
456, the fresh holdout 466 → 469, Gemini on 762 → 763.

- No loss on any set. Answers it now refuses that were wrong: held out 48 and 72 (wrong answers a first version made of
  questions refused before, once the pool held other readings). A first version's losses were fixed before these runs:
  dropping refused readings before ranking changed the search's top reading the decomposition probe reads
  (tests.test_complex_datasets), and "never" as an exclusion cue left the word unread by a difference (Spider DEV 282).

## A most common value is ranked once, over the rows it is common among; a count meets only a number of rows; a key shows the rows it refers to (2026-10-19)

- A most common value is ranked once: the frequency superlative takes no candidate whose filter keeps the value a
  subquery counts most common (`sql_extrema._finds_most_common`, the scalar frequency selector of `sql_constraints`).
  "What is the name of the most common genre in all tracks?" kept the genre a subquery found most common among Genre's
  own rows, each once, and ranked that one genre by its tracks; such readings filled every place of the pool (held out
  940, 972, 880, 9, 707).
- Groups a count ranks for its top ones count nothing by any field telling the one table's rows apart, in the query or
  in a scalar subquery it compares with (`query_contract._select_violations`, beside the key's refusal): "the most
  common amenity in the dorms" ranked Dorm_amenity's own names, and "the dogs of the rarest breed" kept the breed a
  subquery found rarest among Breeds' own rows (held out 882, fresh holdout 638, Spider DEV 954, 955).
- What is "most common" among rows is counted over them: a join to a table the question does not name, and the tables it
  names, read or not, do not need, repeats them (`query_contract._multiplies`): "the most common first name among all
  actors" counted each actor's film_actor rows (held out 156). "The most pets" says no frequency word and counts the
  rows holding them.
- A count meets only a number the question states as one of rows, a noun after it, or "count", "a count of" or "the
  number of" before it (Gemini's rewording "a count of performance more than 1"); a number its sentence ends on is a
  field's value (`query_contract._states_rows`): "Which rooms cost between 120 and 150? Give me
  the room names." kept the rooms counted between 120 and 150 times, and "the names of models that launched between 2002
  and 2004." the models counted so (fresh holdout 28, 616; none of the 13 such counts served on Spider DEV, held out and
  the fresh holdout was right, one strict by a HAVING COUNT(*) < 120000 every group met).
- A key shows the rows it refers to, and a longer name's last word the opening says names its table where the question
  says its other words, a name's or an id's word never (`query_contract._asked_tables`): "What is the most common
  amenity in the dorms?" showed the dorm names, then the dorm ids Has_amenity holds (held out 882; "the names and ids of
  all countries with at least one car maker" asks for no car_names, Spider DEV 124).
- A table holding no name is named by a table its keys refer to, joined to it, and one modifier before the table that is
  no possessive (`closed_class.POSSESSIVE_DETERMINERS`) leaves it the names' owner
  (`query_contract._named_name_owners`): "the first and last name of all biology professors" showed the names of the
  department's students, then of its head, where PROFESSOR's EMP_NUM refers to the EMPLOYEE row (held out 304).
- "Lexicographical" order is alphabetical (`sql_expansion.ALPHABETICAL_WORDS`, which `ordering_requested` now reads):
  "List the section_name in reversed lexicographical order." was refused (Spider DEV 527, 579, fresh holdout 140).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-19), against `bf1c242`: DEV 708 → 710 strict, held out 456 →
462, the fresh holdout 469 → 473, Gemini on 763 → 765.

- Held out 156 ("the most common first name among all actors") is now the gold reading, actors grouped by first name,
  and three names tie at the top: the tie rendering serves them all (the doubled reading was right by picking one).
- Fresh holdout 698 was right by a HAVING COUNT(*) < 120000 that every group met, now refused for counting rows the
  question states as a value; the gold intersects two sets of investors.
- Spider DEV 579 ("all the student details in reversed lexicographical order") is answered now that "lexicographical" is
  read, by the students' first names rather than their other_student_details: a new wrong answer.

## A table's descriptions and details are its own; a number before a field's two-word name compares the field (2026-10-19)

- A table's descriptions and details are its own, by the reading that finds the owner of the names asked for
  (`query_contract._named_name_owners` with a field of `_FIELD_KINDS`), a table holding none being described by a table
  its keys refer to, joined to it: "What is the description of the product category with the code 'Spices'?" showed the
  products' description, "the different descriptions for templates" the documents' (the template types' are asked), and
  "the project details of the project" the outcomes' details (held out 265, Spider DEV 354, fresh holdout 33). A table
  named right before the word is its owner ("the document type description"), and so is the table of a field the words
  before it name, by the longest name they end: "the color description for the product" is Ref_Colors' and "the draft
  detail of the document" a draft's (held out 536, 336); "other" names no owner (`closed_class.GENERIC_MODIFIERS`,
  Spider DEV 580).
- A number before a numeric field's name compares the field where the name follows it in two words
  (`ExpansionSupport.threshold_targets_column`): "the stations that have ever had more than 12 bikes available" filled
  its pool with stations counted more than 12 times (fresh holdout 488, 673).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-19), against `b0d9108`: DEV 710 → 711 strict, held out 462 →
463, the fresh holdout 473 → 476, Gemini on 765 → 766.

## Each listed output is shown by a field of its own (2026-10-19)

- A field a count or a total reads shows no field its words say (`query_contract._select_violations`, the listed-output
  check): "Show id, first name and last name for all customers and the number of accounts." showed no customer id, the
  id said by the COUNT(account_id) it showed (fresh holdout 358).
- A list whose items each say the head word of some field's name lists fields, where no one field realizes an item, so
  an answer showing fewer outputs than it lists is refused: "What are the names of the tourist attractions and the dates
  when the tourists named Vincent or Vivian visited there?" was answered by the names alone (fresh holdout 764, 842). A
  value the query compares is no output ("producing patent and paper as outcomes", fresh holdout 33), and two items one
  field's name joins are that field ("the clean and jerk score" is Clean_Jerk, held out 664).

Measured on Spider (`spider/results/RESULTS.md`, 2026-10-19), against `b306132`: DEV 711 → 711 strict, held out 463 →
463, the fresh holdout 476 → 479, Gemini on 766 → 766.

- The change refuses answers more than it adds right ones: wrong answers fall by 2 on held out, 7 on the fresh holdout
  and 1 with Gemini on (fresh holdout 43, 699, 848, 887 and held out 430, 760 are now refused).

## Learned readers replace the lexical search; the Gemini rewording is gone (2026-10-21)

The owner redirected the accuracy program on 2026-10-19: the aim is accuracy on databases the engine has never seen,
by learning to read a schema instead of adding word rules and question-specific patches, while staying encoder-only,
deterministic and interpretable, with no model writing SQL.

**What replaced what.** The own-data planner was the lexical typed search (`engine/sql_search.py` with its
constraint, parsimony, recursive and extrema expansions), ranked by hand-written features (`CandidateRanker`),
checked word by word against the question (`query_contract.read_question`, `coverage`, `constraint_violations`), and
reworded by Gemini when nothing ran (`engine/question_rewrite.py`). It is replaced, in the same change, by the readers
(`engine/readers/`):

- stage 1 (XLM-R base) scores each table's and column's use for the question; the six most probable tables are kept;
- stage-2 readers (XLM-R large) read the question beside the kept tables as named readings (structure, per-column
  select/condition/group/having/order roles, counts, value and limit pointers, shares); one reads the question's
  values listed beside the columns that hold them (anchors), one does not, and their candidates are pooled;
- a deterministic builder (`readings.build`) makes each reading a typed AST, joining along foreign keys;
- the pool runs and is grounded as before; a re-scorer (XLM-R base) orders it by the question beside each candidate's
  read-as line and result shape; a logistic refusal fitted on held-out databases declines a choice it is not
  confident of, and the reply says how the question was read ("I read this as …").

The response carries `read_as`, which the thread shows as the turn's "read as" line, in place of the `fallback`
record of the rewording. Deleted with the search: `sql_search`, `sql_constraints`, `sql_parsimony`, `sql_recursive`,
`sql_extrema`, `question_rewrite`, `sql_prompt`; `CandidateRanker`, `SemanticSignals`, `FallbackRecord` and the
money-total preference in `sql_rank`; all of `query_contract` but the unreadable-measure checks and `Coverage`; the
helpers in `sql_expansion`, `sql_rank`, `closed_class` and `sql_durations` only the search read (a reachability
prune against `b8bedde`, leaving what was already unused there alone); 251 tests of the search in
`tests/test_sql_ast.py` and 83 in `tests/test_query_contract.py`. Routing's own-data shortcut
(`routing.reads_upload_whole`) now reads the readers' accepted selection and the world types and measures the
upload lacks (`knowledge_query.asked_world_types`, `WORLD_MEASURES`), not the deleted word reader.

**Generic semantics only.** The decoder and builder keep rules that hold across schemas and wording: a date the
question names is read through `engine/sql_dates`, and so is a period it groups by ("per month" groups by the date's
year-month); a stored date literal stays exact; a column an equality pins is not shown, grouped or sorted by again; a
column a condition matches with the question's value (= or LIKE) is counted by its rows, not its distinct values (one
of 16,254 gold blocks of Spider and BIRD train counts such a column distinct); a pointed value drops the marks of its
list; two values of one column the question names (matched on the data) are also
read as either, since a row holds one value of a column; a grouped block's plain number sort column is each group's
total, and groups computing nothing are distinct rows; the first row of the one column shown and ranked is its MAX or
MIN; a contained text compares `LOWER(column)` with the lower-cased text (SQLite's LIKE ignores ASCII case and
PostgreSQL's does not); a share's read-as line names the share. The perfect-readings oracle is unchanged by them (984
of 1,053 calibration questions). No question-specific word list or patch remains in own-data selection.

**One stopgap, with its removal condition.** The readers' structure head reads the complex demos' multi-goal prompts
("the top 3 products … and the top 2 customers …, then list each pair …") as one query, with confidence near zero for
any set operation. In an analysis, a declined question longer than 30 words (`decomposition.ONE_QUERY_WORDS`: 99% of
the readers' training questions have at most 27) asks for the decomposition proposal instead of a clarification. It is
removed when the readers are trained on compound questions and their structure head reads them.

**Training and data.** Spider train and dev (the 20 calibration databases and the 40 test databases excluded), BIRD
train (6,558 readable questions plus 195 percentage questions verified by execution) and Spider-Syn's rewordings.
Calibration is 20 held-out Spider train databases (1,053 questions); the frozen Spider test (40 databases, 2,147
questions) is scored in totals only, at milestones. The pipeline is `training/readers/`; `training/readers/promote.py`
is the one writer of `engine/data/readers/`. Experiment contracts and per-run metrics are under
`training/readers/README.md`.

**Precision and memory.** Serving runs each encoder in bfloat16 with float32 heads (`engine/readers/models.load`):
half the memory of float32 and about 3.5 times faster on the development CPU, with p99 probability drift 0.013 on
calibration rows; int8 dynamic quantization moved readings too much (p99 0.30) and is not used. The two large readers
take about 1.1 GB each in bfloat16 and the two base models about 0.55 GB each, beside about 2.5 GiB of the engine's own
models: an 8 GiB instance kept little room for three requests at once. Engine instances, and the build and suite
containers that mirror their cap, now have 16 GiB (`infra/main.tf`; about $275 a month per warm instance at list
prices, against $230). A four-reader pool with a combiner chose 621 of 1,028 on calibration (re-scorer "us") against
612 for the two shipped with the same re-scorer; it would fit 16 GiB and is not shipped.

**Re-scorer.** The shipped re-scorer ("f") trains on the union of the shipped recipes' own cross-fold candidate pools
(v7a anchored and v7b plain, four folds each; `training/readers/rescorer_data.py`), BIRD share candidates relabelled
against 100 times gold: on the shipped pair's calibration pool it chooses 624 of 1,028 right, against 612 and 601 for
re-scorers trained on older recipes' pools.

**Refusal.** The refusal declines below the confidence at which the readers answer no more questions wrongly on
calibration than the engine they replace (`training.readers.refusal match`, threshold 0.2511): 587 right of 881
answered against the engine's 418 of 712, the same 294 wrong.

**Measured** (`spider/results/RESULTS.md`, 2026-10-20), through `spider.probe.full_eval`, whole_db, served:

- Frozen Spider test (2,147 questions, 40 unseen databases, totals only): engine alone 939 (43.7%), production with
  the Gemini rewording 1,126 (52.4%), the readers 1,351 (62.9%) strict and 1,535 (71.5%) lenient; 1,314 right of
  1,875 answered with the refusal on.
- Calibration (1,053): engine 418 → readers 617 strict; 274 wins, 75 losses.
- The live demo-dataset gate (`tests.test_datasets`, 2026-10-21, this bundle, no connection errors): `b8bedde`
  passes every case; the readers fail 5 (6 before the matched-column count rule). The three compound demos' prompts: a ranking leaf "by total spend" or "by
  total revenue" is read as a ranking by row count, since no column is named so and the re-scorer prefers the count
  to the `SUM(line_total)` reading in the pool. "What is the highest purchase cost?" ranks by another column (the
  right value by chance; until the sort could keep only the shown columns, the Python program refused it, as it did
  230 of the readers' 1,052 chosen calibration queries). "What is the highest
  Net PO Value?" is read right and declined (confidence 0.247 against the threshold 0.251). "How many Intake Consent
  documents are there?" counted the distinct document types (1) until the matched-column rule; it answers 6.
- With the bundle installed, the hermetic suite fails `tests.test_complex_datasets` (5: the same leaves, and "List the
  product names that no customer from Paris has bought" read as one query without its negation) and the enrichment
  benchmarks, now graded on the served answer instead of the pool's first candidate: the currency-symbol join is in
  the pool but not chosen, and the IANA country activation gate has 64% top-1 and 80% pool recall against its 95%.
  Without the bundle (public CI) these model-backed tests are skipped. The offline regression gate
  (`regress.run_regression --offline`, which the engine build runs) passes 12 of 13: "total revenue by title", a sum
  grouped across a foreign key, is declined.
- **Not releasable.** The engine build runs the full product suite on the candidate image and publishes no image that
  fails it; this change is committed, not deployed, and production stays on `b8bedde`.

**Known gaps.** Each failure above is a reading the training data does not cover: measures named by a business
word the schema does not use, negated existence, joins to an enrichment's lookup table, and wide spreadsheet tables.
The next step is training data for those families on the training databases (never the demo questions). The
weights are published at Hugging Face revision
`b9bb6521799ca603845a8e41248ff3abfe70fb40`, verified file by file by an anonymous download against the manifest.

## Four readers trained on the structures they misread; a choice replaces the refusal (2026-10-22)

The readers bundle above failed its release gates on readings its training data rarely held. Each family below got
training data built on the training databases, never from the demo questions, and four new reading models replace
the two:

- **Written training questions** (`training/readers/rewordings.py`, `lookups.py`, `structures.py`; Gemini on Vertex
  AI writes the words, the gold readings come from the query): training questions reworded with an everyday measure
  word, as negated existence, and as the opposite extreme (MIN and MAX swapped in the readings; the training data
  asked for the highest four times as often as the lowest); questions over lookup tables built from a categorical
  column (the name alone, a label beside it, counts, filters); and questions written for queries built from each
  database's foreign keys and values: rows no referencing row matches (`NOT IN`, at most once filtered through the
  referencing table or a table it references), groups ranked by a number's total or average (kept only where that
  ranking's first groups differ from the ranking by row count), and the lowest, highest, average or total of a
  number (MIN, MAX, AVG and SUM equally often), half of them written as the few words a person types into a search
  box. 4,393 rewordings, 1,724 lookup and 2,675 structure questions; 29,561 stage-2 rows.
- **Four readers** (XLM-R large: anchored and plain, seeds 0 and 1; 13 epochs, checkpoints from epoch 5) replace
  v7a and v5. A pool of all four with the shipped re-scorer: right answer in the pool for 788 of 1,028 calibration
  questions (767 for the four earlier readers), the re-scorer alone 657 (628).
- **The choice** (`choice.json`, `training/readers/choice.py`) replaces the refusal: a logistic model over the
  re-scorer's logit, the candidate's log probability, its best rank among the readers, the share of readers whose
  readings build it, and an empty result, fitted on the calibration pool cross-validated by database (664 of 1,028
  against 657 for the re-scorer alone). It ranks the candidates; its probability for the first is the confidence,
  and the threshold is matched on calibration as before (0.1421: answered 943 of 1,053, 649 right, 294 wrong, the
  engine's 294).
- **Generic decoder rule:** a period the question asks for ("per month", `sql_dates.period_grouping`) groups the one
  date column of the tables the readers read when they group only by surrogate keys or by nothing ("how many orders
  per month?" was read as one group per order by all four readers). It reaches no calibration question and 6 of
  the 2,147 test questions.
- **One convention for a joined attribute** (the owner's choice, 2026-10-22): "Show the currency symbol for every
  order" shows the symbol alone, one row per order, as the country-name benchmark shows the name alone
  (`regress/domain_enrichment.py`).
- **The complex-dataset contract:** the three multi-goal prompts still ask for the decomposition proposal; "List the
  product names that no customer from Paris has bought" is one query (the products whose name is not among the names
  Paris customers bought) and is answered directly, with the rows derived from the CSVs
  (`tests.test_complex_datasets`).

**Measured** (`spider/results/RESULTS.md`), served through `spider.probe.full_eval`, whole_db:

- Frozen Spider test (2,147 questions, 40 unseen databases, totals only): engine alone 939 (43.7%), production with
  the Gemini rewording 1,126 (52.4%), shipped two readers 1,351 (62.9%), these readers 1,414 (65.9%) strict and
  1,611 (75.0%) lenient; at the matched threshold 1,387 right of 1,972 answered.
- Calibration (1,053): engine `b8bedde` 418, shipped two readers 617, four earlier readers 641, these readers 663
  strict (63.0%; 746 lenient). Against the engine 298 wins and 53 losses; against the shipped readers 78 and 32.
- The gates pass with this bundle: `tests.test_complex_datasets` 10 of 10, `tests.test_enrichment` 37 of 37 (the
  IANA activation gate and the currency benchmark included), `regress.run_regression --offline`, and the live
  demo-dataset gate (`tests.test_datasets`, every prompt and non-chat follow-up: 92 of 92).

**Known gaps.** "minimum amount" over a one-table sheet is read as the lowest by the anchored readers and the highest
by the plain ones, and the re-scorer picks the highest: it learned on pools where the highest dominates. "maximum
notice_days" is right only because of the same lean. A re-scorer trained on these readers' own pools is the next
step. Weights: Hugging Face revision `d4949122da66f04e8fce1ea68f3e2e0ecb9a0327`.

## Two readers trained on the grouped structure; a membership compares one kind of value; long questions are decomposed (2026-10-23)

The bundle above passed every gate on the Windows development machine and failed two on the Linux build image,
same weights and code: "total revenue by title" (`regress.run_regression --offline`: one reader of four read the
total, and the choice declined it at 0.129 against 0.1421) and the Paris negated-existence prompt
(`tests.test_complex_datasets`). The gates are single questions, and bfloat16 matrix products on another CPU move
the ones decided by small margins. Each fix widens a margin; none is per platform:

- **The grouped structure** (`training/readers/structures.py`, `grouped`): each group beside the total or average
  of a number, unranked, as "total revenue by title" asks. 806 questions on 195 training databases, built and worded
  as the other structures; 30,367 stage-2 rows. Four readers trained on v11's data plus these (v12a, v12a1, v12b,
  v12b1; `training/readers/README.md` 2c, 4c). The bundle keeps v11a and v11b and takes v12b1 and v12a1 (slots c and
  d): of the four-reader pools compared, its choice is right on the most calibration questions out of fold (667 of
  1,028; the four v11 readers 664, the four v12 readers 659). "total revenue by title" is now read by three readers
  of four (confidence 0.64, the threshold 0.1434).
- **A membership compares values of one kind** (`engine/readers/readings.py`, `engine/readers/search.py`
  `referenced`): every reader read "List the product names that no customer from Paris has bought" as "product id
  is not among (the product names Paris customers bought)". A number never equals a name, so every product passed.
  The builder refuses a membership that compares a number column with a text column, and the search reads it again
  on the column the listed values are values of: the listed column itself when it belongs to the compared table,
  else the one column a foreign key pairs it with (purchases.product_name -> products.product_name). The prompt is
  answered with Delta and Omega at confidence 0.78, its read-as line naming the product name.
- **Questions past the readers' calibrated length are declined** (`engine/tables.py` `TableQuery._choose`,
  `engine/decomposition.py` `ONE_QUERY_WORDS`): the new bundle read the 44-word category-gaps prompt ("Find the top
  2 product categories by revenue and the top 2 customers by total spend, then list each ... pair ...") as one
  ranking at 0.19, above its threshold. The threshold was matched on calibration questions of at most 29 words, so
  it says nothing of a longer one. A question longer than 30 words is declined however confident the choice: in an
  analysis it asks for the decomposition proposal, as a declined long question already did, and outside one it is
  asked about. It reaches no calibration question and 2 of the 2,147 test questions (counted without reading them).
- **The choice refitted** on the new pool (`choice.json`), threshold matched on calibration at 0.1434: answered 952
  of 1,053, 658 right, 294 wrong (the engine's 294).

**Measured** (`spider/results/RESULTS.md`), served through `spider.probe.full_eval`, whole_db:

- Calibration (1,053): 669 strict (63.5%; 749 lenient) against 663 for the bundle above; 25 wins and 19 losses
  against it, 308 wins and 57 losses against the engine.
- Gates in the Linux build image (Python 3.11), the bundle installed local-only: `regress.run_regression
  --offline` 13 of 13 and every hermetic suite (`tests.test_complex_datasets` 10 of 10, `tests.test_enrichment`
  37 of 37) but `tests.test_release`, which needs the published revision and passes with it. On Windows:
  `regress.run_regression --offline` 13 of 13, `tests.test_complex_datasets` 10 of 10, and the live demo-dataset
  gate (`tests.test_datasets`) 92 of 92.

**Known gaps.** "minimum amount" over a one-table sheet is still answered with the highest: more of the readers'
readings build the lowest (share 1.0 against 0.75), and the choice, led by the re-scorer's logit, picks the highest
at 0.25. A re-scorer trained on these readers' own pools is the next step. A long single-goal question is now sent
to decomposition, which needs two to four subquestions, so the chat answers "Try asking the parts as separate
questions" where it could have answered it whole. Weights: Hugging Face
revision `fd08934c5a5d45c31ed776c56cbc2d240d2f9d89`.

## The release build's live suites: every column, sheet language, rankings that rank, and values the upload lacks (2026-10-23, 2026-10-24)

The learned readers had never passed a release build: each earlier one stopped at a hermetic gate. The build of
`cb30447` reached the live product suite and failed it in two ways. `tests.test_datasets` ran past the suite's
30-minute limit (the four readers read each question in about 9 s on the gate's 4 vCPU, and the suite asks 88
questions in four execution modes), and the build past its 90 minutes. And live cases the readers had never been run
on failed: four in that build, and three more in the scale suite once the first ones were fixed. Each is fixed as a
family, but for two the owner accepted as known gaps (below):

- **Every column** (`tests.test_scale`, "show all rows ordered by id"): the readings could not hold `SELECT *`, so
  the 55 Spider and 2 BIRD training golds that ask for every column were unreadable and no reader learned it; the
  readers showed the id alone. The star shown plain is now every column (`engine/readers/readings.py`, the decoder,
  read as "every column of ..."), those golds are training data, and a written structure teaches it
  (`training/readers/structures.py` `listing`: every column of a table's rows, at most filtered and ordered; 1,124
  questions on training databases). Two of the four shipped readers have learned it, and the choice is not confident
  of it on a 30,000-row sheet (0.09): the question is asked about, read right ("every column of subscriptions, sorted
  by id lowest first"). A bundle of four readers trained on it answers it (0.17) but chooses right on 649 of 1,028
  calibration questions out of fold against 668, and the owner chose the more accurate bundle (2026-10-24).
- **Sheet language** (`tests.test_geo`, "Count all non-empty Order ID rows below the header in the Customers
  sheet"): every reader read a condition on the order id ("order id is 'non-empty'"), which counted 0 of 4 orders.
  The builder refuses a condition that can hold for no row as written: a number column compared with a text that is
  no number (a spelling of a missing number, `''` or `'null'`, still compares: Spider's golds use them) and a column
  compared with its own unfiltered, unranked values. A rewording family teaches the language
  (`training/readers/rewordings.py` `sheet`: 1,085 training count questions asked as a person looking at the sheet
  would).
- **Rankings that rank** (`tests.test_geo`, "top 3 cities"): the choice ranked first the cities by their number of
  rows, one each, and served three of four by name. A ranking whose order ranks none of the rows it keeps above the
  first one it leaves out (its first row ties with that one) is served only when every eligible ranking is
  (`engine/sql_rank.py`, `undecided`). A first version that refused any tie at the cut lost "the five oldest people"
  on calibration (the fifth and sixth ages tie in the data) and was narrowed.
- **A value the upload lacks** (`tests.test_geo`, "total amount in Europe by city"): the reading chosen dropped
  "Europe", so routing took the upload as answering the question whole and skipped compose; the delegate's world
  join still answered right, with no derivation shown. `engine/routing.py:reads_upload_whole` now also asks that no
  reading of the pool names a value the upload holds nowhere (`engine/sql_grounding.py:unheld_literals`), and compose
  decides as before.
- **Layout copies are read once** (`tests.test_scale`, "What is the total Amount broken down by Plan and
  Currency?" over three subscription exports and their three reports): read against all six tabs, every reading
  joined copies no key connects. The readers read one table of each group of layout copies, the one the question
  names or else the first (`engine/tables.py:one_of_each_layout`), and the answer names the others, as it already did
  (`layout_copies`). The breakdown itself stays unanswered: the readers still list the export's report beside it, and
  a rule that dropped a listed table no key reaches from the tables read let the readings build, after which the
  choice preferred "plan, currency" with no total at 0.31. That rule was left out (a confident wrong total is worse
  than none), and the question is declined, a known gap the owner accepted (2026-10-24). A preference for candidates
  that read every column the question names by its header, the old search's coverage, was measured and left out
  too: 653 of 1,028 out of fold against 668.
- **Two columns under one header are asked about** (`tests.test_scale`, "total Amount" on a sheet headed "id,
  Amount, Amount, Units"): the readers summed column B alone. A reading of one of two columns that share a header,
  when the question does not name its column letter, is not eligible (`engine/sql_grounding.py:shared_headers`), and
  when that leaves nothing the reply asks which column; "total Amount in column C" is answered.
- **Limits:** the product suite gives each suite 3,600 s (`deploy/gcp/run_product_suite.sh`) and the engine build
  10,800 s (`cloudbuild.yaml`, `cloudbuild.product.yaml`, `deploy/gcp/deploy.sh`).
- **An abstaining reader is no dissent** (`engine/readers/search.py:readers_share`, the same count in
  `training/readers/choice.py`): the readers' share of a candidate is counted among the readers that build any reading
  of the question. With two of four readers trained on sheet language, the other two built nothing for the sheet
  count, and the share counted their silence as disagreement (confidence 0.124 against the threshold). Out of fold
  on calibration the choice is right on 666 of 1,028 with it and 667 without, within noise; a result-agreement
  feature (668) and serving any answer every reading agrees on (2 of 9 such declined calibration questions right)
  were tried and left out.
- **Two readers retrained on the new data** (`training/readers/README.md` 2d, 4d): v13a1 and v13b1 replace v12a1
  and v12b1 in slots d and c (gold-pruned calibration, every reading right: 0.238 against 0.236, 0.203 against
  0.197); the choice refitted on the pool, threshold matched at 0.1405 (answered 955 of 1,053, 661 right, 294 wrong,
  the engine's 294). Weights: Hugging Face revision `999b50a9d7038c901998c0b586cbb8d4aa67851e`. Two more on the
  same data (v13a, v13b: seed 0, 0.211 and 0.200), trained with the owner's added budget, are not shipped: in place
  of v11a and v11b they lower the choice to 649 out of fold, and in place of one of them to 660 or 662.

**Measured** (`spider/results/RESULTS.md`): calibration 667 strict of 1,053 (669 for the bundle above: 19 wins, 21
losses, within noise; 301 wins and 52 losses against the engine), so this is not an accuracy gain on Spider; what
it changes is the four product failures. On Windows: `regress.run_regression --offline` 13 of 13,
`tests.test_complex_datasets` 10 of 10, `tests.test_geo` 73 of 73, `tests.test_scale` (each known gap asked about or declined, never answered wrong),
and the live demo-dataset gate 92 of 92. The sheet count is answered at confidence 0.163, 0.023 above the threshold.

**Known gaps** (regressions against the engine before the readers, accepted by the owner for this release):
"show all rows ordered by id" over a 30,000-row sheet is asked about rather than answered, and "What is the total
Amount broken down by Plan and Currency?" over the six-tab subscription workbook is declined; `tests.test_scale`
requires each to be answered right or not at all. "list the id and Amount of every subscription ordered by id" is
read as sorted by amount. "minimum amount" is still answered with the highest. The re-scorer, the oldest model in
the bundle, has never read "every column of ..." or sheet language; refreshing it needs cross-fold readers on the
new data. RunPod balance after this round: $37.51.
