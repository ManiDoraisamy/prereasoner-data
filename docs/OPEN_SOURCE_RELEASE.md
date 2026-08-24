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
| Schema.org property head | In the same private weight bundle | `schema_property_head.pt` is required by the manifest and container startup gate |
| Schema.org semantic corpus JSONL | Generated, not committed | Rebuild from the exact synchronized releases with `python -m training.schema_org.corpus`; the committed semantic manifest preserves identity |
| Unified-router training inputs | Not committed | `columns.csv`, `type_table_map.csv`, databases, and warm-start inputs are documented in `training/props/pipeline.md` |
| Seeded Wikidata/source database | Not distributed | Build from publisher artifacts with `db/README.md` and `docs/SOURCE_DATA.md` |
| Spider dataset | Upstream dataset is not intentionally bundled, but tracked per-example outputs currently reproduce questions and gold SQL | Resolve redistribution authorization before publishing, or remove per-example derived content with explicit approval and retain aggregate provenance |
| Production Terraform state and secrets | Never distributed | Create a new state backend, or restore/import an existing deployment before planning |
| Customer-held-out metadata | Never distributed without explicit consent | Keep consent-bound corpora under ignored `regress/private/` |

Publishing source alone therefore does not reproduce the hosted model-backed application.
The private weight bundle is the remaining external blocker to a fully self-contained public
deployment. The source and deterministic non-model test path are reproducible today.

## What May Be Claimed

- **Source release:** the Apache-2.0 code can be published with the committed deterministic test
  path, source notices, and documented external-artifact boundary.
- **Reproducible model release:** not yet. The default weight repository is private, and the stable
  unified-router checkpoint lacks complete machine-readable training-corpus, split, seed, and metric
  provenance. See `MODEL_CARD.md`.
- **Production launch:** not established by a source release. It additionally requires an immutable
  container image, non-superuser database serving, protected state and backups, restricted browser
  keys, live auth/isolation tests, and a release-database regression run.

Current launch status is **no-go**. External LLM calls are now disabled unless deployment opt-in and
per-request consent are both present, and Terraform no longer permits superuser serving. Remaining
release gates include a user-facing privacy/consent flow if LLM features are enabled, automatic
RTDB trace retention, resolution of Spider-derived redistribution, immutable dependency/image/base
model pins, externally verified browser-key restrictions, backups, and live isolation tests.

Schema.org should be described as the semantic shell. Wikidata and publisher datasets supply
training observations and runtime facts under their own terms; Wikidata is not the primary ontology.
The current multi-source Schema.org head is evidence-only, while the unified routing checkpoint was
trained primarily from mapped Wikidata observations. Do not merge those claims.

## Release Checklist

1. Run `python -m ruff check engine db training tests orchestrator mcp_server regress --select F,E9`.
2. Run the CI-equivalent public-checkout suite and frontend test shown above.
3. Run `python -m tests.run_all`; record every live/external skip separately.
4. Maintainer-only: run `python -m regress.run_regression --require-world` against the fully
   seeded release database. A public checkout without that database must record this gate as skipped.
5. For planner changes, run a fresh provenance-bearing Spider `whole_db` evaluation and compare per-example losses.
6. Run `terraform -chdir=infra fmt -check`, `terraform -chdir=infra init -backend=false`, and `terraform -chdir=infra validate`.
7. Build both containers. The chat image is optional; the engine must build and start without an Anthropic key.
8. Scan tracked files and history for credentials, customer data, database dumps, model binaries, local state, and generated checkpoints.
9. Review `THIRD_PARTY.md`, source-specific licenses, `MODEL_CARD.md`, `DATA_CARD.md`,
   `SECURITY.md`, and `CITATION.cff`.
10. Tag only an exact tested commit. Keep benchmark outputs tied to that commit and artifact hashes.
11. Provision weights into an empty directory with `python -m engine.fetch_weights` and verify every
    external and committed artifact against `weights_manifest.json`.
12. Confirm Cloud Run serves through a non-superuser database role and deploy an image by digest,
    not the mutable `latest` tag.

## Deployment Boundary

Terraform defaults to creating the core engine only. Set `enable_orchestrator=true` and
provide `anthropic_api_key` to create the optional chat resources. Reference enrichment is
also independently opt-in through code approval, database grants, and
`ENRICHMENT_ACTIVE_DATASETS`.

Do not apply this repository's Terraform to an existing GCP project from an empty state.
Validation proves configuration shape, not ownership of live resources; restore the real
backend and import existing resources before any plan or apply.
