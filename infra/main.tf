# PreReasoner GCP deployment — ONE Cloud Run service (the consolidated engine) + ONE
# Cloud SQL Postgres. The Firebase pieces (Auth, RTDB, Hosting for web/) are managed
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
  ]

  # Default image = what cloudbuild.yaml pushes. Override with -var image=... to pin a tag.
  image = var.image != "" ? var.image : "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_repo}/engine:latest"
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

  # Set true once the DB is seeded and you care about it; false keeps `terraform destroy` one-step.
  deletion_protection = false

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL" # single-zone: cheapest; this is a rebuildable cache of Wikidata
    disk_size         = 20      # GB — full sync is ~2-3 GB (db/README.md §4); 20 GB is comfortable
    disk_autoresize   = true

    ip_configuration {
      # Public IP with ZERO authorized networks: nothing can reach it over plain TCP.
      # All access goes through the Cloud SQL connector paths (Cloud Run's /cloudsql
      # unix-socket mount, cloud-sql-proxy for seeding) which authenticate via IAM.
      # This avoids the ~$10/mo VPC connector + private-services-access setup that a
      # private-IP instance would require — see docs/notes/infra.md for the trade-off.
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

# Sets the password of the built-in postgres superuser-ish role (cloudsqlsuperuser),
# which init.sql needs for CREATE EXTENSION and the engine needs for CREATE SCHEMA.
resource "google_sql_user" "postgres" {
  name     = "postgres"
  instance = google_sql_database_instance.world.name
  password = random_password.db.result
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
  deletion_protection = false

  template {
    service_account = google_service_account.run.email

    scaling {
      min_instance_count = 0 # scale to zero — cold starts (~model load) traded for cost
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

      # Sizing: the Qwen2.5-0.5B base + LoRA + relational readout + spaCy en_core_web_md
      # sit at ~1.5-2 GB resident after load; 4Gi leaves headroom for request-time tensors
      # and the 10 MB request bodies. 2 vCPU keeps CPU inference latency tolerable.
      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
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

      # A WORLD_PG_HOST starting with "/" makes the engine use the unix socket (no port/ssl).
      env {
        name  = "WORLD_PG_HOST"
        value = "/cloudsql/${google_sql_database_instance.world.connection_name}"
      }
      env {
        name  = "WORLD_PG_DB"
        value = google_sql_database.world.name
      }
      env {
        name  = "WORLD_PG_USER"
        value = google_sql_user.postgres.name
      }
      env {
        name = "WORLD_PG_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_password.secret_id
            version = "latest"
          }
        }
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
  ]
}

# Unauthenticated invocations are intentional: authentication happens at the APPLICATION
# layer (engine/auth.py verifies Firebase ID tokens on /api/reason and /api/world;
# /api/dimension is stateless and public by design). Firebase Hosting's /api/** rewrite
# also requires the service to accept unauthenticated calls.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
