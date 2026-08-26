# PreReasoner GCP deployment — the core Cloud Run engine + one Cloud SQL Postgres.
# The optional chat service is declared in orchestrator.tf. Firebase Auth, RTDB, and Hosting are managed
# OUTSIDE Terraform: enabling Firebase on a project is a one-time console/CLI step and
# `firebase deploy` owns hosting — see infra/README.md.

locals {
  required_apis = [
    "run.googleapis.com",              # Cloud Run
    "sqladmin.googleapis.com",         # Cloud SQL
    "secretmanager.googleapis.com",    # DB password
    "artifactregistry.googleapis.com", # engine image
    "cloudbuild.googleapis.com",       # gcloud builds submit
    "iam.googleapis.com",              # dedicated service account
    "cloudscheduler.googleapis.com",   # scheduled RTDB trace retention (only when RTDB is enabled)
  ]

  image = var.image

  serving_user      = var.serving_db_role
  serving_secret_id = google_secret_manager_secret.serving_db_password.secret_id
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)

  service            = each.value
  disable_on_destroy = false # leave APIs on at destroy; disabling them can break unrelated workloads
}

# ---------- Artifact Registry (engine image) ----------

resource "google_artifact_registry_repository" "engine" {
  repository_id = var.artifact_repo
  location      = var.region
  format        = "DOCKER"
  description   = "PreReasoner engine images (built via cloudbuild.yaml)"

  depends_on = [google_project_service.apis]
}

# ---------- Cloud SQL: the world database ----------

resource "google_sql_database_instance" "world" {
  name             = var.sql_instance_name
  region           = var.region
  database_version = "POSTGRES_16"

  deletion_protection = var.deletion_protection

  settings {
    tier              = var.db_tier
    availability_type = var.db_availability_type
    disk_size         = 20 # GB — full sync is ~2-3 GB (db/README.md §4); 20 GB is comfortable
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
    }


    ip_configuration {
      # Public IP with ZERO authorized networks: nothing can reach it over plain TCP.
      # All access goes through the Cloud SQL connector paths (Cloud Run's /cloudsql
      # unix-socket mount, cloud-sql-proxy for seeding) which authenticate via IAM.
      # This avoids the VPC connector + private-services-access setup that a private-IP
      # instance requires; infra/README.md documents the supported access path.
      ipv4_enabled = true
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "world" {
  name     = "world"
  instance = google_sql_database_instance.world.name
}

resource "random_password" "db" {
  length  = 32
  special = false # keep it copy/paste- and URL-safe for psql/proxy use during seeding
}

# Sets the password of the built-in postgres administration role. It is reserved for
# migrations, extension setup, source synchronization, and grants; Cloud Run never receives it.
resource "google_sql_user" "postgres" {
  name     = "postgres"
  instance = google_sql_database_instance.world.name
  password = random_password.db.result
}

# ---------- Non-superuser serving role (least privilege) ----------
# The engine always authenticates as this role instead of `postgres`. Cloud SQL's
# google_sql_user is NOT a superuser. Least privilege is
# enforced by a one-time SQL bootstrap (infra/README.md §6), not by Terraform: the role is
# granted CREATE on the database (so serving can still make runtime conversation/master
# schemas), SELECT on the `knowledgebase` schema, chat DML after the admin migration, and
# SELECT-only on activated enrichment sources via `python -m db.reference_grants`. Applying this
# resource alone does NOT run that bootstrap; deployment is not ready until the bootstrap passes.

resource "random_password" "serving" {
  length  = 32
  special = false
}

resource "google_sql_user" "serving" {
  name     = var.serving_db_role
  instance = google_sql_database_instance.world.name
  password = random_password.serving.result
}

resource "google_secret_manager_secret" "serving_db_password" {
  secret_id = "${var.service_name}-serving-db-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "serving_db_password" {
  secret      = google_secret_manager_secret.serving_db_password.id
  secret_data = random_password.serving.result
}

resource "google_secret_manager_secret_iam_member" "run_serving_db_password" {
  secret_id = google_secret_manager_secret.serving_db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

# ---------- Secret Manager: DB password ----------

resource "google_secret_manager_secret" "db_password" {
  secret_id = "${var.service_name}-db-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db.result
}

# ---------- Service account for the Cloud Run service ----------

resource "google_service_account" "run" {
  account_id   = "${var.service_name}-run"
  display_name = "PreReasoner engine (Cloud Run)"

  depends_on = [google_project_service.apis]
}

# Cloud SQL connector access (the /cloudsql unix-socket mount authenticates as this SA).
resource "google_project_iam_member" "run_cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.run.email}"
}

# RTDB trace streaming: engine/trace.py initializes firebase-admin via ADC, so the runtime
# SA needs RTDB access. Also covers Firebase Auth token verification's cert fetch (public,
# but firebase-admin init wants credentials). Scoped to the whole project's RTDB instances.
resource "google_project_iam_member" "run_rtdb" {
  count   = var.rtdb_url != "" ? 1 : 0
  project = var.project_id
  role    = "roles/firebasedatabase.admin"
  member  = "serviceAccount:${google_service_account.run.email}"
}

resource "google_secret_manager_secret_iam_member" "run_db_password" {
  secret_id = google_secret_manager_secret.db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

# ---------- Cloud Run v2: the engine ----------

resource "google_cloud_run_v2_service" "api" {
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = var.deletion_protection

  template {
    service_account = google_service_account.run.email

    scaling {
      min_instance_count = var.min_instances # default 1: see the variable — cold start is ~96s
      max_instance_count = 3
    }

    # The world/reason paths serialize on one in-process model lock, so high per-instance
    # concurrency only queues requests behind the lock; keep it modest so load fans out
    # to new instances instead.
    max_instance_request_concurrency = 8
    timeout                          = "300s"

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.world.connection_name]
      }
    }

    containers {
      image = local.image

      # Sizing: the production instance runs 4 vCPU / 8Gi. The two model stacks (world reasoner +
      # dimension) sit at ~2-3 GB resident and load CPU-bound in ~96s on 4 vCPU — halving the CPU
      # roughly doubles that, and 8Gi keeps headroom for request-time tensors, 10 MB bodies, and
      # the in-memory SQLite copies of uploaded sheets.
      resources {
        limits = {
          cpu    = "4"
          memory = "8Gi"
        }
        startup_cpu_boost = true # model load is CPU-bound; boost cuts cold-start time
      }

      ports {
        container_port = 8080
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      # A KB_PG_HOST starting with "/" makes the engine use the unix socket (no port/ssl).
      env {
        name  = "KB_PG_HOST"
        value = "/cloudsql/${google_sql_database_instance.world.connection_name}"
      }
      env {
        name  = "APP_ENV"
        value = "production"
      }
      env {
        name  = "EXTERNAL_LLM_ENABLED" # deployment half of the consent gate (PRIVACY.md); the
        value = "true"                 # per-request half is the client's external_llm_consent
      }
      env {
        name  = "CORS_ORIGINS"
        value = var.cors_origins
      }
      env {
        name  = "KB_PG_DB"
        value = google_sql_database.world.name
      }
      env {
        name  = "KB_PG_USER"
        value = local.serving_user
      }
      env {
        name = "KB_PG_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = local.serving_secret_id
            version = "latest"
          }
        }
      }
      # Deterministic reference-enrichment deployment allowlist (empty = enrichment OFF). The
      # engine still requires per-dataset code approval in the registry; this is the 2nd key.
      env {
        name  = "ENRICHMENT_ACTIVE_DATASETS"
        value = var.enrichment_active_datasets
      }
      env {
        name  = "DEVICE"
        value = "cpu"
      }
      dynamic "env" {
        for_each = var.rtdb_url != "" ? [var.rtdb_url] : []
        content {
          name  = "RTDB_URL"
          value = env.value
        }
      }

      # /healthz reports ok only once both models finished loading; give the cold start
      # up to ~5 minutes before the instance is killed.
      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 10
        period_seconds        = 10
        timeout_seconds       = 5
        failure_threshold     = 30
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.db_password,
    google_secret_manager_secret_iam_member.run_db_password,
    # Ensure the serving secret and access exist before the service references them.
    google_secret_manager_secret_version.serving_db_password,
    google_secret_manager_secret_iam_member.run_serving_db_password,
  ]
}

# Unauthenticated invocations are intentional: authentication happens at the APPLICATION
# layer (engine/auth.py verifies Firebase ID tokens on /api/reason and /api/knowledge;
# /api/dimension also verifies the Firebase token before running the taxonomy model. Firebase Hosting's /api/** rewrite
# also requires the service to accept unauthenticated calls.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# RTDB has no native TTL. When trace streaming is enabled, run the same immutable engine image
# as a small scheduled job so expired trace payloads are deleted with Firebase Admin credentials.
resource "google_cloud_run_v2_job" "trace_cleanup" {
  count    = var.rtdb_url != "" ? 1 : 0
  name     = "${var.service_name}-trace-cleanup"
  location = var.region

  template {
    template {
      service_account = google_service_account.run.email
      timeout         = "900s"

      containers {
        image   = local.image
        command = ["python", "-m", "engine.trace_cleanup"]
        env {
          name  = "RTDB_URL"
          value = var.rtdb_url
        }
        env {
          name  = "RTDB_TRACE_RETENTION_DAYS"
          value = tostring(var.rtdb_trace_retention_days)
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_job_iam_member" "trace_cleanup_invoker" {
  count    = var.rtdb_url != "" ? 1 : 0
  name     = google_cloud_run_v2_job.trace_cleanup[0].name
  location = google_cloud_run_v2_job.trace_cleanup[0].location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.run.email}"
}

resource "google_cloud_scheduler_job" "trace_cleanup" {
  count     = var.rtdb_url != "" ? 1 : 0
  name      = "${var.service_name}-trace-cleanup"
  region    = var.region
  schedule  = "17 3 * * *"
  time_zone = "Etc/UTC"

  http_target {
    uri         = "https://run.googleapis.com/apis/run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.trace_cleanup[0].name}:run"
    http_method = "POST"
    oauth_token {
      service_account_email = google_service_account.run.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.trace_cleanup_invoker]
}

# ---------- Scheduled ECB rates refresh ----------
# The exchange-rate world table is only correct for CARRY_FORWARD_DAYS past its last build
# (db/sync/build_exchange_rate.py), so it is rebuilt daily from the SAME immutable engine image
# the service runs — data pipeline and serving code are one artifact. 16:30 UTC is safely after
# the ECB's ~16:00 CET reference-rate publication in both DST regimes. Live since 2026-08-26
# (created imperatively first; this block codifies it).

resource "google_cloud_run_v2_job" "ecb_rates_refresh" {
  name                = "ecb-rates-refresh"
  location            = var.region
  deletion_protection = false # stateless projection rebuild; the source ledger is the durable record

  template {
    template {
      service_account = google_service_account.run.email
      timeout         = "1800s"
      max_retries     = 1

      volumes {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.world.connection_name]
        }
      }

      containers {
        image   = local.image
        command = ["bash", "-c", "python -m db.sync.sources.ecb.sync && python -m db.sync.build_exchange_rate"]

        resources {
          limits = {
            cpu    = "1"
            memory = "2Gi"
          }
        }

        volume_mounts {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }

        env {
          name  = "KB_PG_HOST"
          value = "/cloudsql/${google_sql_database_instance.world.connection_name}"
        }
        env {
          name  = "KB_PG_DB"
          value = google_sql_database.world.name
        }
        # The sync needs DDL + TRUNCATE on its own schemas (ecb, knowledgebase.exchange_rate),
        # which the least-privilege serving role deliberately lacks; db/sync/_conn.py honors
        # SYNC_PG_* overrides, so the job authenticates as the admin role.
        env {
          name  = "SYNC_PG_USER"
          value = "postgres"
        }
        env {
          name = "SYNC_PG_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.db_password.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [google_secret_manager_secret_version.db_password]
}

resource "google_cloud_scheduler_job" "ecb_rates_refresh" {
  name      = "ecb-rates-refresh-daily"
  region    = var.region
  schedule  = "30 16 * * *"
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.ecb_rates_refresh.name}:run"

    oauth_token {
      service_account_email = google_service_account.run.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_job_iam_member" "ecb_rates_refresh_invoker" {
  name     = google_cloud_run_v2_job.ecb_rates_refresh.name
  location = google_cloud_run_v2_job.ecb_rates_refresh.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.run.email}"
}
