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
  description = "Immutable engine image reference ending in @sha256:<64 lowercase hex characters>."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.image))
    error_message = "image must be an immutable registry digest reference, not a mutable tag."
  }
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

variable "db_availability_type" {
  description = "Cloud SQL availability. REGIONAL is the production default because this database contains customer conversation state as well as rebuildable public data; use ZONAL only for disposable development environments."
  type        = string
  default     = "REGIONAL"

  validation {
    condition     = contains(["REGIONAL", "ZONAL"], var.db_availability_type)
    error_message = "db_availability_type must be REGIONAL or ZONAL."
  }
}

variable "rtdb_url" {
  description = "Firebase Realtime Database URL for live trace streaming. Empty disables streaming; when set, Terraform also creates the scheduled retention cleanup job."
  type        = string
  default     = ""
}

variable "cors_origins" {
  description = "Comma-separated exact browser origins allowed to call the engine. Empty keeps the API same-origin only."
  type        = string
  default     = ""
}

variable "rtdb_trace_retention_days" {
  description = "Days before RTDB reasoning traces are removed by the scheduled cleanup job."
  type        = number
  default     = 7

  validation {
    condition     = var.rtdb_trace_retention_days >= 1 && var.rtdb_trace_retention_days <= 365 && floor(var.rtdb_trace_retention_days) == var.rtdb_trace_retention_days
    error_message = "rtdb_trace_retention_days must be a whole number from 1 through 365."
  }
}

variable "serving_db_role" {
  description = <<-EOT
    Mandatory least-privilege serving role. Terraform creates this NON-superuser Cloud SQL login
    and points Cloud Run's KB_PG_USER at it. The role still needs the admin-run application
    migration and one-time SQL bootstrap
    (CREATE on the database for runtime conversation/master schemas, chat DML, SELECT on
    `knowledgebase`, and `python -m db.reference_grants` for activated enrichment sources) — see
    infra/README.md §6. Applying with this set changes only the serving credential; it does NOT
    run the bootstrap.
  EOT
  type        = string
  default     = "serving"

  validation {
    condition     = can(regex("^[a-z][a-z0-9_]*$", var.serving_db_role)) && var.serving_db_role != "postgres"
    error_message = "serving_db_role must be a non-postgres lowercase PostgreSQL identifier."
  }
}

variable "deletion_protection" {
  description = "Protect customer-bearing Cloud SQL and Cloud Run resources from accidental deletion."
  type        = bool
  default     = true
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
