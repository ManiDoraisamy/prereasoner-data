# Deploy Prereasoner Community Edition

This walkthrough builds and deploys Prereasoner into **your** Google Cloud project. It creates
billable resources, including a Zonal Cloud SQL instance, required engine and chat Cloud Run
services, and a Firebase Hosting CDN release. Cloud Run scales to zero; Cloud SQL is the main
recurring cost.

## Choose a project

<walkthrough-project-setup></walkthrough-project-setup>

The project must have billing enabled. You need permission to enable APIs, create service accounts,
manage IAM, build images, create Cloud Run and Cloud SQL resources, and administer Firebase resources.

## Authenticate this temporary shell

Google deliberately withholds account credentials from third-party Open-in-Cloud-Shell repositories.
Review [`deploy/gcp/deploy.sh`](https://github.com/ManiDoraisamy/prereasoner-data/blob/v0.2.14/deploy/gcp/deploy.sh),
then authorize this shell explicitly:

```bash
gcloud auth login --update-adc
gcloud config set project "${GOOGLE_CLOUD_PROJECT}"
```

The script never receives a service-account key and never sends your Google credential to
Prereasoner. Terraform and `gcloud` use the active short-lived Google authorization directly.

## Deploy

Run the guided deployer:

```bash
bash deploy/gcp/deploy.sh --project "${GOOGLE_CLOUD_PROJECT}"
```

It shows one cost confirmation, then:

1. creates a private, versioned Terraform-state bucket in this project;
2. enables the required APIs and creates Artifact Registry;
3. enables Vertex AI for the Community Gemini chat service and grants its Cloud Run service account
   `roles/aiplatform.user`;
4. downloads and verifies the public manifested weights;
5. builds and regression-tests immutable engine and chat images in Cloud Build;
6. applies the Zonal, scale-to-zero Community Terraform profile with chat enabled;
7. prepares Firebase Hosting and the Firebase Web app, then publishes web/public through the
   same Cloud Build release;
8. downloads `community-seed-v4.dump`, verifies its pinned SHA-256, restores the public world/reference
   schemas, and installs least-privilege serving grants; and
9. removes the temporary database-bootstrap identity and temporary Firebase setup grant.

The image build, Firebase setup, and seed restore normally take tens of minutes.
The terminal continues to show progress and ends with the Firebase Hosting URL.

## Browser client

The deployment publishes the browser client at the deployment-scoped Hosting URL printed by the
script. Firebase Hosting serves the static HTML/CSS/JS from its CDN; `/api/**` and `POST /chat`
are rewrites to the two Cloud Run services. The
installer adapts the checked-in Firebase config for the selected project in the ephemeral release
context, so the repository does not maintain a second UI copy.

External model processing is enabled only for the required chat service, using Vertex AI Gemini
through the Cloud Run service account. The guided profile activates only the reviewed IANA country dataset; other reference
datasets remain disabled until the operator adds the required source data, grants, and allowlist entry.

If Firebase reports that its Terms have not been accepted, the project owner must accept them once in
the Firebase console and rerun the same command. That is a Firebase account/terms boundary, not a
separate static-file deployment step.

## Remove the deployment

The same script reviews and destroys the resources while retaining versioned Terraform state:

```bash
bash deploy/gcp/deploy.sh --project "${GOOGLE_CLOUD_PROJECT}" --destroy
```

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>
