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

  # State is local by default for disposable development only. Production state contains
  # generated database credentials: use a versioned, access-restricted GCS backend, e.g.:
  #   backend "gcs" { bucket = "<your-tf-state-bucket>", prefix = "prereasoner" }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
