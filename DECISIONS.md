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

## The IR and its emitters are separate owners (2026-09-09)

`engine/sql_ast.py` had two jobs: define and validate the typed tree, and render it to SQL text.
Those are different responsibilities, and the file's own `dialect` parameter already proved it —
`standard`, `sqlite_decimal`, and `postgres_numeric` are three lowerings of one tree. Rendering now
lives in `deterministic/emitter/sql/render.py`, and the composition view builders (formerly
`engine/primitives.py`, the second SQL producer) in `deterministic/emitter/sql/views.py`.
`engine/sql_ast.py` keeps the node grammar, typing, and validation, and must never import
`deterministic` — the IR stays the single definition of meaning that every target renders.
`_validate_expr` and `_expr_tables` were promoted to `validate_expression` and `expression_tables`
so validation could stay with the IR while only rendering moved.

The move was pure relocation, gated on byte-identical output: the same 44 planner candidates over a
fixed corpus hashed to `d6b85dab…` before and after, and `tests/test_routing.py`'s cross-process
repeatability test still passes. Two packaging facts had to follow the code — `Dockerfile` COPYs
`deterministic/` (the engine image previously shipped only `engine/`, `regress/`, `db/`, so serving
would have failed at import), and `spider/probe/full_eval.py` fingerprints the emitter, or a
rendering change would no longer invalidate a `--resume` checkpoint.

## A Python emitter as a differential oracle, CI-only (2026-09-09)

Determinism guarantees the same question yields the same SQL. It does not guarantee that SQL is
correct, and nothing in the suite recomputed the relational algebra independently — the hermetic
tests assert pinned SQL strings and hand-written expected rows, both authored alongside the planner
they check. `deterministic/emitter/py/` is a second lowering of the same IR: it hydrates uploaded
rows into objects and evaluates the tree over them, and CI requires it to agree with SQL row for row.

Three constraints keep it honest:

- **It never serves.** No serving module imports it, so `docs/SHEETS_AS_REASONING.md` rules 1 and 8
  are untouched: the workbook still shows executed SQL. This also means there is no dev/production
  path split to diverge — the emitted `.py` under `deterministic/_gen/` is a debug artifact written
  only under `PR_EMIT_PY_DEBUG=1`, never in an environment holding real tenant data (it embeds
  question literals and uploaded column names).
- **It must not re-ask the database.** SQLAlchemy may issue only `SELECT * FROM "<table>"`, once per
  table; joins, filters, grouping and ordering happen in Python over materialized rows, with
  `lazy="raise"` on every relationship. A statement listener fails the suite on any other statement.
  Without this the oracle would be SQL checking SQL.
- **It must be written from AST semantics, not transliterated** from `render.py`, or it reproduces
  the renderer's bugs and proves nothing.

SQLAlchemy is therefore a *test* dependency (`requirements-ci.txt` only, never `requirements.txt`);
the serving image is unchanged. Arithmetic is not re-implemented: the evaluator reuses
`engine/numeric.py`, the same exact-decimal code `register_sqlite_decimal` installs into SQLite, so
the two emitters agree on arithmetic by construction and the oracle tests relational algebra.

**Removal condition.** The differential suite is permanent test infrastructure. The generated-class
layer (`emitter/py/classes.py`, and with it the SQLAlchemy dependency) is the speculative part: if it
has surfaced no defect the evaluator alone would have missed by 2026-12-31, delete the class layer
and hydrate into plain dataclasses instead.
