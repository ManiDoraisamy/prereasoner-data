# Guided Google Cloud Deployment

This directory owns the public Community Edition deployment entry point. The marketing website only
links here; it does not receive Google credentials, run Terraform, retain state, or proxy deployment
commands.

## What The Button Does

[`button.html`](button.html) opens the public repository and
[`cloudshell-tutorial.md`](cloudshell-tutorial.md) in a temporary Google Cloud Shell. Google requires
the user to authenticate that shell explicitly because this is a third-party repository. The tutorial
then runs [`deploy.sh`](deploy.sh).

The deployer uses:

- the caller's active Google authorization;
- one billing-enabled project selected by the caller;
- a private versioned bucket named `<project>-<deployment>-tfstate`;
- the canonical Terraform under `infra/`, with an isolated backend prefix;
- the public manifest-pinned model bundle;
- the canonical `cloudbuild.yaml` and `cloudbuild.orchestrator.yaml`, including their regression gates;
- the Cloud Build Firebase Hosting release for the canonical `web/public` source, which also
  provisions sign-in (see below); and
- `db.sync.community_seed_import` in a short-lived Cloud Run Job, restoring the versioned
  `community-seed-v4.dump` artifact and applying the current application migrations/grants; then one run
  of the deployment's ECB exchange-rate refresh job, because the seed's rates end a week after it was built
  and the daily refresh first runs at 16:30 UTC.

## Sign-in Is Provisioned, Not Delegated

The hosting release ([`hosting_release.js`](hosting_release.js)) enables Firebase **anonymous**
authentication and adds `<site>.web.app` and `<site>.firebaseapp.com` to the project's authorized
domains, merging with any domains already trusted. The operator configures nothing.

Google sign-in is deliberately not offered here, and that is a measured conclusion rather than a
preference. On a project with no prior Firebase configuration, enabling the `google.com` provider
returns `INVALID_CONFIG : client_id cannot be empty`; creating a Firebase Web app does not
auto-provision an OAuth client; and the only documented OAuth-client-creation API answers
`Project must belong to an organization`, so a personal account cannot use it at all. A
deployment-scoped Hosting domain is therefore never a registered redirect URI and no API can make it
one. Anonymous auth needs no OAuth client and still issues a real Firebase uid and a verifiable ID
token, so the engine's bearer-token check and per-user schema isolation are unchanged. The trade-off
is that identity is per-browser: conversations do not follow a user to another device.

`AUTH_PROVIDER` in the generated `web/public/lib/config.js` records which provider a deployment
actually has, so the reference deployment keeps Google sign-in without a second code path.

For an existing reference installation with Google sign-in already configured, publish Hosting with
`_AUTH_PROVIDER=google` and `_CUSTOM_DOMAINS=chat.prereasoner.com,prereasoner.com`, selecting the
site that owns the custom domain. This preserves its provider and merges its authorized domains;
it does not create a new OAuth client. Community installations keep the anonymous default.

No Google credential or database password is sent to prereasoner.com. The database administrator
password remains in the caller's Secret Manager. The temporary bootstrap identity is granted access
to that secret and Cloud SQL only for the initialization job, then removed.

## Run Directly

Prerequisites are `gcloud`, Terraform 1.5 or newer, Python 3, Git, curl, a billing-enabled GCP
project, and sufficient IAM permissions.

```bash
gcloud auth login --update-adc
git clone --branch v0.3.2 --depth 1 https://github.com/ManiDoraisamy/prereasoner-data.git
cd prereasoner-data
bash deploy/gcp/deploy.sh --project <PROJECT_ID>
```

Options:

```text
--region REGION     default us-central1
--name NAME         isolated resource/state prefix; default prereasoner
--skip-bootstrap    create infrastructure without loading the minimal world database
--destroy           review and remove this deployment
--yes               CI only, after an external plan/cost approval
```

The Community profile uses Zonal Cloud SQL `db-custom-2-7680` and `min_instances=0`. Its required chat
service and the engine (reference generation and the selection fallback) use Vertex AI
`gemini-3.8-flash` in this project; Terraform enables the Vertex AI API and grants both service accounts
`roles/aiplatform.user`, so no model key is collected or written to Secret Manager.
It keeps deletion protection on, activates only the reviewed `iana_country` enrichment dataset, and
restores the pinned `community-seed-v4.dump` after verifying its SHA-256. The deployment creates the
engine API, chat service, Firebase Hosting CDN release, and daily PostgreSQL conversation-retention job.
The seed is produced by `seed-export/`, a one-shot job that runs `pg_dump` of the `public`,
`knowledgebase`, and `iana` schemas of the reference world database and uploads the dump to
`SEED_BUCKET`; a new seed needs a new object name and a new pinned SHA-256 in `deploy.sh`.

The tier is pinned here rather than in `infra/variables.tf`, because the reference deployment takes
`db-g1-small` from that default and moving it would resize production. It is bought for one reason: a
dump carries index definitions, never contents, so the restore rebuilds a 623k-row, 384-dimension
pgvector HNSW index over ~957 MB of vectors. Measured 2026-09-16 on the real seed — 31 minutes on
`db-g1-small`, where the index cannot fit in memory and raising `maintenance_work_mem` fails outright
because pgvector builds HNSW in parallel and parallel builds allocate that memory in *shared* memory;
3.5 minutes on `db-custom-2-7680` with a serial 2 GB build. Switching the index to `ivfflat` would
build in 43 seconds but returned no row for about 1% of the filtered nearest-neighbour lookups
`engine/entities.py` performs, which would surface as silently unresolved entities. The whole seed
bootstrap measures about nine minutes; the remainder is transferring and copying 1.4 GB.

Before reporting success, the deployer runs the current application migrations, reads the required shared tables
as the non-superuser serving role, executes an exact-decimal calculation and a model-backed reasoning request in
the built image, checks service readiness, and verifies that an unauthenticated reasoning request is rejected.
The Hosting release is submitted by the same script after Cloud Run is configured; there is no separate
manual Firebase deployment step.

## State And Replays

### Hermetic Python suite (no deployment)

The model-backed hermetic suites run inside the digest-pinned CPU serving image with the same
frozen model bundle, plus the repository's hash-locked CI-only test dependencies. Build a clean,
allowlisted source archive first, then explicitly use its no-filter upload file; the normal
`.gcloudignore` is included for the source-boundary tests and must not be applied a second time:

```bash
python deploy/gcp/build_context.py --target suite --output /tmp/prereasoner-suite-context
cd /tmp/prereasoner-suite-context
gcloud builds submit --project <PROJECT_ID> \
  --ignore-file=cloudbuild.hermetic.ignore \
  --config=cloudbuild.hermetic.yaml .
```

This executes `tests.run_all` with the production image capped at 4 vCPU / 8 GiB. Live Postgres,
world-data, and external-orchestrator tests remain separate; this gate cannot be reported as live
product acceptance and does not deploy or change Cloud Run traffic.

### Live product suite against disposable seeded Postgres (no deployment)

The live-world acceptance gate uses a temporary Docker network and PostgreSQL 16/pgvector container
inside Cloud Build. It restores only the public, SHA-256-pinned Community seed, refreshes the public
ECB series for the current as-of date inside that disposable database, creates a temporary
non-superuser `serving` role, runs `regress.run_regression --require-world`, and runs all configured
engine/world/dataset tests with the **production image's unchanged hash-locked Python 3.11
environment**. It then runs the actual HTTP server under the production 4-vCPU/8-GiB container cap,
checks a customers+orders-to-France knowledgebase join, and records CPU-only request latency and
container RSS/CPU. The Cloud Build worker has enough host memory for the test containers; each
container remains capped at its tested serving limit. The PostgreSQL container, volume, network, and
test server are removed on both success and failure. This lane never connects to Cloud SQL,
including `prereasoner-world`, and creates no GCP database or service.

```bash
python deploy/gcp/build_context.py --target suite --output /tmp/prereasoner-product-context
cd /tmp/prereasoner-product-context
gcloud builds submit --project <PROJECT_ID> \
  --ignore-file=cloudbuild.hermetic.ignore \
  --config=cloudbuild.product.yaml .
```

Cloud Build time/cost is bounded by the 90-minute timeout; the test image and PostgreSQL are
container-local to that build. A green result is live product-dataset evidence against the public
seed, not a production-data soak or a Spider 80% result.

The Terraform backend in `infra/versions.tf` is deliberately partial. `deploy.sh` supplies the caller's
bucket and `deployments/<name>` prefix at `terraform init`. This prevents a public checkout from ever
defaulting to the maintainer's production state.

The seed import records its version and state in `knowledgebase.community_bootstrap`. Repeating
the import skips an already-ready version, concurrent runs serialize through a PostgreSQL advisory
lock, and failed runs retain an error state while the temporary cloud identity is still removed.

To remove the deployment:

```bash
bash deploy/gcp/deploy.sh --project <PROJECT_ID> --destroy
```

The versioned state bucket is intentionally retained after resource destruction for audit and recovery.
Delete it separately only after confirming no deployment state is needed.

## Scope

This is a guided infrastructure deployment, not anonymous execution. Google authentication, project
selection, IAM, billing, one cost confirmation, and organization-policy enforcement cannot be bypassed.
The marketing button must be described as **Deploy to Google Cloud**, not as a credential-free or
zero-cost installation.

## Launch promotion evidence

Freeze a clean commit with `build_context.py`. The engine build runs offline,
startup, complete hermetic and seeded live-product gates before pushing; the chat
build runs its lean contracts. Record both build IDs, tags and immutable digests,
the model fingerprint, migration version 11, current configuration, and retained
engine/chat/Hosting rollback targets in a release manifest. Run
`python deploy/gcp/release_gate.py <manifest.json>` before any service promotion.
Before the image change, apply migrations (`python -m db.sync.app_migrations`) and
`python -m db.reference_grants --role serving` as the admin, then run `engine.release_smoke`
using the new image and the serving identity. An engine image once served a database one chat
migration behind, and every chat question failed for ten hours (2026-10-04). With
`require_current_schema = true`, a revision whose database lacks a migration its code needs never
becomes ready, so traffic stays on the previous revision. The grants step gives the serving role read
access to the migration ledgers that check reads; run it before the first image that checks.

Build passes do not certify a launch. After compatible backend/Hosting/Apps Script
updates, record public version identity, all 18 `?load=` examples, ordered
conversational follow-ups, real Sheets, 30,000-row scale and history recovery in
hosted evidence. `release_gate.py --hosted-evidence <evidence.json>` rejects missing
or skipped lanes. Keep per-case observations and gold alongside that report.
