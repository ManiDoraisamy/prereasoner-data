# Contributing

Thanks for improving Prereasoner. Begin with [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md), then read the
owner for the behavior you intend to change and its tests.
Participation is governed by the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).

## Development Principles

- Extend the existing production owner; do not add a competing planner, router, evaluator, endpoint, or model bundle.
- Keep the typed AST as the only own-data SQL representation.
- Keep routing in `engine.routing.route()` and FK discovery in `engine.relations.discover_fks()`.
- Preserve deterministic ordering and explicit tie-breakers.
- Treat execution success as validity, not proof of semantic correctness.
- Add a regression test that fails for the observed reason.
- Keep configuration in `engine/config.py` and `.env.example`; never commit hosts, credentials, tokens, or user data.
- Do not commit generated Spider checkpoints or model experiments as source changes.

The detailed machine-agent rules in [CLAUDE.md](CLAUDE.md) express the same ownership and validation discipline.

## Setup

Python 3.11 is the supported runtime and the version used by CI and both containers.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-ci.txt
Copy-Item .env.example .env
```

Install `requirements.txt` in addition when working on model-backed or live engine paths.

Use Docker Compose for PostgreSQL and the engine, or follow the native setup in
[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md). Runtime weights are described in
[engine/data/README.md](engine/data/README.md).

## Test Matrix

| Change | Required checks |
|---|---|
| Python-only utility | focused tests, Ruff fatal checks, and `python -m compileall -q engine db training tests orchestrator mcp_server regress` |
| Planner/search/ranker | `python -m tests.test_sql_ast` |
| Routing/compose | `python -m tests.test_routing` and `python -m tests.test_compose` |
| Saved references | `python -m tests.test_master_ingest` |
| Workbook frontend | `node --check web/public/lib/workbook.js` and `node web/tests/workbook_reference.test.js` |
| Repository-wide | `python -m tests.run_all` and `git diff --check` |
| Planner behavior measured on Spider | fresh serving-faithful `whole_db` evaluation with provenance |

Live suites require model artifacts and a seeded PostgreSQL database. A skipped suite is not a pass; state skips and
their missing prerequisites in the pull request.

## Pull Requests

Describe:

1. the failure or capability being addressed;
2. the production owner changed;
3. the behavioral invariant preserved;
4. exact test counts and any skips;
5. benchmark deltas when planner behavior changed;
6. migration or rollback steps for schema, model, or deployment changes.

Keep structural decisions in [DECISIONS.md](DECISIONS.md), architecture facts in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and measured SQL results in
[spider/results/RESULTS.md](spider/results/RESULTS.md).

## Security And Data

Authentication always derives the user from a verified Firebase token. Client-provided user ids must never select a
schema. Conversation ids require ownership checks. Reference table and column names must pass the validated quoting
path in `engine/master.py`.

Use synthetic fixtures in tests. Do not commit conversation snapshots, uploaded customer data, database dumps,
credentials, or production evaluation checkpoints containing sensitive content.
