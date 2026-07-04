terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is local by default (fine for a personal deployment). For teams, configure a
  # GCS backend instead, e.g.:
  #   backend "gcs" { bucket = "<your-tf-state-bucket>", prefix = "prereasoner" }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
