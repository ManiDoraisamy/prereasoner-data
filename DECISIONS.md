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

## The database contract is now explicit

The world database schema had accreted across generations of setup scripts; no single file
described it. [db/init.sql](db/init.sql) is now the reconstructed, idempotent contract
(extensions, `words` + HNSW index, world tables), and [db/README.md](db/README.md) documents
what fills lazily from Wikidata at query time versus what must be pre-seeded. Notably, geo
queries are plain-SQL haversine — no PostGIS — so the stock `pgvector` image suffices.

## Reproduction honesty

`training/` reproduces the shipped model from the published checkpoints and documents precisely
which early-generation corpus artifacts would be needed for a true from-scratch retrain (they
are data artifacts, not code, and are stated as such rather than papered over).

## Fresh git history

The predecessor repos' histories contain hardcoded infrastructure IPs, committed virtualenvs,
and large binaries. This repo starts clean; the private repos remain as archives.

## License

Apache 2.0 — the patent grant matters for research code intended for broad reuse.

## The Schema.org named-property head supersedes the 9-family router

Two learned typers exist today. `engine/router.py` decodes an uploaded COLUMN to one of 9 families over a
71-coordinate Schema.org-property-named basis whose supervision instances come primarily from
Wikidata (Wikidata is not the vocabulary authority); `engine/schema_model.py` decodes a TABLE to
Schema.org classes over a URI-indexed basis compiled from Schema.org 30.0 and trained on all active
publisher releases. They share an architecture — consensus over calibrated per-property firing — and they
now share a granularity, because the corpus emits column-shaped instances rendered by the same
`summarize_table` serving function the interpreter uses.

The decision is that the head is the successor and the router is scheduled for retirement, for three
reasons that are properties of the design rather than of the current metrics:

* **One ontology, validated.** The router's basis was assembled without checking terms against Schema.org,
  so it carries dimensions that are not properties at all — `GeoCoordinates` is a class, `taxonName` is not
  a Schema.org term in any version — and dimensions whose supervision came from a hardcoded modal stamp
  over a corpus file that no longer exists in the repository. The head rejects both by construction.
* **One coordinate space.** Families are a closed 9-way partition; the head's coordinates are 1,521
  property URIs with inheritance, so a new domain extends the basis instead of needing a new family.
* **One evidence contract.** Coverage is explicit per class (servable / calibration-failed /
  observed-insufficient / representable-unobserved) rather than implied by a family's existence.

Retirement is gated on measurement, not on this decision: the head must match or beat the router on
column typing under the same held-out split before `engine/knowledge_query.py` routing migrates onto it.
Until that is measured and recorded in `spider/results/RESULTS.md`, the router remains the production owner
of world-join column typing and its `alloc.json` / `props_thr.json` / `families.json` bundle stays pinned.
Two owners is a documented, expiring state — not a permanent boundary.

**Removal condition:** the head matches or beats the router on per-column family typing. **Removal date:
2026-11-19.**

**Measured 2026-08-20 — the router stays. The head is not yet a candidate.** Both models were run over the
same six columns (three geo entity columns, one person column, two literals): router 5/6, head 2/6. The head
abstained on *every* entity column and was correct only on the two literals, where abstention is the right
answer anyway. The cause is structural rather than marginal: the router's entire production responsibility is
geo column typing for world joins, and its geo family spans City, Country, AdministrativeArea, Hospital,
School, CollegeOrUniversity, LandmarksOrHistoricalBuildings and Place — of which the head currently has
**zero** servable. Its six servable classes (ExchangeRateSpecification, UnitPriceSpecification, Movie,
Periodical, Product, Taxon) do not intersect that job at all.

What the measurement does show is that the head's *property* layer already reads these columns correctly —
`addressCountry` and `countryOfOrigin` fire on a country column, `foundingLocation` on a city column — and it
is only the class layer that abstains. So the unblocking condition is specific and testable: the geo classes
must clear the serving gates (held-out precision >= 0.90, recall >= 0.60), which today they cannot because
their signatures are built from properties whose Wikidata values are QID references the capped snapshot does
not contain. **That is a data problem, the same one that makes 28 legacy dims unreachable, and no amount of
recalibration fixes it.** Re-run the comparison after a Person/entity source lands; until then "two owners"
is the correct state, not a deferred cleanup.

Note the measurement also surfaced a router weakness worth keeping in view: it typed a column of person names
(`customer`) as `place` at 0.50. That is the documented design — the router proposes a family and *grounding*
disposes, so a name column that does not ground to real cities is dropped before any join — but it means the
router's raw family output should not be read as a standalone type judgement.
