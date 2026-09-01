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

  # Partial backend configuration is deliberate. Every deployment supplies its own versioned,
  # access-restricted bucket and prefix at `terraform init`; a public checkout must never point at
  # the maintainer's production state. See deploy/gcp/deploy.sh and infra/README.md.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
