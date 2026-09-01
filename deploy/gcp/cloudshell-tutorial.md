# Deploy PreReasoner Community Edition

This walkthrough builds and deploys PreReasoner into **your** Google Cloud project. It creates
billable resources, including a Zonal Cloud SQL instance. Cloud Run scales to zero; Cloud SQL is
the main recurring cost.

## Choose a project

<walkthrough-project-setup></walkthrough-project-setup>

The project must have billing enabled. You need permission to enable APIs, create service accounts,
manage IAM, build images, and create Cloud Run and Cloud SQL resources.

## Authenticate this temporary shell

Google deliberately withholds account credentials from third-party Open-in-Cloud-Shell repositories.
Review [`deploy/gcp/deploy.sh`](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/deploy/gcp/deploy.sh),
then authorize this shell explicitly:

```bash
gcloud auth login --update-adc
gcloud config set project "${GOOGLE_CLOUD_PROJECT}"
```

The script never receives a service-account key and never sends your Google credential to
PreReasoner. Terraform and `gcloud` use the active short-lived Google authorization directly.

## Deploy

Run the guided deployer:

```bash
bash deploy/gcp/deploy.sh --project "${GOOGLE_CLOUD_PROJECT}"
```

It shows one cost confirmation, then:

1. creates a private, versioned Terraform-state bucket in this project;
2. enables the required APIs and creates Artifact Registry;
3. downloads and verifies the public manifested weights;
4. builds and regression-tests an immutable engine image in Cloud Build;
5. applies the Zonal, scale-to-zero Community Terraform profile;
6. loads the minimal Wikidata world tables and current ECB exchange-rate history, then installs
   least-privilege serving grants; and
7. removes the temporary database-bootstrap identity.

The image build and minimal Wikidata synchronization normally take tens of minutes. The terminal
continues to show progress and ends with the Cloud Run service URL.

## Browser client

The deployment above is the deterministic engine API. Protected answer endpoints require a Firebase
ID token. To host the included browser client, follow
[`web/README.md`](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/web/README.md) and attach
your own Firebase project. External LLM processing and reference enrichment remain disabled.

## Remove the deployment

The same script reviews and destroys the resources while retaining versioned Terraform state:

```bash
bash deploy/gcp/deploy.sh --project "${GOOGLE_CLOUD_PROJECT}" --destroy
```

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>
