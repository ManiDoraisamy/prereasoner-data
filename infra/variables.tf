variable "project_id" {
  description = "GCP project id. Must already exist and (for auth/RTDB/hosting) be Firebase-enabled — see infra/README.md prerequisites."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry and Cloud SQL. web/firebase.json rewrites /api/** to this region, so change both together."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name. web/firebase.json rewrites /api/** to serviceId \"prereasoner-api\", so change both together."
  type        = string
  default     = "prereasoner-api"
}

variable "image" {
  description = <<-EOT
    Full image ref for the engine (built by `gcloud builds submit --config cloudbuild.yaml`).
    Empty = the default tag cloudbuild.yaml pushes:
    <region>-docker.pkg.dev/<project>/<repo>/engine:latest
  EOT
  type        = string
  default     = ""
}

variable "artifact_repo" {
  description = "Artifact Registry docker repository name (must match cloudbuild.yaml's _REPO)."
  type        = string
  default     = "prereasoner"
}

variable "sql_instance_name" {
  description = "Cloud SQL instance name. NOTE: Cloud SQL reserves deleted instance names for ~1 week; pick a new name if you destroy and re-apply quickly."
  type        = string
  default     = "prereasoner-world"
}

variable "db_tier" {
  description = "Cloud SQL machine tier. db-custom-1-3840 (1 vCPU / 3.75 GB) comfortably holds the ~2-3 GB fully-synced world DB + HNSW index."
  type        = string
  default     = "db-custom-1-3840"
}

variable "rtdb_url" {
  description = "Firebase Realtime Database URL for live reasoning-trace streaming (e.g. https://<project>-default-rtdb.firebaseio.com). Empty = streaming disabled; responses still carry full JSON."
  type        = string
  default     = ""
}

variable "serving_db_role" {
  description = <<-EOT
    Opt-in least-privilege serving. Empty (default) = the engine serves as the `postgres`
    superuser (current behavior). Set to a lowercase role name (e.g. "serving") to have
    Terraform create a NON-superuser Cloud SQL login role and point Cloud Run's KB_PG_USER at
    it. The role still needs the admin-run application migration and one-time SQL bootstrap
    (CREATE on the database for runtime conversation/master schemas, chat DML, SELECT on
    `knowledgebase`, and `python -m db.reference_grants` for activated enrichment sources) — see
    infra/README.md §6. Applying with this set changes only the serving credential; it does NOT
    run the bootstrap.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.serving_db_role == "" || can(regex("^[a-z][a-z0-9_]*$", var.serving_db_role))
    error_message = "serving_db_role must be empty or a lowercase PostgreSQL identifier."
  }
}

variable "enrichment_active_datasets" {
  description = <<-EOT
    Deployment allowlist for deterministic reference enrichment — the second of two activation
    keys (the first is per-dataset code approval in engine/enrichment/registry.py). Comma-separated
    code-approved dataset names (e.g. "iana_country"). Empty (default) keeps enrichment OFF: the
    engine serves own-data + world answers exactly as before.
  EOT
  type        = string
  default     = ""
}
