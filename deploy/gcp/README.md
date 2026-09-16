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
  `community-seed-v4.dump` artifact and applying the current application migrations/grants.

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

No Google credential or database password is sent to prereasoner.com. The database administrator
password remains in the caller's Secret Manager. The temporary bootstrap identity is granted access
to that secret and Cloud SQL only for the initialization job, then removed.

## Run Directly

Prerequisites are `gcloud`, Terraform 1.5 or newer, Python 3, Git, curl, a billing-enabled GCP
project, and sufficient IAM permissions.

```bash
gcloud auth login --update-adc
git clone --branch v0.2.23 --depth 1 https://github.com/ManiDoraisamy/prereasoner-data.git
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
service uses Vertex AI `gemini-3.8-flash`; Terraform enables the Vertex AI API and grants the chat
service account `roles/aiplatform.user`, so no provider key is collected or written to Secret Manager.
It keeps deletion protection on, activates only the reviewed `iana_country` enrichment dataset, and
restores the pinned `community-seed-v4.dump` after verifying its SHA-256. The deployment creates the
engine API, chat service, Firebase Hosting CDN release, and daily PostgreSQL conversation-retention job.

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
