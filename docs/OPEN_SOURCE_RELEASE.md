# Open-Source Release Guide

This repository can be published as Apache-2.0 source code. A public clone can run the
deterministic SQL planner, routing, enrichment-policy, source-parser, migration, MCP, and
frontend tests with no private model weights or production database.

## Public Checkout

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-ci.txt
$env:RUN_ENGINE_TESTS = "0"
$env:RUN_ORCHESTRATOR_TESTS = "0"
python -m tests.run_all
node web/tests/workbook_reference.test.js
```

CI runs the same Python boundary, Ruff fatal checks, frontend tests, and Terraform
format/validation. The typed planner does not need PyTorch at import time; model libraries
are loaded only by model-backed methods.

## External Artifacts

These are intentionally not source files:

| Artifact | Public status | Reproduction path |
|---|---|---|
| Runtime encoder/LoRA weights | Not public at this revision | Publish a manifest-pinned compatible bundle or set `PREREASONER_WEIGHTS_REPO`; see `engine/data/README.md` |
| Seeded Wikidata/source database | Not distributed | Build from publisher artifacts with `db/README.md` and `docs/SOURCE_DATA.md` |
| Spider dataset | Not distributed | Fetch under Spider's terms with `spider/probe/fetch_data.py` |
| Production Terraform state and secrets | Never distributed | Create a new state backend, or restore/import an existing deployment before planning |
| Customer-held-out metadata | Never distributed without explicit consent | Keep consent-bound corpora under ignored `regress/private/` |

Publishing source alone therefore does not reproduce the hosted model-backed application.
The private weight bundle is the remaining external blocker to a fully self-contained public
deployment. The source and deterministic non-model test path are reproducible today.

## Release Checklist

1. Run `python -m ruff check engine db training tests orchestrator mcp_server regress --select F,E9`.
2. Run the CI-equivalent public-checkout suite and frontend test shown above.
3. Run `python -m tests.run_all`; record every live/external skip separately.
4. Run `python -m regress.run_regression --require-world` against the release database.
5. For planner changes, run a fresh provenance-bearing Spider `whole_db` evaluation and compare per-example losses.
6. Run `terraform -chdir=infra fmt -check`, `terraform -chdir=infra init -backend=false`, and `terraform -chdir=infra validate`.
7. Build both containers. The chat image is optional; the engine must build and start without an Anthropic key.
8. Scan tracked files and history for credentials, customer data, database dumps, model binaries, local state, and generated checkpoints.
9. Review `THIRD_PARTY.md`, source-specific licenses, model notices, `SECURITY.md`, and `CITATION.cff`.
10. Tag only an exact tested commit. Keep benchmark outputs tied to that commit and artifact hashes.

## Deployment Boundary

Terraform defaults to creating the core engine only. Set `enable_orchestrator=true` and
provide `anthropic_api_key` to create the optional chat resources. Reference enrichment is
also independently opt-in through code approval, database grants, and
`ENRICHMENT_ACTIVE_DATASETS`.

Do not apply this repository's Terraform to an existing GCP project from an empty state.
Validation proves configuration shape, not ownership of live resources; restore the real
backend and import existing resources before any plan or apply.
