# infra — deployment infrastructure notes (open-source release)

What was built for the release: `Dockerfile` + `.dockerignore` (engine image),
`docker-compose.yml` (local db + engine + one-shot `seed` profile), `infra/` (Terraform:
ONE Cloud Run service + Cloud SQL + Artifact Registry + Secret Manager + runtime SA),
`cloudbuild.yaml` + `.gcloudignore` (image build path), `.github/workflows/ci.yml`
(cheap structural CI). Deploy walkthrough lives in `infra/README.md`; this note records
the *choices* and what could/couldn't be verified on this machine.

## Choices made (and why)

### Image (Dockerfile)

- **python:3.11-slim, two-stage.** Builder installs the pinned CPU stack into
  `/opt/venv`; runtime copies the venv + `engine/`. All wheels in requirements.txt are
  binary (torch-cpu, psycopg2-binary, spacy, en_core_web_md straight from the release
  wheel URL) ⇒ no gcc/apt layer at all. The two stages mainly drop pip leftovers; the
  image is dominated by torch+transformers+spacy (~2.5–3.5 GB — irreducible for this
  stack).
- **No `spacy download` step**: requirements.txt already pins the `en_core_web_md`
  release wheel; the builder still runs `spacy.load('en_core_web_md')` as a build-time
  assertion so a future requirements edit that drops the model fails the *build*, not
  the first request.
- **Missing-weights = start-time failure, not build-time.** `engine/data/*.pt`, `*.npz`,
  `qwen_lora/` are gitignored, so `COPY engine/` legitimately produces a weights-less
  image from a fresh clone (and must — CI, contributors without weights). A tiny
  `/app/entrypoint.sh` (written via Dockerfile heredoc, so no extra repo file) checks
  the five artifacts under `PREREASONER_DATA_DIR` (default `/app/engine/data`) and exits
  with the fix instructions (rebuild with weights, or mount + `PREREASONER_DATA_DIR`)
  before `python -m engine.server` ever runs.
- **`.dockerignore` ships engine + requirements only.** `db/` is excluded on purpose;
  the compose `seed` service bind-mounts `./db` instead (below), so seed-script edits
  never invalidate image layers.
- `HF_HOME=/tmp/hf`: the base Qwen2.5-0.5B tokenizer/model pulls from HF at first load;
  `/tmp` is the only guaranteed-writable path on Cloud Run's read-only rootfs.

### Local stack (docker-compose.yml)

- `db` = stock `pgvector/pgvector:pg16` (has `vector` + contrib `pg_trgm` — all
  `db/init.sql` needs, per docs/notes/db.md); `init.sql` mounted into
  `/docker-entrypoint-initdb.d` (applies on first start of an empty volume); pg_isready
  healthcheck gates the engine.
- `engine`: `AUTH_TEST_SUB=localdev` so reviewers hit `/api/reason` without Firebase;
  `RTDB_URL` unset (streaming no-ops); password `${KB_PG_PASSWORD:-devpassword}` so
  `docker compose up` works with zero config but respects `.env`.
- **Seeding**: a `seed` service under `profiles: ["seed"]` (excluded from `up`), reusing
  the engine image with `entrypoint: []` (seeding needs no model weights, so it must not
  trip the weights gate) and `./db` bind-mounted rw (sync_types refreshes its
  `p279_cache.json`). **Dependency reconciliation checked:** `db/sync/requirements.txt`
  = psycopg2-binary, numpy, torch, transformers (bge-small embedder) — all already in
  the engine image via requirements.txt; the scripts use script-local sibling imports
  (`from _conn import ...`) so `python db/sync/<script>.py` works as-is; `sync_wikidata
  --reset` applies `db/init.sql` itself via `import_dump.ensure_schema`, so seeding is
  proof against a volume that predates the init.sql mount. `HF_HOME` goes to a named
  volume so the ~130 MB bge download survives re-runs.

### GCP (infra/ Terraform, single service)

- **One Cloud Run v2 service** — the repo already consolidated the three legacy Cloud
  Run services into one engine; infra matches. Name/region default to
  `prereasoner-api`/`us-central1` because `web/firebase.json`'s `/api/**` rewrite
  hardcodes them (variables carry that warning).
- **Cloud SQL over unix socket, NOT a VPC connector.** The engine treats a
  `KB_PG_HOST` starting with `/` as a unix socket, so the Cloud Run `cloudsql` volume
  (`/cloudsql/<connection_name>`) is the zero-extra-cost path. The instance keeps a
  public IP with **zero authorized networks** — unreachable over TCP, connector/proxy
  (IAM-authenticated) only. A private-IP instance would add a VPC connector (~$10/mo)
  plus private-services-access setup for no security gain at this posture.
- **Sizing: 4Gi / 2 vCPU** — Qwen2.5-0.5B (fp32 CPU ~2 GB with LoRA + readout) + spaCy
  en_core_web_md + request-time tensors; 2Gi OOMs too easily, 4Gi is the sweet spot.
  `startup_cpu_boost` + an HTTP startup probe on `/healthz` (which reports ok only after
  models load) with a ~5 min budget covers the cold start. `min_instances = 0`: this is
  a demo; cold starts beat ~$60/mo of idle 4Gi. `max_instance_request_concurrency = 8`:
  the world/reason paths serialize on one in-process lock, so high concurrency only
  queues behind it.
- **Auth model:** Cloud Run allows `allUsers` invocation; authentication is application
  level (engine/auth.py verifies Firebase ID tokens; `/api/dimension` is public by
  design). The Hosting `/api/**` rewrite requires unauthenticated invoke anyway. The
  dedicated runtime SA gets exactly `roles/cloudsql.client`, secret accessor on the ONE
  db-password secret, and `roles/firebasedatabase.admin` (engine/trace.py streams to
  RTDB via ADC; also satisfies firebase-admin init for token verification).
- **Password path:** `random_password` → Secret Manager → Cloud Run `value_source`
  secret ref; never in state-adjacent plaintext env or tfvars. (It IS in the tf state
  file — local state; the versions.tf comment points at a GCS backend for teams.)
- **Build path is two-step by design:** `gcloud builds submit --config cloudbuild.yaml`
  (BuildKit forced on — the Dockerfile heredoc needs it), then `terraform apply` with
  the image variable (default = the `:latest` tag cloudbuild pushes). Terraform doesn't
  build images; Cloud Build has the weights.
- **`.gcloudignore` is load-bearing:** without it, `gcloud builds submit` derives its
  upload ignore list from `.gitignore`, which excludes the model weights — you'd
  silently ship an image that dies at startup (with a clear message, thanks to the
  entrypoint gate, but still). The file mirrors `.dockerignore` while keeping
  `engine/data/` in the upload.
- **Firebase left manual:** enabling Firebase on a project, the Auth provider, RTDB
  creation and `firebase deploy` for `web/` are console/CLI steps (documented in
  infra/README.md). Terraform's Firebase resources need the beta provider and still
  can't do the plan upgrade; not worth the surface for a one-time step.
- **DB seeding on GCP is documented, not automated:** Cloud Shell + cloud-sql-proxy +
  `db/sync/*` (README §3). It's a one-time, 15 min–hours, WDQS-rate-limited job — an
  interactive babysit, not a Terraform resource. A Cloud Run job variant is sketched in
  the README as an alternative.

### CI (.github/workflows/ci.yml)

Three jobs, all dependency-free: (1) `python -m compileall` over engine/db/training/
tests on 3.11; (2) `node --check` on `web/public/lib/*.js` + JSON validation of the
Firebase configs (`database.rules.json` is JSONC — Firebase allows `/* */` comments —
so the check strips block comments first; discovered when the strict parse failed
locally); (3) `terraform fmt -check` + `init -backend=false` + `validate` (no cloud
creds). **Deliberately NO docker build / pip install / tests in CI:** the weights are
gitignored (a CI image can never start), torch+transformers is a multi-GB install per
run, and tests/ needs a seeded live Postgres. The image is built where the weights
live (Cloud Build); CI verifies structure only.

## Deploy sequence (condensed; full version in infra/README.md)

1. Manual: add Firebase to the GCP project; enable Google sign-in; create RTDB (note
   URL); Blaze plan.
2. `gcloud builds submit --config cloudbuild.yaml` (from a working copy WITH weights).
3. `cd infra && terraform apply -var project_id=... [-var rtdb_url=...]`.
4. Seed: Cloud Shell → cloud-sql-proxy → `pip install -r db/sync/requirements.txt` →
   the five db/sync commands (README §3; password from Secret Manager).
5. `cd web && firebase deploy --only hosting,database`.
6. Smoke: `curl $URL/healthz`, `/api/dimension`.

Local: `docker compose up --build`, then once
`docker compose --profile seed run --rm seed`.

## Verified locally / NOT verified

| Check | Result |
|---|---|
| `terraform fmt -check` + `terraform init -backend=false` + `terraform validate` on infra/ | **PASS** (terraform 1.9.8 binary downloaded to scratchpad; terraform is not installed on this machine) |
| `python -m compileall engine db training tests` (the CI job, run locally) | PASS (local interpreter is 3.14; CI pins 3.11 — engine targets 3.11, so 3.14 compiling is a stricter-syntax proxy, not proof) |
| `node --check` on all 4 `web/public/lib/*.js`; firebase.json/database.rules.json parse (with JSONC comment strip) | PASS |
| YAML well-formedness of docker-compose.yml, cloudbuild.yaml, ci.yml | PASS (PyYAML safe_load) |
| db/sync deps ⊆ engine image; sibling-import run style; `--reset` applies init.sql | verified by reading db/sync/*.py + requirements files |
| **docker build / compose up** | **NOT verified — docker is not installed on this machine.** Dockerfile/compose were written to current stable syntax (heredoc COPY needs BuildKit; forced on in cloudbuild.yaml; Docker Desktop/CE ≥ 23 default it). First `docker compose up --build` on a docker machine is the remaining risk. |
| Cloud deploy (`gcloud builds submit`, `terraform apply` against a real project) | NOT run — needs a billed project; validate-only. |
| Image size / pip resolution of requirements.txt inside python:3.11-slim | NOT verified (no docker); pins match the repo's tested requirements.txt so risk is low. |

## Known risks / follow-ups

- **Unverified docker build** (above) — top of the list for anyone with docker: run
  `docker compose up --build`, then the seed profile, then `curl localhost:8080/healthz`
  and a `POST /api/reason` with `AUTH_TEST_SUB`'s implicit principal.
- Cloud Run startup probe budget (~5 min) is an estimate; if Qwen download + model load
  on a cold cache exceeds it, bump `failure_threshold` — or better, bake the HF cache
  into the image at build time (future optimization; also removes the runtime HF
  network dependency).
- `web/firebase.json` hardcodes serviceId/region; the Terraform variables warn about
  the coupling but nothing enforces it.
- The postgres password lands in local Terraform state (expected with local state;
  README/versions.tf point to a GCS backend for anything shared).
- Cloud SQL name reservation (~1 week after destroy) can surprise a quick
  destroy/re-apply cycle; `sql_instance_name` variable is the escape hatch.
