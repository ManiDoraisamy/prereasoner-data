#!/usr/bin/env bash
# Guided, cost-aware Community Edition deployment into the caller's own GCP project.
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
readonly ZERO_DIGEST="$(printf '0%.0s' {1..64})"

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-}"
REGION="us-central1"
DEPLOYMENT="prereasoner"
YES=0
DESTROY=0
SKIP_BOOTSTRAP=0
BOOTSTRAP_JOB=""
BOOTSTRAP_SA=""
DB_SECRET=""
SMOKE_JOB=""
BUILD_CONTEXT=""
CHAT_BUILD_CONTEXT=""
HOSTING_BUILD_CONTEXT=""
CHAT_IMAGE=""
BUILD_SERVICE_ACCOUNT=""
FIREBASE_ADMIN_GRANTED=0
HOSTING_SITE=""
RELEASE_SUCCEEDED=0

usage() {
  cat <<'EOF'
Usage: deploy/gcp/deploy.sh [options]

  --project ID       Billing-enabled target GCP project (defaults to gcloud's project)
  --region REGION    Cloud Run, Cloud SQL, Artifact Registry region (default: us-central1)
  --name NAME        Deployment prefix, lowercase letters/digits/hyphens (default: prereasoner)
  --skip-bootstrap   Create infrastructure without importing the versioned Community seed
  --destroy          Destroy a deployment created by this script
  --yes              Non-interactive confirmation
  -h, --help         Show this help
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

while (($#)); do
  case "$1" in
    --project) PROJECT_ID="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --name) DEPLOYMENT="${2:-}"; shift 2 ;;
    --skip-bootstrap) SKIP_BOOTSTRAP=1; shift ;;
    --destroy) DESTROY=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

for command in gcloud terraform python3 curl git; do need "$command"; done

if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi
[[ "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "invalid or missing --project"
[[ "$REGION" =~ ^[a-z]+-[a-z]+[0-9]$ ]] || die "invalid --region"
[[ "$DEPLOYMENT" =~ ^[a-z][a-z0-9-]{1,19}$ ]] || die "--name must be 2-20 lowercase letters, digits, or hyphens"

readonly SERVICE_NAME="${DEPLOYMENT}-api"
readonly CHAT_SERVICE_NAME="${DEPLOYMENT}-chat"
readonly HOSTING_SITE_ID="${DEPLOYMENT}"
readonly SQL_INSTANCE="${DEPLOYMENT}-world"
readonly ARTIFACT_REPO="$DEPLOYMENT"
readonly STATE_BUCKET="${PROJECT_ID}-${DEPLOYMENT}-tfstate"
readonly STATE_PREFIX="deployments/${DEPLOYMENT}"
readonly TF_PLAN="${ROOT}/.terraform-${DEPLOYMENT}.tfplan"
COMMUNITY_SEED_URI="${COMMUNITY_SEED_URI:-https://storage.googleapis.com/prereasoner-community-artifacts/community-seed-v4.dump}"
COMMUNITY_SEED_SHA256="${COMMUNITY_SEED_SHA256:-2c39e749e2ae87654cca80881cdec4de924e131b1f8199179f6c7ceef2d8840a}"
# This script shares the infra/ root with whatever deployment the operator already manages, so it
# must NOT share infra/.terraform: `terraform init -reconfigure` rewrites that cache, and a later
# `cd infra && terraform apply` (infra/README.md §2) would then resolve the wrong backend and plan
# a duplicate stack against an unrelated state. A per-deployment data dir keeps the roots isolated.
export TF_DATA_DIR="${ROOT}/.terraform-${DEPLOYMENT}.data"

active_account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -n1)"
if [[ -z "$active_account" ]]; then
  cat >&2 <<'EOF'
No active Google credential is available. Google isolates third-party Open-in-Cloud-Shell
repositories from your account by design. Authenticate explicitly, then rerun this command:

  gcloud auth login --update-adc
EOF
  exit 2
fi

gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null \
  || die "the active account cannot access project $PROJECT_ID"
billing_enabled="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)"
[[ "$billing_enabled" == "True" || "$billing_enabled" == "true" ]] \
  || die "project $PROJECT_ID must have billing enabled and visible to the active account"
gcloud config set project "$PROJECT_ID" >/dev/null

confirm() {
  local word="$1" message="$2" answer
  if ((YES)); then return 0; fi
  [[ -t 0 ]] || die "interactive confirmation is required; pass --yes only after an external approval"
  printf '\n%s\n\nType %s to continue: ' "$message" "$word"
  read -r answer
  [[ "$answer" == "$word" ]] || die "cancelled"
}

submit_build() {
  # Cloud Build's permissions are eventually consistent with enabling its API. On a FIRST install
  # the API was turned on seconds earlier, and the first submission fails with PERMISSION_DENIED
  # before the grant takes effect -- the identical command succeeds minutes later untouched
  # (observed on a brand-new project, 2026-09-16). A re-install never sees it because the API is
  # already on, so this is another failure only a real new user would hit.
  #
  # Retry ONLY that denial. A build that actually ran and failed must surface immediately instead
  # of being run again, and `tee` keeps the live build log streaming while still letting the
  # failure be classified.
  local attempts=0 status log
  log="$(mktemp "${TMPDIR:-/tmp}/prereasoner-submit.XXXXXX")"
  while true; do
    status=0
    gcloud builds submit "$@" 2>&1 | tee "$log" || status=$?
    if [[ "$status" == 0 ]]; then
      rm -f "$log"
      return 0
    fi
    if ! grep -q 'PERMISSION_DENIED' "$log"; then
      rm -f "$log"
      die "Cloud Build failed"
    fi
    attempts=$((attempts + 1))
    if ((attempts >= 20)); then
      rm -f "$log"
      die "Cloud Build did not accept a submission within 5 minutes of enabling its API"
    fi
    printf '\nCloud Build permissions are still propagating; retrying in 15s (%d/20)\n' "$attempts"
    sleep 15
  done
}

cleanup_bootstrap_identity() {
  set +e
  if [[ -n "$BOOTSTRAP_JOB" ]]; then
    gcloud run jobs delete "$BOOTSTRAP_JOB" --project="$PROJECT_ID" --region="$REGION" --quiet >/dev/null 2>&1
  fi
  if [[ -n "$SMOKE_JOB" ]]; then
    gcloud run jobs delete "$SMOKE_JOB" --project="$PROJECT_ID" --region="$REGION" --quiet >/dev/null 2>&1
  fi
  if [[ -n "$BOOTSTRAP_SA" && -n "$DB_SECRET" ]]; then
    gcloud secrets remove-iam-policy-binding "$DB_SECRET" --project="$PROJECT_ID" \
      --member="serviceAccount:${BOOTSTRAP_SA}" --role=roles/secretmanager.secretAccessor --quiet >/dev/null 2>&1
    gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${BOOTSTRAP_SA}" --role=roles/cloudsql.client --condition=None --quiet >/dev/null 2>&1
    gcloud iam service-accounts delete "$BOOTSTRAP_SA" --project="$PROJECT_ID" --quiet >/dev/null 2>&1
  fi
  if [[ "$FIREBASE_ADMIN_GRANTED" == 1 && -n "$BUILD_SERVICE_ACCOUNT" ]]; then
    gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${BUILD_SERVICE_ACCOUNT}" \
      --role=roles/firebase.admin --condition=None --quiet >/dev/null 2>&1
  fi
}

cleanup_firebase_release() {
  # Firebase Hosting is intentionally outside Terraform because it is a CDN release. Remove only
  # this deployment's site; never touch the project's default site or a shared Web app.
  if [[ -n "$HOSTING_SITE" ]]; then
    token="$(gcloud auth print-access-token 2>/dev/null || true)"
    if [[ -n "$token" ]]; then
      curl --fail --silent --show-error --request DELETE \
        --header "Authorization: Bearer ${token}" \
        "https://firebasehosting.googleapis.com/v1beta1/projects/${PROJECT_ID}/sites/${HOSTING_SITE}" \
        >/dev/null 2>&1 || true
    fi
  fi
}

cleanup_release() {
  if [[ "$RELEASE_SUCCEEDED" != 1 ]]; then
    cleanup_firebase_release
  fi
  cleanup_bootstrap_identity
  for context in "$BUILD_CONTEXT" "$CHAT_BUILD_CONTEXT" "$HOSTING_BUILD_CONTEXT"; do
    case "$context" in
      "${TMPDIR:-/tmp}"/prereasoner-build.*) rm -rf -- "$context" ;;
    esac
  done
}

init_state() {
  if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${STATE_BUCKET}" \
      --project="$PROJECT_ID" \
      --location="$REGION" \
      --uniform-bucket-level-access \
      --public-access-prevention
  fi
  gcloud storage buckets update "gs://${STATE_BUCKET}" --versioning >/dev/null
  terraform -chdir="$ROOT/infra" init -reconfigure -input=false \
    -backend-config="bucket=${STATE_BUCKET}" \
    -backend-config="prefix=${STATE_PREFIX}"
}

# The Community tier is pinned HERE rather than changed in infra/variables.tf, because the
# reference deployment runs db-g1-small and takes that value from the default -- moving the
# default would try to resize production on its next apply.
#
# db-custom-2-7680 is bought for ONE reason, measured on the real seed (2026-09-16): restoring it
# rebuilds a 623k-row, 384-dimension HNSW index over ~957 MB of vectors. On db-g1-small (1.7 GB)
# that never fits and took 31 MINUTES; on 7.5 GB with a 2 GB private build allocation it takes
# 3.5. ivfflat would build in 43 seconds but returned NO ROW for ~1% of filtered
# nearest-neighbour lookups, which engine/entities.py depends on, so speed there would have cost
# silent entity-resolution failures instead of slow installs.
tf_vars() {
  local image="$1" protection="$2" chat_enabled="${3:-true}"
  printf '%s\n' \
    "-var=project_id=${PROJECT_ID}" \
    "-var=region=${REGION}" \
    "-var=service_name=${SERVICE_NAME}" \
    "-var=sql_instance_name=${SQL_INSTANCE}" \
    "-var=artifact_repo=${ARTIFACT_REPO}" \
    "-var=image=${image}" \
    "-var=db_tier=db-custom-2-7680" \
    "-var=db_availability_type=ZONAL" \
    "-var=min_instances=0" \
    "-var=deletion_protection=${protection}" \
    "-var=enable_external_llm=false" \
    "-var=enable_orchestrator=${chat_enabled}" \
    "-var=anthropic_secret_id=" \
    "-var=chat_llm_provider=gemini" \
    "-var=gemini_model=gemini-3.8-flash" \
    "-var=gemini_location=global" \
    "-var=community_seed_uri=${COMMUNITY_SEED_URI}" \
    "-var=community_seed_sha256=${COMMUNITY_SEED_SHA256}" \
    "-var=chat_service_name=${CHAT_SERVICE_NAME}" \
    "-var=chat_image=$([[ "$chat_enabled" == true ]] && printf '%s' "$CHAT_IMAGE" || true)" \
    "-var=enrichment_active_datasets=iana_country" \
    "-var=rtdb_url="
}

destroy_deployment() {
  init_state
  image="$(terraform -chdir="$ROOT/infra" output -raw image 2>/dev/null || true)"
  if [[ "$image" != *@sha256:* ]]; then
    # A destroy that stopped partway clears the root outputs, so a missing `image` output does
    # NOT mean "nothing to destroy". Refusing here is the worst possible answer: it tells the
    # operator the deployment is gone while the surviving Cloud SQL instance keeps billing
    # (observed 2026-09-16). Recover the digest from the running service, and if even that is
    # gone, destroy on whatever state remains -- the variable only has to satisfy the plan.
    image="$(gcloud run services describe "$SERVICE_NAME" --project="$PROJECT_ID" \
      --region="$REGION" --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  fi
  if [[ "$image" != *@sha256:* ]]; then
    [[ -n "$(terraform -chdir="$ROOT/infra" state list 2>/dev/null)" ]] \
      || die "no ${DEPLOYMENT} deployment exists in gs://${STATE_BUCKET}/${STATE_PREFIX}"
    image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/engine@sha256:${ZERO_DIGEST}"
  fi
  mapfile -t variables < <(tf_vars "$image" false false)
  terraform -chdir="$ROOT/infra" plan -input=false "${variables[@]}"
  confirm DESTROY "This removes ${SERVICE_NAME}, ${SQL_INSTANCE}, its databases, secrets, and images from ${PROJECT_ID}. The versioned Terraform state bucket is retained for audit."
  # Terraform reads deletion_protection from STATE, not from this run's variables, so passing
  # -var=deletion_protection=false to `destroy` alone leaves the guard armed and the uninstall
  # aborts with "cannot destroy service without setting deletion_protection=false", stranding a
  # billable Cloud SQL instance the operator was told they had removed (observed 2026-09-16).
  # Clear the flag with an apply that still describes the RUNNING deployment -- chat enabled, its
  # deployed digest -- so this step only lowers the guard and never changes what is deployed. The
  # digest comes from the live service rather than Terraform state, so an uninstall also works on
  # a deployment created by an earlier release.
  CHAT_IMAGE="$(gcloud run services describe "$CHAT_SERVICE_NAME" --project="$PROJECT_ID" \
    --region="$REGION" --format='value(spec.template.spec.containers[0].image)' 2>/dev/null || true)"
  if [[ "$CHAT_IMAGE" == *@sha256:* ]]; then
    mapfile -t unprotect < <(tf_vars "$image" false true)
  else
    mapfile -t unprotect < <(tf_vars "$image" false false)
  fi
  terraform -chdir="$ROOT/infra" apply -auto-approve -input=false "${unprotect[@]}"
  terraform -chdir="$ROOT/infra" destroy -auto-approve -input=false "${variables[@]}"
  HOSTING_SITE="$HOSTING_SITE_ID"
  cleanup_firebase_release
  printf '\nDeployment destroyed. State retained at gs://%s/%s\n' "$STATE_BUCKET" "$STATE_PREFIX"
}

if ((DESTROY)); then
  destroy_deployment
  exit 0
fi

[[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=all)" ]] \
  || die "deployment requires a clean checkout; commit or remove every local change first"

confirm DEPLOY "Prereasoner will create a ZONAL Cloud SQL instance, required Cloud Run engine and chat services, Firebase Hosting release, Secret Manager secrets, Cloud Builds, and a small versioned state bucket in ${PROJECT_ID}. These are billable resources. Cloud Run scales to zero; Cloud SQL is the main recurring cost."

if ((!SKIP_BOOTSTRAP)); then
  [[ "$COMMUNITY_SEED_URI" =~ ^https:// ]] || die "COMMUNITY_SEED_URI must be an HTTPS object URL"
  [[ "$COMMUNITY_SEED_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] \
    || die "COMMUNITY_SEED_SHA256 must be the 64-character SHA-256 of community-seed-v4.dump"
fi

# Register cleanup before creating state, build contexts, temporary jobs, or identities.
# Every failure from this point onward owns its rollback path.
trap cleanup_release EXIT

init_state

dummy_image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/engine@sha256:${ZERO_DIGEST}"
mapfile -t bootstrap_vars < <(tf_vars "$dummy_image" true false)
terraform -chdir="$ROOT/infra" apply -auto-approve -input=false \
  -target=google_project_service.apis \
  -target=google_artifact_registry_repository.engine \
  "${bootstrap_vars[@]}"

cache_dir="${HOME}/.cache/prereasoner-deploy"
venv="${cache_dir}/venv"
VENV_PYTHON="${venv}/bin/python"
if [[ ! -x "$VENV_PYTHON" && -x "${venv}/Scripts/python.exe" ]]; then
  VENV_PYTHON="${venv}/Scripts/python.exe"
fi
if [[ ! -x "$VENV_PYTHON" ]]; then
  python3 -m venv "$venv"
  if [[ -x "${venv}/bin/python" ]]; then
    VENV_PYTHON="${venv}/bin/python"
  else
    VENV_PYTHON="${venv}/Scripts/python.exe"
  fi
fi
[[ -x "$VENV_PYTHON" ]] || die "python virtual environment did not expose an executable"
"$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check \
  --require-hashes -r deploy/gcp/requirements.lock.txt
(
  cd "$ROOT"
  HF_TOKEN= HF_HUB_DISABLE_IMPLICIT_TOKEN=1 "$VENV_PYTHON" -m engine.fetch_weights
)

BUILD_CONTEXT="$(mktemp -d "${TMPDIR:-/tmp}/prereasoner-build.XXXXXX")"
(
  cd "$ROOT"
  "$VENV_PYTHON" deploy/gcp/build_context.py --output "$BUILD_CONTEXT"
)

build_service_account="$(gcloud builds get-default-service-account \
  --project="$PROJECT_ID" --format='value(serviceAccountEmail)')"
build_service_account="${build_service_account##*/}"
[[ "$build_service_account" == *@*.gserviceaccount.com ]] \
  || die "Cloud Build did not return its default service account"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${build_service_account}" \
  --role=roles/artifactregistry.writer --condition=None --quiet >/dev/null
BUILD_SERVICE_ACCOUNT="$build_service_account"

commit="$(git -C "$ROOT" rev-parse --short=12 HEAD)"
image_tag="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/engine:community-${commit}"
submit_build "$BUILD_CONTEXT" \
  --project="$PROJECT_ID" \
  --config="$BUILD_CONTEXT/cloudbuild.yaml" \
  --timeout=3600s \
  --substitutions="_REGION=${REGION},_REPO=${ARTIFACT_REPO},_TAG=community-${commit}"
digest="$(gcloud artifacts docker images describe "$image_tag" \
  --project="$PROJECT_ID" --format='value(image_summary.digest)')"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "could not resolve the built image digest"
image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/engine@${digest}"

CHAT_BUILD_CONTEXT="$(mktemp -d "${TMPDIR:-/tmp}/prereasoner-build.XXXXXX")"
(
  cd "$ROOT"
  "$VENV_PYTHON" deploy/gcp/build_context.py --target chat --output "$CHAT_BUILD_CONTEXT"
)
chat_tag="community-${commit}"
chat_image_tag="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/chat:${chat_tag}"
submit_build "$CHAT_BUILD_CONTEXT" \
  --project="$PROJECT_ID" \
  --config="$CHAT_BUILD_CONTEXT/cloudbuild.orchestrator.yaml" \
  --timeout=900s \
  --substitutions="_REGION=${REGION},_REPO=${ARTIFACT_REPO},_TAG=${chat_tag}"
chat_digest="$(gcloud artifacts docker images describe "$chat_image_tag" \
  --project="$PROJECT_ID" --format='value(image_summary.digest)')"
[[ "$chat_digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "could not resolve the built chat image digest"
CHAT_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/chat@${chat_digest}"

mapfile -t variables < <(tf_vars "$image" true true)
terraform -chdir="$ROOT/infra" plan -input=false -out="$TF_PLAN" "${variables[@]}"
terraform -chdir="$ROOT/infra" apply -auto-approve -input=false "$TF_PLAN"
rm -f "$TF_PLAN"

if ((!SKIP_BOOTSTRAP)); then
  connection="$(terraform -chdir="$ROOT/infra" output -raw sql_connection_name)"
  DB_SECRET="$(terraform -chdir="$ROOT/infra" output -raw db_password_secret)"
  serving_role="$(terraform -chdir="$ROOT/infra" output -raw serving_db_role)"
  BOOTSTRAP_JOB="${DEPLOYMENT}-bootstrap"
  bootstrap_account="${DEPLOYMENT}-bootstrap"
  BOOTSTRAP_SA="${bootstrap_account}@${PROJECT_ID}.iam.gserviceaccount.com"

  if ! gcloud iam service-accounts describe "$BOOTSTRAP_SA" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$bootstrap_account" --project="$PROJECT_ID" \
      --display-name="Temporary Prereasoner database bootstrap"
  fi
  # IAM is eventually consistent: `service-accounts create` returns before the new identity
  # resolves in the policy APIs, so binding a role on the next line fails with "Service account
  # ... does not exist" (observed 2026-09-16). Only a FIRST install is exposed — a re-run reuses
  # the existing account and never waits — which is exactly the path a new user takes. Retry the
  # binding itself rather than polling `describe`, because `describe` becoming visible does not
  # prove the policy API can resolve the member yet.
  grant_bootstrap_role() {
    local attempts=0
    until "$@" >/dev/null 2>&1; do
      attempts=$((attempts + 1))
      if ((attempts >= 30)); then
        "$@" >&2 || true   # surface the real error instead of a bare timeout
        die "could not grant ${BOOTSTRAP_SA} a required role within 60s"
      fi
      sleep 2
    done
  }
  grant_bootstrap_role gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${BOOTSTRAP_SA}" --role=roles/cloudsql.client --condition=None --quiet
  grant_bootstrap_role gcloud secrets add-iam-policy-binding "$DB_SECRET" --project="$PROJECT_ID" \
    --member="serviceAccount:${BOOTSTRAP_SA}" --role=roles/secretmanager.secretAccessor --quiet

  gcloud run jobs delete "$BOOTSTRAP_JOB" --project="$PROJECT_ID" --region="$REGION" --quiet >/dev/null 2>&1 || true
  gcloud run jobs create "$BOOTSTRAP_JOB" \
    --project="$PROJECT_ID" --region="$REGION" --image="$image" \
    --service-account="$BOOTSTRAP_SA" \
    --set-cloudsql-instances="$connection" \
    --set-secrets="SYNC_PG_PASSWORD=${DB_SECRET}:latest" \
    --set-env-vars="SYNC_PG_HOST=/cloudsql/${connection},SYNC_PG_DB=world,SYNC_PG_USER=postgres,COMMUNITY_SEED_URI=${COMMUNITY_SEED_URI},COMMUNITY_SEED_SHA256=${COMMUNITY_SEED_SHA256}" \
    --command=python --args=-m,db.sync.community_seed_import,--role,"$serving_role",--datasets,iana_country \
    --tasks=1 --max-retries=0 --task-timeout=7200s --cpu=4 --memory=8Gi
  gcloud run jobs execute "$BOOTSTRAP_JOB" --project="$PROJECT_ID" --region="$REGION" --wait

  runtime_sa="$(terraform -chdir="$ROOT/infra" output -raw runtime_service_account)"
  [[ "$runtime_sa" == *@*.gserviceaccount.com ]] || die "Terraform did not return the runtime service account"
  serving_secret="$(terraform -chdir="$ROOT/infra" output -raw serving_db_password_secret)"
  SMOKE_JOB="${DEPLOYMENT}-release-smoke"
  gcloud run jobs delete "$SMOKE_JOB" --project="$PROJECT_ID" --region="$REGION" --quiet >/dev/null 2>&1 || true
  gcloud run jobs create "$SMOKE_JOB" \
    --project="$PROJECT_ID" --region="$REGION" --image="$image" \
    --service-account="$runtime_sa" \
    --set-cloudsql-instances="$connection" \
    --set-env-vars="KB_PG_HOST=/cloudsql/${connection},KB_PG_DB=world,KB_PG_USER=${serving_role}" \
    --set-secrets="KB_PG_PASSWORD=${serving_secret}:latest" \
    --command=python --args=-m,engine.release_smoke \
    --tasks=1 --max-retries=0 --task-timeout=900s --cpu=4 --memory=16Gi
  gcloud run jobs execute "$SMOKE_JOB" --project="$PROJECT_ID" --region="$REGION" --wait
fi

HOSTING_BUILD_CONTEXT="$(mktemp -d "${TMPDIR:-/tmp}/prereasoner-build.XXXXXX")"
HOSTING_SITE="$HOSTING_SITE_ID"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SERVICE_ACCOUNT}" \
  --role=roles/firebase.admin --condition=None --quiet >/dev/null
FIREBASE_ADMIN_GRANTED=1
(
  cd "$ROOT"
  "$VENV_PYTHON" deploy/gcp/build_context.py --target hosting --output "$HOSTING_BUILD_CONTEXT"
)
submit_build "$HOSTING_BUILD_CONTEXT" \
  --project="$PROJECT_ID" \
  --config="$HOSTING_BUILD_CONTEXT/cloudbuild.hosting.yaml" \
  --timeout=900s \
  --substitutions="_REGION=${REGION},_API_SERVICE=${SERVICE_NAME},_CHAT_SERVICE=${CHAT_SERVICE_NAME},_HOSTING_SITE=${HOSTING_SITE}"

service_url="$(terraform -chdir="$ROOT/infra" output -raw service_url)"
printf '\nWaiting for the model-backed service to become ready...\n'
for _ in {1..60}; do
  if curl --fail --silent --show-error "${service_url}/api/healthz" | grep -q '"ok": true'; then
    auth_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
      --request POST --header 'Content-Type: application/json' \
      --data '{"data":"amount\\n1","question":"total amount"}' \
      "${service_url}/api/reason")"
    [[ "$auth_status" == "401" ]] || die "reasoning endpoint auth smoke returned HTTP ${auth_status}, expected 401"
    printf '\nPrereasoner Community Edition is ready.\nWeb:    https://%s.web.app/\nChat API: /chat\nEngine: %s\nState:  gs://%s/%s\n' \
      "$HOSTING_SITE" "$service_url" "$STATE_BUCKET" "$STATE_PREFIX"
    printf 'Firebase Hosting serves the static UI; /chat and /api/** are rewritten to the two Cloud Run services.\n'
    RELEASE_SUCCEEDED=1
    exit 0
  fi
  sleep 10
done
die "deployment completed, but ${service_url}/api/healthz did not become ready within 10 minutes"
