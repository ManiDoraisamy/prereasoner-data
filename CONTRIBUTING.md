# Contributing

Thanks for your interest in PreReasoner.

## Development setup

1. `cp .env.example .env` and fill in what you need (local dev needs almost nothing).
2. `docker compose up` — starts Postgres (pgvector, schema auto-applied from `db/init.sql`)
   and the engine on `http://localhost:8080` with `AUTH_TEST_SUB=localdev` so you can call
   `/api/reason` without Firebase.
3. Seed world data (one-time, ~15–45 min): see [db/README.md](db/README.md).
4. Frontend: see [web/README.md](web/README.md) (Firebase emulators).

## Tests

`tests/` are end-to-end suites that need the seeded Postgres from step 3:

```
pip install -r requirements.txt
python -m tests.test_geo        # etc. — see tests/README.md
```

CI runs compile/syntax checks on every PR; the live suites are run manually until a seeded
database is available in CI.

## Guidelines

- No version-numbered names (modules, classes, endpoints, artifacts) — names describe function.
- Configuration goes through `engine/config.py` and `.env.example`; never hardcode hosts, URLs,
  or credentials.
- `engine/auth.py` is security-critical: changes to it get extra scrutiny and must not weaken
  the verified-subject → schema derivation.
- Keep [DECISIONS.md](DECISIONS.md) current when you change something structural.
