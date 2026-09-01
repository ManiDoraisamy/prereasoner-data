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
- the canonical `cloudbuild.yaml`, including its offline regression gate; and
- `db.sync.community_bootstrap` in a short-lived Cloud Run Job, including the initial minimal
  Wikidata and ECB builds that later scheduled refreshes maintain.

No Google credential or database password is sent to prereasoner.com. The database administrator
password remains in the caller's Secret Manager. The temporary bootstrap identity is granted access
to that secret and Cloud SQL only for the initialization job, then removed.

## Run Directly

Prerequisites are `gcloud`, Terraform 1.5 or newer, Python 3, Git, curl, a billing-enabled GCP
project, and sufficient IAM permissions.

```bash
gcloud auth login --update-adc
git clone https://github.com/ManiDoraisamy/prereasoner-data.git
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

The Community profile uses Zonal Cloud SQL and `min_instances=0`. It keeps deletion protection on,
external LLM processing off, and enrichment off. The initial bootstrap loads the high-population
Wikidata serving projection and complete current ECB history. The deployment creates a functional engine API; the
included browser client remains a separate Firebase deployment because its Google OAuth identifiers
and authorized domains belong to each operator.

## State And Replays

The Terraform backend in `infra/versions.tf` is deliberately partial. `deploy.sh` supplies the caller's
bucket and `deployments/<name>` prefix at `terraform init`. This prevents a public checkout from ever
defaulting to the maintainer's production state.

The database bootstrap records its version and state in `knowledgebase.community_bootstrap`. Repeating
the bootstrap skips an already-ready version, concurrent runs serialize through a PostgreSQL advisory
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
