# infra/ — deploying PreReasoner to Google Cloud

Terraform for the **one** Cloud Run service (`prereasoner-api`, the consolidated engine)
plus its Cloud SQL Postgres, Artifact Registry repo, Secret Manager password and a
dedicated runtime service account. The Firebase pieces (Auth, Realtime Database,
Hosting for `web/`) are managed outside Terraform — see step 1.

[![Open in Cloud Shell](https://gstatic.com/cloudssh/images/open-btn.svg)](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=GITHUB_URL_PLACEHOLDER&cloudshell_tutorial=infra/README.md)

> `GITHUB_URL_PLACEHOLDER` — replace with the repo's public GitHub URL
> (e.g. `https://github.com/you/prereasoner`) once the repo is published.

## Architecture

```
browser ── Firebase Hosting (web/) ── /api/** rewrite ──> Cloud Run "prereasoner-api"
   │                                                        │ 4Gi / 2 vCPU, scale 0..3
   ├── Firebase Auth (ID tokens, verified in-app)           │ unix socket /cloudsql/...
   └── Firebase RTDB  (live trace stream, optional) <───────┤
                                            Cloud SQL Postgres 16 (pgvector) "world"
```

- Cloud SQL has a public IP with **zero authorized networks**: unreachable over plain
  TCP; all access is via the IAM-authenticated connector (Cloud Run's `/cloudsql`
  socket, `cloud-sql-proxy` for seeding). No VPC connector needed.
- Cloud Run allows **unauthenticated invocations on purpose** — auth is application
  level (Firebase ID tokens verified by `engine/auth.py`), and the Hosting rewrite
  requires it.

## Prerequisites

- `gcloud` (authenticated: `gcloud auth login && gcloud config set project <PROJECT>`),
  Terraform >= 1.5, and the `firebase` CLI (`npm i -g firebase-tools`) for the web step.
- A GCP project with billing enabled.
- **Manual Firebase step (Terraform does not do this):** add Firebase to the project at
  <https://console.firebase.google.com> ("Add project" → pick the existing GCP project).
  Then, in the Firebase console:
  1. **Authentication** → sign-in method → enable **Google**.
  2. **Realtime Database** → create a database (us-central1) → note its URL
     (`https://<project>-default-rtdb.firebaseio.com`) — this becomes the `rtdb_url`
     Terraform variable. Skippable: without it, trace streaming is disabled and the app
     still works (full-JSON responses).
  3. Hosting rewrites to Cloud Run require the **Blaze** (pay-as-you-go) plan.
- A full working copy **including the model weights** in `engine/data/` (`encoder.pt`,
  `encoder_meta.pt`, `anchor_assignment.npz`, `primitives.npz`, `qwen_lora/`). They are
  gitignored; a bare clone builds an image that exits at startup with instructions.
  See `engine/data/README.md`.

## Deploy (two-step: build, then apply)

### 1. Build + push the engine image

Terraform consumes an image tag; it does not build. From the repo root:

```bash
gcloud services enable cloudbuild.googleapis.com artifactregistry.googleapis.com
gcloud builds submit --config cloudbuild.yaml
```

(The very first submit may fail with "repository not found" if the Artifact Registry
repo doesn't exist yet — either run step 2 first and re-submit, or pre-create it:
`gcloud artifacts repositories create prereasoner --repository-format=docker
--location=us-central1`.)

The repo-root `.gcloudignore` keeps the gitignored weights in the upload — do not
delete it (without it, gcloud falls back to `.gitignore` and ships a weights-less
image).

### 2. Terraform apply

```bash
cd infra
terraform init
terraform apply -var project_id=<PROJECT> \
  -var rtdb_url=https://<project>-default-rtdb.firebaseio.com   # omit to disable streaming
```

Outputs: `service_url`, `sql_connection_name`, `sql_public_ip`, `db_password_secret`.

The service will deploy but return errors on `/api/reason`/`/api/knowledge` until the
database is seeded (next step) — `/healthz` and `/api/dimension` work immediately.

### 3. Seed the world database (one-time, ~15–45 min)

The engine resolves nothing until `knowledgebase."words"`, `knowledgebase."types"`, the friendly world
tables and `public.settlement` are populated (`db/README.md` §2). Run the seed from
**Cloud Shell** (or any machine) via `cloud-sql-proxy`:

```bash
# in Cloud Shell, repo checked out:
curl -Lo cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.14.2/cloud-sql-proxy.linux.amd64
chmod +x cloud-sql-proxy
./cloud-sql-proxy "$(cd infra && terraform output -raw sql_connection_name)" &

export KB_PG_HOST=127.0.0.1 KB_PG_PORT=5432 KB_PG_DB=world KB_PG_USER=postgres
export KB_PG_PASSWORD="$(gcloud secrets versions access latest --secret=prereasoner-api-db-password)"

pip install -r db/sync/requirements.txt        # torch-cpu + transformers, ~2 GB — fits Cloud Shell

python db/sync/sync_wikidata.py --reset --high-only   # schema (init.sql) + geo import
python db/sync/build_world.py
python db/sync/build_words.py --cities
python db/sync/sync_types.py
python db/sync/unify_words_qid.py                     # optional health check
```

Full sync variant (several hours) and per-type syncs: `db/README.md` §3.

*Alternative:* package the same commands as a Cloud Run job using the engine image
(it already contains every dependency) with `db/` baked in or fetched — not automated
here because the seed is a one-time, long-running, Wikidata-rate-limited task that is
easier to babysit interactively.

### 4. Deploy the web frontend

`web/firebase.json` already rewrites `/api/**` to the Cloud Run service
(`prereasoner-api`, `us-central1` — keep in sync with `service_name`/`region` vars):

```bash
cd web
firebase use <PROJECT>
firebase deploy --only hosting,database    # hosting + RTDB security rules
```

### 5. Smoke test

```bash
URL=$(cd infra && terraform output -raw service_url)
curl "$URL/healthz"          # {"ok": true, ...} once models are loaded
curl -X POST "$URL/api/dimension" -H 'Content-Type: application/json' \
     -d '{"data": "Paris", "mode": "analyze"}'
# /api/reason & /api/knowledge need a Firebase ID token — use the hosted web UI.
```

## 6. Least-privilege serving + enrichment activation (opt-in)

By default the engine serves as the `postgres` superuser and reference **enrichment is off**.
Two independent opt-ins harden this; both default to today's behavior, so leaving them unset is
a no-op.

- `serving_db_role` — run serving as a **non-superuser** Cloud SQL role.
- `enrichment_active_datasets` — the deployment **allowlist** (2nd activation key; the 1st is
  per-dataset code approval in `engine/enrichment/registry.py`). Empty = enrichment off.

> Terraform is not validated in this repo's dev environment. Run `terraform validate &&
> terraform plan` before every apply below, and note the grant bootstrap can only be verified
> against the live instance.

### 6a. Non-superuser serving role

Serving is **not** read-only — it creates per-conversation and per-user `m_<hash>` master
schemas at request time (`engine/pg.py`, `engine/master.py`). So the role needs `CREATE` on the
database plus `SELECT` on the curated world data, and only **SELECT-only** on enrichment sources.
Apply in three steps to avoid a broken window (the role must be grantable *before* Cloud Run
points at it):

```bash
cd infra
# 1. Create the role + its secret WITHOUT repointing Cloud Run yet (-target skips the service).
terraform apply -var project_id=<PROJECT> -var serving_db_role=serving \
  -target=google_sql_user.serving \
  -target=google_secret_manager_secret_version.serving_db_password

# 2. Run application migrations and bootstrap grants as the privileged postgres role (via
#    cloud-sql-proxy, as in step 3 above). The migration owns shared chat DDL; the serving role
#    receives chat DML only and never owns or alters those tables.
SYNC_PG_USER=postgres SYNC_PG_PASSWORD=... python -m db.sync.app_migrations
SYNC_PG_USER=postgres SYNC_PG_PASSWORD=... python -m db.reference_grants --role serving

#    The base world grants are still applied directly; verify the schema list against your
#    seeded DB (db/README.md) before running.
psql "$PROXY_CONN" <<'SQL'
  GRANT CONNECT, CREATE ON DATABASE world TO serving;      -- runtime conversation/master schemas
  GRANT USAGE ON SCHEMA knowledgebase, public TO serving;  -- curated world + public.settlement
  GRANT SELECT ON ALL TABLES IN SCHEMA knowledgebase, public TO serving;
  ALTER DEFAULT PRIVILEGES IN SCHEMA knowledgebase GRANT SELECT ON TABLES TO serving;
SQL

# 3. Now repoint Cloud Run at the (grant-ready) role.
terraform apply -var project_id=<PROJECT> -var serving_db_role=serving
```

The privileged `postgres` credential stays reserved for sync/migration/grants jobs (via
`SYNC_PG_*`, see `.env.example` and `db/sync/_conn.py`); serving never uses it once flipped.

### 6b. Activate a reference dataset

Per-dataset, after 6a:

```bash
# Grant chat DML plus SELECT-only on the dataset's source tables + audit privileges.
SYNC_PG_USER=postgres SYNC_PG_PASSWORD=... python -m db.reference_grants \
  --role serving --datasets iana_country

# Add it to the deployment allowlist and redeploy.
cd infra && terraform apply -var project_id=<PROJECT> -var serving_db_role=serving \
  -var enrichment_active_datasets=iana_country
```

### 6c. Rollback

- **Disable enrichment only** (fast, keeps the role): re-apply with
  `-var enrichment_active_datasets=""`. Answers revert to own-data + world immediately.
- **Back to superuser serving**: re-apply with `-var serving_db_role=""`. Cloud Run flips back to
  `postgres`; the serving secret is destroyed. Dropping the DB role itself can fail while it owns
  ephemeral conversation/master schemas — reassign or drop those first, or simply leave the role
  in place (unused) and rely on the enrichment-off rollback.

## Teardown

```bash
cd infra && terraform destroy -var project_id=<PROJECT> [-var rtdb_url=...]
```

Notes:
- `deletion_protection` is `false` on both Cloud SQL and Cloud Run so destroy is
  one-step. Flip it on in `main.tf` once you care about the seeded DB.
- Cloud SQL reserves a deleted instance's **name for about a week** — re-applying
  immediately needs `-var sql_instance_name=<new-name>`.
- Enabled APIs stay enabled (`disable_on_destroy = false`).
- Firebase Hosting/Auth/RTDB are not Terraform-managed: disable them in the Firebase
  console if desired.

## Cost estimate (us-central1, list prices, rough)

| Component | Config | Est. monthly |
|---|---|---|
| Cloud SQL | `db-custom-1-3840` (1 vCPU / 3.75 GB), zonal, 20 GB SSD | ~$52 + ~$3.40 disk |
| Cloud Run | 4 Gi / 2 vCPU, min 0 (scale to zero) | $0 idle; ~$0.21/active-hour (light demo use: a few $) |
| Artifact Registry | ~3–4 GB image | ~$0.40 |
| Secret Manager | 1 secret, few accesses | < $0.10 |
| Cloud Build | E2_HIGHCPU_8, ~20 min/build | ~$0.30 per build |
| Firebase Hosting/Auth/RTDB | demo traffic | $0 within free quotas (Blaze plan required for the rewrite, but usage-billed) |
| **Total** | | **~$56/mo steady + pennies per use** |

Cloud SQL dominates. To pause spend between demo sessions:
`gcloud sql instances patch prereasoner-world --activation-policy=NEVER`
(and `--activation-policy=ALWAYS` to resume) — storage (~$3.40/mo) still bills.
`db-f1-micro` (~$9/mo shared-core) technically works for the minimal seed but is slow
for HNSW builds and disallowed for some pgvector workloads' memory spikes; `db_tier`
is a variable — downsize at your own risk.
