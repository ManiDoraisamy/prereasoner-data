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

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,22}[a-z0-9]$", var.service_name))
    error_message = "service_name must be 2-24 lowercase letters, digits, or hyphens so derived service-account ids remain valid."
  }
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
  description = "Firebase Realtime Database URL for live trace streaming. Empty disables trace streaming and trace cleanup; PostgreSQL conversation cleanup still runs."
  type        = string
  default     = ""
}

variable "cors_origins" {
  description = "Comma-separated exact browser origins allowed to call the engine. Empty keeps the API same-origin only."
  type        = string
  default     = ""
}

variable "admin_emails" {
  description = "Comma-separated Firebase-authenticated email allowlist for /api/admin/*. Empty disables admin API access."
  type        = string
  default     = ""
}

variable "enable_external_llm" {
  description = "Enable engine features that call the configured external LLM. False leaves its secret, IAM grant, and request paths disabled."
  type        = bool
  default     = false
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

variable "conversation_retention_days" {
  description = "Days of inactivity before a stored conversation and its PostgreSQL schema are deleted."
  type        = number
  default     = 90
  validation {
    condition     = var.conversation_retention_days >= 1 && var.conversation_retention_days <= 3650 && floor(var.conversation_retention_days) == var.conversation_retention_days
    error_message = "conversation_retention_days must be a whole number from 1 through 3650."
  }
}

variable "max_conversations_per_user" {
  description = "Maximum number of durable conversations per authenticated user."
  type        = number
  default     = 100
  validation {
    condition     = var.max_conversations_per_user >= 1 && var.max_conversations_per_user <= 1000 && floor(var.max_conversations_per_user) == var.max_conversations_per_user
    error_message = "max_conversations_per_user must be a whole number from 1 through 1000."
  }
}

variable "max_conversation_storage_bytes" {
  description = "Maximum stored source-table and workbook-state bytes per authenticated user."
  type        = number
  default     = 268435456
  validation {
    condition     = var.max_conversation_storage_bytes >= 1048576 && floor(var.max_conversation_storage_bytes) == var.max_conversation_storage_bytes
    error_message = "max_conversation_storage_bytes must be a whole number of at least 1 MiB."
  }
}

variable "deterministic_execution_mode" {
  description = "Default shared-plan execution policy: auto prefers bounded Python and falls back to SQL."
  type        = string
  default     = "auto"

  validation {
    condition     = contains(["auto", "python", "sql", "verify"], var.deterministic_execution_mode)
    error_message = "deterministic_execution_mode must be auto, python, sql, or verify."
  }
}

variable "deterministic_python_row_limit" {
  description = "Maximum estimated and materialized input rows allowed for generated Python execution."
  type        = number
  default     = 10000

  validation {
    condition     = var.deterministic_python_row_limit >= 0 && floor(var.deterministic_python_row_limit) == var.deterministic_python_row_limit
    error_message = "deterministic_python_row_limit must be a non-negative whole number."
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

variable "min_instances" {
  description = <<-EOT
    Minimum warm engine instances. 1 is the production default: the engine loads ~96s of models
    at cold start (past Firebase Hosting's ~60s proxy timeout, so a first visitor's answer falls
    back to the RTDB stream). One warm 4-CPU/8Gi instance costs roughly $30-35/month at idle
    rates and removes that first impression entirely. Use 0 for disposable dev environments.
  EOT
  type        = number
  default     = 1

  validation {
    condition     = var.min_instances >= 0 && var.min_instances <= 3
    error_message = "min_instances must be between 0 and 3."
  }
}
