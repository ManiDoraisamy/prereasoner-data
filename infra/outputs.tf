output "service_url" {
  description = "Cloud Run URL of the engine (Firebase Hosting rewrites /api/** here)."
  value       = google_cloud_run_v2_service.api.uri
}

output "sql_connection_name" {
  description = "Cloud SQL connection name — use with cloud-sql-proxy for seeding, and as the /cloudsql/<...> unix-socket path."
  value       = google_sql_database_instance.world.connection_name
}

output "sql_public_ip" {
  description = "Cloud SQL public IP (no authorized networks — reachable only via the connector/proxy)."
  value       = google_sql_database_instance.world.public_ip_address
}

output "image" {
  description = "Engine image the service is running."
  value       = local.image
}

output "db_password_secret" {
  description = "Secret Manager secret id holding the postgres password (access: gcloud secrets versions access latest --secret=<this>)."
  value       = google_secret_manager_secret.db_password.secret_id
}

output "serving_db_role" {
  description = "Mandatory non-superuser role used by the engine."
  value       = var.serving_db_role
}

output "serving_db_password_secret" {
  description = "Secret Manager secret id holding the serving role's password."
  value       = google_secret_manager_secret.serving_db_password.secret_id
}

output "enrichment_active_datasets" {
  description = "Deterministic reference-enrichment deployment allowlist in effect (empty = enrichment off)."
  value       = var.enrichment_active_datasets
}
