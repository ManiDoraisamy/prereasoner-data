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

  # Production state contains generated database credentials, so it lives in a versioned,
  # access-restricted GCS bucket (created 2026-08-27; project-private, uniform access).
  # Self-hosters: point this at your own bucket or delete the block for local dev state.
  backend "gcs" {
    bucket = "prereasoner-inference-tfstate"
    prefix = "prereasoner"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
