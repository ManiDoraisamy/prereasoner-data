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

usage() {
  cat <<'EOF'
Usage: deploy/gcp/deploy.sh [options]

  --project ID       Billing-enabled target GCP project (defaults to gcloud's project)
  --region REGION    Cloud Run, Cloud SQL, Artifact Registry region (default: us-central1)
  --name NAME        Deployment prefix, lowercase letters/digits/hyphens (default: prereasoner)
  --skip-bootstrap   Create infrastructure without loading the minimal world database
  --destroy          Destroy a deployment created by this script
  --yes              Non-interactive confirmation; use only after an external plan approval
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
readonly SQL_INSTANCE="${DEPLOYMENT}-world"
readonly ARTIFACT_REPO="$DEPLOYMENT"
readonly STATE_BUCKET="${PROJECT_ID}-${DEPLOYMENT}-tfstate"
readonly STATE_PREFIX="deployments/${DEPLOYMENT}"
readonly TF_PLAN="${ROOT}/.terraform-${DEPLOYMENT}.tfplan"
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
}

cleanup_release() {
  cleanup_bootstrap_identity
  case "$BUILD_CONTEXT" in
    "${TMPDIR:-/tmp}"/prereasoner-build.*) rm -rf -- "$BUILD_CONTEXT" ;;
  esac
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

tf_vars() {
  local image="$1" protection="$2"
  printf '%s\n' \
    "-var=project_id=${PROJECT_ID}" \
    "-var=region=${REGION}" \
    "-var=service_name=${SERVICE_NAME}" \
    "-var=sql_instance_name=${SQL_INSTANCE}" \
    "-var=artifact_repo=${ARTIFACT_REPO}" \
    "-var=image=${image}" \
    "-var=db_availability_type=ZONAL" \
    "-var=min_instances=0" \
    "-var=deletion_protection=${protection}" \
    "-var=enable_external_llm=false" \
    "-var=enrichment_active_datasets=iana_country" \
    "-var=rtdb_url="
}

destroy_deployment() {
  init_state
  image="$(terraform -chdir="$ROOT/infra" output -raw image 2>/dev/null || true)"
  [[ "$image" == *@sha256:* ]] || die "no ${DEPLOYMENT} deployment exists in gs://${STATE_BUCKET}/${STATE_PREFIX}"
  mapfile -t variables < <(tf_vars "$image" false)
  terraform -chdir="$ROOT/infra" plan -input=false "${variables[@]}"
  confirm DESTROY "This removes ${SERVICE_NAME}, ${SQL_INSTANCE}, its databases, secrets, and images from ${PROJECT_ID}. The versioned Terraform state bucket is retained for audit."
  terraform -chdir="$ROOT/infra" apply -auto-approve -input=false "${variables[@]}"
  terraform -chdir="$ROOT/infra" destroy -auto-approve -input=false "${variables[@]}"
  printf '\nDeployment destroyed. State retained at gs://%s/%s\n' "$STATE_BUCKET" "$STATE_PREFIX"
}

if ((DESTROY)); then
  destroy_deployment
  exit 0
fi

[[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=all)" ]] \
  || die "deployment requires a clean checkout; commit or remove every local change first"

confirm DEPLOY "PreReasoner will create a ZONAL Cloud SQL instance, Artifact Registry, Cloud Run service, Secret Manager secrets, a Cloud Build, and a small versioned state bucket in ${PROJECT_ID}. These are billable resources. Cloud Run scales to zero; Cloud SQL is the main recurring cost."

# Register cleanup before creating state, build contexts, temporary jobs, or identities.
# Every failure from this point onward owns its rollback path.
trap cleanup_release EXIT

init_state

dummy_image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/engine@sha256:${ZERO_DIGEST}"
mapfile -t bootstrap_vars < <(tf_vars "$dummy_image" true)
terraform -chdir="$ROOT/infra" apply -auto-approve -input=false \
  -target=google_project_service.apis \
  -target=google_artifact_registry_repository.engine \
  "${bootstrap_vars[@]}"

cache_dir="${HOME}/.cache/prereasoner-deploy"
venv="${cache_dir}/venv"
if [[ ! -x "${venv}/bin/python" ]]; then
  python3 -m venv "$venv"
  "${venv}/bin/python" -m pip install --quiet --disable-pip-version-check huggingface_hub==1.29.0
fi
(
  cd "$ROOT"
  HF_TOKEN= HF_HUB_DISABLE_IMPLICIT_TOKEN=1 "${venv}/bin/python" -m engine.fetch_weights
)

BUILD_CONTEXT="$(mktemp -d "${TMPDIR:-/tmp}/prereasoner-build.XXXXXX")"
(
  cd "$ROOT"
  "${venv}/bin/python" deploy/gcp/build_context.py --output "$BUILD_CONTEXT"
)

build_service_account="$(gcloud builds get-default-service-account \
  --project="$PROJECT_ID" --format='value(serviceAccountEmail)')"
build_service_account="${build_service_account##*/}"
[[ "$build_service_account" == *@*.gserviceaccount.com ]] \
  || die "Cloud Build did not return its default service account"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${build_service_account}" \
  --role=roles/artifactregistry.writer --condition=None --quiet >/dev/null

commit="$(git -C "$ROOT" rev-parse --short=12 HEAD)"
image_tag="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/engine:community-${commit}"
gcloud builds submit "$BUILD_CONTEXT" \
  --project="$PROJECT_ID" \
  --config="$BUILD_CONTEXT/cloudbuild.yaml" \
  --timeout=3600s \
  --substitutions="_REGION=${REGION},_REPO=${ARTIFACT_REPO},_TAG=community-${commit}"
digest="$(gcloud artifacts docker images describe "$image_tag" \
  --project="$PROJECT_ID" --format='value(image_summary.digest)')"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "could not resolve the built image digest"
image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/engine@${digest}"

mapfile -t variables < <(tf_vars "$image" true)
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
      --display-name="Temporary PreReasoner database bootstrap"
  fi
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${BOOTSTRAP_SA}" --role=roles/cloudsql.client --condition=None --quiet >/dev/null
  gcloud secrets add-iam-policy-binding "$DB_SECRET" --project="$PROJECT_ID" \
    --member="serviceAccount:${BOOTSTRAP_SA}" --role=roles/secretmanager.secretAccessor --quiet >/dev/null

  gcloud run jobs delete "$BOOTSTRAP_JOB" --project="$PROJECT_ID" --region="$REGION" --quiet >/dev/null 2>&1 || true
  gcloud run jobs create "$BOOTSTRAP_JOB" \
    --project="$PROJECT_ID" --region="$REGION" --image="$image" \
    --service-account="$BOOTSTRAP_SA" \
    --set-cloudsql-instances="$connection" \
    --set-env-vars="SYNC_PG_HOST=/cloudsql/${connection},SYNC_PG_DB=world,SYNC_PG_USER=postgres" \
    --set-secrets="SYNC_PG_PASSWORD=${DB_SECRET}:latest" \
    --command=python --args=-m,db.sync.community_bootstrap,--role,"$serving_role",--datasets,iana_country \
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
    --tasks=1 --max-retries=0 --task-timeout=900s --cpu=4 --memory=8Gi
  gcloud run jobs execute "$SMOKE_JOB" --project="$PROJECT_ID" --region="$REGION" --wait
fi

service_url="$(terraform -chdir="$ROOT/infra" output -raw service_url)"
printf '\nWaiting for the model-backed service to become ready...\n'
for _ in {1..60}; do
  if curl --fail --silent --show-error "${service_url}/api/healthz" | grep -q '"ok": true'; then
    auth_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
      --request POST --header 'Content-Type: application/json' \
      --data '{"data":"amount\\n1","question":"total amount"}' \
      "${service_url}/api/reason")"
    [[ "$auth_status" == "401" ]] || die "reasoning endpoint auth smoke returned HTTP ${auth_status}, expected 401"
    printf '\nPreReasoner Community Edition is ready.\nEngine: %s\nState:  gs://%s/%s\n' \
      "$service_url" "$STATE_BUCKET" "$STATE_PREFIX"
    printf 'The engine API requires a Firebase ID token. Follow web/README.md to attach your own Firebase web client.\n'
    exit 0
  fi
  sleep 10
done
die "deployment completed, but ${service_url}/api/healthz did not become ready within 10 minutes"
