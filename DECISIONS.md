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
