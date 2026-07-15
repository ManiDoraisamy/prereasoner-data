# Engine tests

`python -m tests.test_sql_ast` is the hermetic SQL AST search/ranking suite and needs neither model weights nor
Postgres. The remaining engine suites below are live integration tests.

These are **live integration tests**: every suite talks to a real world Postgres (pgvector) with the
`world`/`wikipedia`/`public` schemas populated. The intended harness is the repo's docker-compose Postgres
plus the `db/sync` pipeline that mirrors the Wikidata world model into it. They are deliberately NOT unit
tests — they oracle-check the served answers against SQL recomputed from the same live tables.

## Prerequisites

1. Postgres with pgvector running and synced (docker-compose service + `db/sync`).
2. Environment (see `.env.example`):
   - `WORLD_PG_HOST` / `WORLD_PG_PORT` / `WORLD_PG_DB` / `WORLD_PG_USER` / `WORLD_PG_PASSWORD` (required — every
     suite skips or exits early without the password)
   - `AUTH_TEST_SUB` (optional) — the per-user schema the world tests write bridges into; each suite has its own
     default test schema.
   - `GEO_TEST_SUB` (optional) — schema for the geo suite (default `geotest`).
3. Model data in `engine/data` (see `engine/data/README.md`) — the router/world tests load the trained encoder.
4. `pip install -r requirements.txt` (+ the spaCy `en_core_web_md` model).

## Running

From the repo root:

```
python -m tests.test_world        # type hierarchy, router, aggregates, population, nearby
python -m tests.test_geo          # deep geo suite with SQL oracles + concurrency regression
python -m tests.test_nongeo      # non-geo world join + lazy Wikidata fill (needs outbound network)
python -m tests.test_world_joins  # join coverage per routable world table
python -m tests.test_route_wired  # trained model drives live routing end to end
```

Notes:

- First runs are slow on CPU (the LoRA Qwen + bge + spaCy load takes minutes) and the lazy Wikidata fill
  performs live WDQS/API calls.
- `tests.test_geo` intentionally builds a SECOND model instance to reproduce a concurrency deadlock scenario;
  expect double the load time.
