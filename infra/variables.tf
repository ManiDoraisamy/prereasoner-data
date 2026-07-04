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
