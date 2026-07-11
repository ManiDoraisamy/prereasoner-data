# The Sonnet orchestrator chat backend (prereasoner-chat) — mcp-now.md §7.
#
# A SECOND, lightweight Cloud Run service alongside the engine (google_cloud_run_v2_service.api in
# main.tf). It calls the engine over HTTP and Anthropic over HTTPS; it does NOT touch Postgres or write
# RTDB (the engine does that when it receives the forwarded Firebase token + jobId). So its SA needs only
# the Anthropic-key secret — no Cloud SQL, no RTDB roles.
#
# Deploy order (see infra/README.md):
#   1. gcloud builds submit --config cloudbuild.orchestrator.yaml   # builds+pushes the chat image (tests-gated)
#   2. terraform apply -var="anthropic_api_key=$(...)"              # this file + main.tf
#   3. cd web && firebase deploy --only hosting,database            # ships chat.html + the /chat rewrite

variable "anthropic_api_key" {
  description = "Anthropic API key for the Sonnet orchestrator. REQUIRED (no default). Pass at apply time, e.g. -var=\"anthropic_api_key=$(grep ^ANTHROPIC_API_KEY .env | cut -d= -f2)\"."
  type        = string
  sensitive   = true
}

variable "chat_service_name" {
  description = "Cloud Run service name for the orchestrator. web/firebase.json rewrites /chat to this serviceId, so change both together."
  type        = string
  default     = "prereasoner-chat"
}

variable "chat_image" {
  description = "Full image ref for the orchestrator (built by cloudbuild.orchestrator.yaml). Empty = the default tag: <region>-docker.pkg.dev/<project>/<repo>/chat:latest."
  type        = string
  default     = ""
}

variable "anthropic_model" {
  description = "Sonnet model id the orchestrator uses."
  type        = string
  default     = "claude-sonnet-5"
}

locals {
  chat_image = var.chat_image != "" ? var.chat_image : "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_repo}/chat:latest"
}

# ---------- Secret Manager: the Anthropic key ----------
resource "google_secret_manager_secret" "anthropic_key" {
  secret_id = "${var.chat_service_name}-anthropic-key"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "anthropic_key" {
  secret      = google_secret_manager_secret.anthropic_key.id
  secret_data = var.anthropic_api_key
}

# ---------- Service account for the orchestrator ----------
resource "google_service_account" "chat_run" {
  account_id   = "${var.chat_service_name}-run"
  display_name = "PreReasoner orchestrator (Cloud Run)"
  depends_on   = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "chat_anthropic_key" {
  secret_id = google_secret_manager_secret.anthropic_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.chat_run.email}"
}

# ---------- Cloud Run v2: the orchestrator ----------
resource "google_cloud_run_v2_service" "chat" {
  name                = var.chat_service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.chat_run.email

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    # I/O-bound (calls Anthropic + the engine), not model-locked like the engine — so a higher
    # per-instance concurrency is fine. Each request spawns a short-lived MCP stdio subprocess.
    max_instance_request_concurrency = 20
    timeout                          = "300s" # a multi-hop tool loop can run tens of seconds

    containers {
      image = local.chat_image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      # Cloud Run sets PORT; the Dockerfile CMD binds ORCH_PORT=$PORT. Pin container_port so PORT=8090.
      ports {
        container_port = 8090
      }

      env {
        name  = "ANTHROPIC_MODEL"
        value = var.anthropic_model
      }
      env {
        name = "ANTHROPIC_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.anthropic_key.secret_id
            version = "latest"
          }
        }
      }
      # The engine's own Cloud Run URL (server-to-server). The forwarded Firebase token authenticates
      # /api/reason at the app layer; the engine allows unauthenticated invocations at the network layer.
      env {
        name  = "ENGINE_BASE_URL"
        value = google_cloud_run_v2_service.api.uri
      }

      # The orchestrator's /healthz returns immediately (no model load) — a short cold start.
      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        timeout_seconds       = 5
        failure_threshold     = 12
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.anthropic_key,
    google_secret_manager_secret_iam_member.chat_anthropic_key,
  ]
}

# Unauthenticated at the network layer: Firebase Hosting's /chat rewrite calls it anonymously, and auth
# happens at the application layer (the browser's Firebase ID token flows through to the engine).
resource "google_cloud_run_v2_service_iam_member" "chat_invoker" {
  name     = google_cloud_run_v2_service.chat.name
  location = google_cloud_run_v2_service.chat.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "chat_url" {
  description = "The orchestrator's Cloud Run URL."
  value       = google_cloud_run_v2_service.chat.uri
}
