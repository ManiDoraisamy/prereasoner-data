# The Sonnet orchestrator chat backend (prereasoner-chat) — docs/MCP.md.
#
# A SECOND, lightweight Cloud Run service alongside the engine (google_cloud_run_v2_service.api in
# main.tf). It calls the engine over HTTP and Anthropic over HTTPS; it does NOT touch Postgres or write
# RTDB (the engine does that when it receives the forwarded Firebase token + jobId). So its SA needs only
# the Anthropic-key secret — no Cloud SQL, no RTDB roles.
#
# Deploy order (see infra/README.md):
#   1. gcloud builds submit --config cloudbuild.orchestrator.yaml   # builds+pushes the chat image (tests-gated)
#   2. terraform apply -var="enable_orchestrator=true" \
#        -var="anthropic_secret_id=existing-secret-id"              # this file + main.tf
#   3. cd web && firebase deploy --only hosting,database            # ships chat.html + the /chat rewrite

variable "enable_orchestrator" {
  description = "Create the optional third-party chat orchestrator and its secret/IAM resources."
  type        = bool
  default     = false
}

variable "anthropic_secret_id" {
  description = "Existing Secret Manager secret ID containing the Anthropic API key. Provision its version out-of-band; required when enable_orchestrator=true."
  type        = string
  default     = ""
}

variable "chat_service_name" {
  description = "Cloud Run service name for the orchestrator. web/firebase.json rewrites /chat to this serviceId, so change both together."
  type        = string
  default     = "prereasoner-chat"
}

variable "chat_image" {
  description = "Immutable orchestrator image digest. Required when enable_orchestrator=true."
  type        = string
  default     = ""

  validation {
    condition     = !var.enable_orchestrator || can(regex("@sha256:[0-9a-f]{64}$", var.chat_image))
    error_message = "chat_image must be an immutable digest when the orchestrator is enabled."
  }
}

variable "anthropic_model" {
  description = "Sonnet model id the orchestrator uses."
  type        = string
  default     = "claude-sonnet-5"
}

# ---------- Service account for the orchestrator ----------
resource "google_service_account" "chat_run" {
  count        = var.enable_orchestrator ? 1 : 0
  account_id   = "${var.chat_service_name}-run"
  display_name = "PreReasoner orchestrator (Cloud Run)"
  depends_on   = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "chat_anthropic_key" {
  count     = var.enable_orchestrator ? 1 : 0
  secret_id = var.anthropic_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.chat_run[0].email}"
}

# ---------- Cloud Run v2: the orchestrator ----------
resource "google_cloud_run_v2_service" "chat" {
  count               = var.enable_orchestrator ? 1 : 0
  name                = var.chat_service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  lifecycle {
    precondition {
      condition     = length(trimspace(var.anthropic_secret_id)) > 0
      error_message = "anthropic_secret_id must name an out-of-band secret when enable_orchestrator=true."
    }
  }

  template {
    service_account = google_service_account.chat_run[0].email

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    # I/O-bound (calls Anthropic + the engine), not model-locked like the engine — so a higher
    # per-instance concurrency is fine. Each request spawns a short-lived MCP stdio subprocess.
    max_instance_request_concurrency = 20
    timeout                          = "300s" # a multi-hop tool loop can run tens of seconds

    containers {
      image = var.chat_image

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
        name  = "EXTERNAL_LLM_ENABLED"
        value = "true"
      }
      env {
        name = "ANTHROPIC_API_KEY"
        value_source {
          secret_key_ref {
            secret  = var.anthropic_secret_id
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
    google_secret_manager_secret_iam_member.chat_anthropic_key,
  ]
}

# Unauthenticated at the network layer: Firebase Hosting's /chat rewrite calls it anonymously, and auth
# happens at the application layer (the browser's Firebase ID token flows through to the engine).
resource "google_cloud_run_v2_service_iam_member" "chat_invoker" {
  count    = var.enable_orchestrator ? 1 : 0
  name     = google_cloud_run_v2_service.chat[0].name
  location = google_cloud_run_v2_service.chat[0].location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "chat_url" {
  description = "The orchestrator's Cloud Run URL."
  value       = var.enable_orchestrator ? google_cloud_run_v2_service.chat[0].uri : null
}
