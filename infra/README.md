# infra/ — deploying PreReasoner to Google Cloud

Terraform creates the core Cloud Run engine (`prereasoner-api`), Cloud SQL Postgres,
Artifact Registry, secrets, and runtime identity. A separate third-party chat orchestrator
is optional (`enable_orchestrator=false` by default). Firebase Auth, Realtime Database, and
Hosting for `web/` are managed outside Terraform; see step 1.

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
- Cloud SQL defaults to regional high availability because it stores customer conversations
  and saved state in addition to rebuildable public reference data. Set
  `db_availability_type=ZONAL` only for disposable development environments.
- Cloud Run allows **unauthenticated invocations on purpose** — auth is application
  level (Firebase ID tokens verified by `engine/auth.py`), and the Hosting rewrite
  requires it.

## Prerequisites

- `gcloud` (authenticated: `gcloud auth login && gcloud config set project <PROJECT>`),
  Terraform >= 1.5 (CI uses 1.15.8 and the checked-in provider lock), and the `firebase`
  CLI (`npm i -g firebase-tools`) for the web step.
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

Terraform consumes an immutable image digest; it does not build. From the repo root:

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

**State comes first.** This configuration defaults to local state for disposable development.
Production state contains generated database credentials, so store it in a versioned,
access-restricted backend. Never run `apply` against an existing project from an empty state:
restore the production backend and import existing resources first. Otherwise Terraform will
propose duplicate Cloud Run, Cloud SQL, secret, IAM, and repository resources.

```bash
cd infra
terraform init
terraform apply -var project_id=<PROJECT> \
  -var image=<region>-docker.pkg.dev/<project>/<repo>/engine@sha256:<digest> \
  -var rtdb_url=https://<project>-default-rtdb.firebaseio.com \
  -var rtdb_trace_retention_days=7                         # omit rtdb_url to disable streaming
```

Outputs include `service_url`, `sql_connection_name`, `sql_public_ip`, `db_password_secret`,
and the serving-role secret. `chat_url` is null unless the optional orchestrator is enabled.

The service will deploy but return errors on `/api/reason`/`/api/knowledge` until the
database is seeded (next step). `/api/healthz` reports readiness once the models are loaded;
`/api/dimension` also requires the caller's Firebase ID token.

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

### 3a. Optional chat orchestrator

The engine does not require an Anthropic key. Terraform can create the optional chat service,
its dedicated service account, and least-privilege access to an **existing** Secret Manager
secret. The secret value is provisioned out of band and never enters Terraform configuration
or state.

```bash
gcloud secrets create prereasoner-chat-anthropic-key --replication-policy=automatic
printf '%s' "$ANTHROPIC_API_KEY" | \
  gcloud secrets versions add prereasoner-chat-anthropic-key --data-file=-
gcloud builds submit --config cloudbuild.orchestrator.yaml

# Resolve the pushed tag to an immutable digest, then use the same backend/state and
# engine variables as the core apply in step 2.
gcloud artifacts docker images describe \
  us-central1-docker.pkg.dev/<project>/prereasoner/chat:latest
terraform -chdir=infra apply \
  -var project_id=<PROJECT> \
  -var image=<engine-image@sha256:digest> \
  -var enable_orchestrator=true \
  -var chat_image=<chat-image@sha256:digest> \
  -var anthropic_secret_id=prereasoner-chat-anthropic-key
```

Enabling the module is the deployment-level external-LLM opt-in. The chat API still requires
an authenticated request containing literal `external_llm_consent: true`. Before exposing the
Hosting route, adopt and document an appropriate consent experience: the checked-in client uses
the notice-and-choice flow described in `PRIVACY.md`; deployments requiring ask-first consent must
adapt that client first.

The chat startup probe calls `/readyz`, which checks the injected key and imports the real MCP
server module. `/healthz` is liveness only and must not be used as the deployment readiness gate.

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
curl "$URL/api/healthz"      # {"ok": true, ...} once models are loaded
curl -X POST "$URL/api/dimension" -H 'Content-Type: application/json' \
     -H "Authorization: Bearer $FIREBASE_ID_TOKEN" \
     -d '{"data": "Paris", "mode": "analyze"}'
# /api/reason, /api/knowledge, and /api/dimension need a Firebase ID token.
```

## 6. Least-privilege serving + enrichment activation

The engine always serves as a dedicated **non-superuser** role (`serving` by default). There is
no Terraform option that gives the internet-facing service the `postgres` administration
credential. Reference enrichment remains off by default.

- `serving_db_role` — name of the mandatory non-superuser Cloud SQL role.
- `enrichment_active_datasets` — the deployment **allowlist** (2nd activation key; the 1st is
  per-dataset code approval in `engine/enrichment/registry.py`). Empty = enrichment off.

> CI and the release review run `terraform fmt -check` and `terraform validate` against the
> checked-in lock file. Run `terraform plan` against the correct restored state before every
> apply; validation alone cannot detect missing imports or verify live grants.
>
> Known benign diff: after any `gcloud run services update-traffic` (tags, pins), the Cloud Run
> API re-materializes an all-zero service-level `scaling {}` block and every plan shows
> `- scaling { manual_instance_count = 0 -> null ... }` for both services. Applying it is a
> no-op that creates no revision; the block returns on the next gcloud service mutation.

### 6a. Bootstrap the serving role

Serving is **not** read-only — it creates per-conversation and per-user `m_<hash>` master
schemas at request time (`engine/pg.py`, `engine/master.py`). So the role needs `CREATE` on the
database plus `SELECT` on the curated world data, and only **SELECT-only** on enrichment sources.
Apply in three steps to avoid a broken window (the role must be grantable *before* Cloud Run
points at it):

```bash
cd infra
# 1. Create the role + its secret WITHOUT repointing Cloud Run yet (-target skips the service).
terraform apply -var project_id=<PROJECT> -var image=<engine-image@sha256:digest> -var serving_db_role=serving \
  -target=google_sql_user.serving \
  -target=google_secret_manager_secret_version.serving_db_password

# 2. Run application migrations and bootstrap grants as the privileged postgres role (via
#    cloud-sql-proxy, as in step 3 above). The migration owns shared chat DDL and installs the
#    admin-owned SECURITY DEFINER lazy-fill functions (knowledgebase.lazy_ensure_table /
#    lazy_upsert_entity / lazy_register_word — engine/knowledge_sync.py's only write path).
#    The grants step gives serving chat DML plus EXECUTE on exactly those three functions and
#    audits that direct knowledgebase writes stay denied.
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
terraform apply -var project_id=<PROJECT> -var image=<engine-image@sha256:digest> -var serving_db_role=serving
```

The privileged `postgres` credential stays reserved for sync/migration/grants jobs (via
`SYNC_PG_*`, see `.env.example` and `db/sync/_conn.py); serving never receives it.

### 6b. Activate a reference dataset

Per-dataset, after 6a:

```bash
# Grant chat DML plus SELECT-only on the dataset's source tables + audit privileges.
SYNC_PG_USER=postgres SYNC_PG_PASSWORD=... python -m db.reference_grants \
  --role serving --datasets iana_country

# Add it to the deployment allowlist and redeploy.
cd infra && terraform apply -var project_id=<PROJECT> -var image=<engine-image@sha256:digest> -var serving_db_role=serving \
  -var enrichment_active_datasets=iana_country
```

### 6c. Rollback

- **Disable enrichment only** (fast, keeps the role): re-apply with
  `-var enrichment_active_datasets=""`. Answers revert to own-data + world immediately.
- There is no superuser-serving rollback. Roll back to the previous image while retaining the
  least-privilege role and grants.

## Teardown

```bash
cd infra && terraform destroy -var project_id=<PROJECT> -var image=<engine-image@sha256:digest> [-var rtdb_url=...]
```

Notes:
- `deletion_protection` defaults to `true` on Cloud SQL and Cloud Run. An intentional teardown
  requires first applying `-var deletion_protection=false`, reviewing that plan, and then
  running destroy.
- Cloud SQL reserves a deleted instance's **name for about a week** — re-applying
  immediately needs `-var sql_instance_name=<new-name>`.
- Enabled APIs stay enabled (`disable_on_destroy = false`).
- Firebase Hosting/Auth/RTDB are not Terraform-managed: disable them in the Firebase
  console if desired.

## Cost estimate (us-central1, list prices, rough)

| Component | Config | Est. monthly |
|---|---|---|
| Cloud SQL | `db-custom-1-3840` (1 vCPU / 3.75 GB), regional HA, 20 GB SSD | Dominant fixed cost; regional HA is roughly twice the equivalent zonal instance. Verify current pricing before apply |
| Cloud Run | 4 Gi / 2 vCPU, min 0 (scale to zero) | $0 idle; ~$0.21/active-hour (light demo use: a few $) |
| Artifact Registry | ~3–4 GB image | ~$0.40 |
| Secret Manager | 1 secret, few accesses | < $0.10 |
| Cloud Build | E2_HIGHCPU_8, ~20 min/build | ~$0.30 per build |
| Firebase Hosting/Auth/RTDB | demo traffic | $0 within free quotas (Blaze plan required for the rewrite, but usage-billed) |
| **Total** | | Database cost above plus usage-based Cloud Run, build, registry, Firebase, and secret charges |

Cloud SQL dominates; provider prices change, so use the Google Cloud pricing calculator for
the selected region and tier before launch. To pause spend between demo sessions:
`gcloud sql instances patch prereasoner-world --activation-policy=NEVER`
(and `--activation-policy=ALWAYS` to resume) — storage (~$3.40/mo) still bills.
`db-f1-micro` (~$9/mo shared-core) technically works for the minimal seed but is slow
for HNSW builds and disallowed for some pgvector workloads' memory spikes; `db_tier`
is a variable — downsize at your own risk.
