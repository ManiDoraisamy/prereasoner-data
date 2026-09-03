# Open-Source Release Guide

This guide distinguishes an Apache-2.0 **source release** from a reproducible Community Edition
runtime and from a hosted production deployment. See the [documentation map](README.md) for the
architecture and status vocabulary.

This repository can be published as Apache-2.0 source code. A public clone can run the
deterministic SQL planner, routing, enrichment-policy, source-parser, migration, MCP, and
frontend tests without downloading model weights or provisioning a production database.

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
| Runtime encoder/LoRA weights | Public, immutable bundle | Download anonymously with `python -m engine.fetch_weights`; see `engine/data/README.md` |
| Schema.org property head | Public in the same pinned bundle | `schema_property_head.pt` is required by the manifest and container startup gate |
| Schema.org semantic corpus JSONL | Generated, not committed | Rebuild from the exact synchronized releases with `python -m training.schema_org.corpus`; the committed semantic manifest preserves identity |
| Unified-router training inputs | Not committed | `columns.csv`, `type_table_map.csv`, databases, and warm-start inputs are documented in `training/props/pipeline.md` |
| Seeded Wikidata/source database | Not distributed | Build from publisher artifacts with `db/README.md` and `docs/SOURCE_DATA.md` |
| Spider dataset | Not bundled; licensed upstream under CC BY-SA 4.0 | Fetch from Yale with `spider/probe/fetch_data.py`; only aggregate measurements and attribution are committed |
| Production Terraform state and secrets | Never distributed | Create a new state backend, or restore/import an existing deployment before planning |
| Customer-held-out metadata | Never distributed without explicit consent | Keep consent-bound corpora under ignored `regress/private/` |

Publishing source plus the public manifested bundle reproduces the shipped model artifacts. It does
not reproduce the hosted application's seeded knowledge database, cloud state, or secrets. The public
source and deterministic non-model test path are reproducible without those deployment resources.

## What May Be Claimed

- **Source release:** the Apache-2.0 code can be published with the committed deterministic test
  path, source notices, and documented external-artifact boundary.
- **Reproducible runtime bundle:** yes. The public repository is pinned by commit and per-file hashes.
  Reproducing the historical training run is not yet established because the stable unified-router
  checkpoint lacks complete machine-readable corpus, split, seed, and metric provenance. See `MODEL_CARD.md`.
- **Production launch:** not established by a source release. It additionally requires the public model
  bundle in an immutable tested image, non-superuser database serving, protected state and backups,
  restricted browser keys, live auth/isolation tests, and a release-database regression run.

Community Edition is an intentional launch offer and must not be removed because its release
artifacts are still being prepared. Its current evidence gaps and the separately owned marketing
repository boundary are recorded in `docs/MARKETING_WEBSITE_REVIEW.md`. Before the offer is marked
generally available, retain the now-public manifested weight link and provide either a versioned
reference snapshot or a verified clean-build data path. Changes to this repository do not authorize edits or deployment
of the marketing website.

The source tree is designed for an open-source code release, but readiness belongs to one exact,
clean commit that has completed the checklist below. A dirty working tree or a prior deployment is
not release evidence. The hosted launch remains conditional.
External LLM calls are disabled unless the operator enables the deployment switch. Hosted operators
must publish an accurate privacy notice and establish an appropriate legal basis and processor terms.
The target Terraform configuration uses dedicated service accounts, a non-superuser serving role,
regional Cloud SQL with backups/PITR, immutable image digests, and RTDB cleanup when trace streaming
is enabled. Those are code-level guarantees only until a plan against the restored production state
is reviewed and applied. Remaining launch gates are external or deployment-specific: provision and
verify the public model bundle in the release image, verify browser-key
restrictions, run live auth/isolation tests, and record a seeded release-database regression.

Schema.org should be described as the semantic shell. Wikidata and publisher datasets supply
training observations and runtime facts under their own terms; Wikidata is not the primary ontology.
The current multi-source Schema.org head is the active ontology-validated class vocabulary and can
propose resolver families. The shared historical encoder was trained primarily from mapped Wikidata
observations and still supplies intent, ranking, and calculation signals. Do not merge their corpus
or metric claims, and do not describe a learned class proposal as authorization for a join: exact
source-key grounding remains mandatory.

## Release Checklist

1. Run `python -m ruff check engine db training tests orchestrator mcp_server regress --select F,E9`.
   Run `python -m bandit -q -r engine db training orchestrator mcp_server -x tests -lll`.
2. Run `python -m pip_audit` for `requirements-ci.txt`, `orchestrator/requirements.txt`,
   `requirements.txt`, `training/requirements.txt`, and `db/sync/requirements.txt`; then run the
   CI-equivalent public-checkout suite and frontend test shown above.
3. Run `python -m tests.run_all`; record every live/external skip separately.
4. Maintainer-only: run `python -m regress.run_regression --require-world` against the fully
   seeded release database. A public checkout without that database must record this gate as skipped.
5. For planner changes, run a fresh provenance-bearing Spider `whole_db` evaluation and compare per-example losses.
6. Run `terraform -chdir=infra fmt -check`, `terraform -chdir=infra init -backend=false`, and `terraform -chdir=infra validate`.
7. Build both containers. The chat image is optional; its build must pass the real MCP stdio handshake.
8. Run `gitleaks git --redact --log-opts='--all'` and scan tracked files/history for customer
   data, database dumps, model binaries, local state, and generated checkpoints. The configured
   exception is limited to documented public browser identifiers in `web/public/lib/config.js`.
9. Review `THIRD_PARTY.md`, source-specific licenses, `MODEL_CARD.md`, `DATA_CARD.md`,
   `SECURITY.md`, and `CITATION.cff`.
10. Tag only an exact tested commit. Keep benchmark outputs tied to that commit and artifact hashes.
11. Provision weights into an empty directory with `python -m engine.fetch_weights` and verify every
    external and committed artifact against `weights_manifest.json`.
12. Confirm Cloud Run serves through a non-superuser database role and deploy an image by digest,
    not the mutable `latest` tag. If RTDB is enabled, confirm the cleanup job is scheduled.
13. Leave `admin_emails` empty unless the admin dashboard is required; when enabled, verify the
    explicit Firebase email allowlist with both authorized and unauthorized accounts.

## Deployment Boundary

Terraform defaults to a core engine with external model processing disabled and no Anthropic
secret or IAM grant. Set `enable_external_llm=true` for engine-only assistant features, or set
`enable_orchestrator=true` and provide an immutable `chat_image`; either mode also requires the ID
of an out-of-band `anthropic_secret_id`. The secret value never belongs in Terraform variables or
state. Reference enrichment is
also independently opt-in through code approval, database grants, and
`ENRICHMENT_ACTIVE_DATASETS`.

Do not apply this repository's Terraform to an existing GCP project from an empty state.
Validation proves configuration shape, not ownership of live resources; restore the real
backend and import existing resources before any plan or apply.
