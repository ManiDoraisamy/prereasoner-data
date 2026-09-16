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
Review [`deploy/gcp/deploy.sh`](https://github.com/ManiDoraisamy/prereasoner-data/blob/v0.2.23/deploy/gcp/deploy.sh),
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
7. downloads `community-seed-v4.dump`, verifies its pinned SHA-256, restores the public world/reference
   schemas, and installs least-privilege serving grants;
8. prepares Firebase Hosting and the Firebase Web app, **enables anonymous sign-in, authorizes this
   deployment's Hosting domains**, and publishes web/public through the same Cloud Build release; and
9. removes the temporary database-bootstrap identity and temporary Firebase setup grant.

Step 8 is why there is nothing left for you to set up: the deployed site can sign users in the moment
it is published. You will not be asked for an API key, and you do not need to open the Firebase
console.

The two image builds dominate a first run. Restoring the seed takes about nine minutes, because a
PostgreSQL dump stores index definitions rather than index contents, so the vector index is rebuilt in
your project. The terminal shows progress throughout and ends with the Firebase Hosting URL.

## Try it

Open the Hosting URL the script prints. The home page arrives with a demo spreadsheet already
attached and a question filled in, so press **Ask (↑)**: you are signed in silently and the answer
renders as a sheet, with the derivation steps beside it.

Firebase Hosting serves the static HTML/CSS/JS from its CDN; `/api/**` and `POST /chat` are rewrites
to the two Cloud Run services. The installer adapts the checked-in Firebase config for the selected
project in the ephemeral release context, so the repository does not maintain a second UI copy.

Sign-in is Firebase **anonymous** auth, enabled for you during the release. Each browser gets its own
identity and its own conversations, so clearing site data or moving to another device starts fresh.
Google sign-in is not offered because enabling it requires an OAuth client that no public API can
create for a project outside an organization — it cannot be automated, and this installer refuses to
hand you console homework instead.

External model processing is enabled only for the required chat service, using Vertex AI Gemini
through the Cloud Run service account. The guided profile activates only the reviewed IANA country dataset; other reference
datasets remain disabled until the operator adds the required source data, grants, and allowlist entry.

Adding Firebase to a brand-new project succeeded without any Terms prompt when this was last verified
(2026-09-16), under an account that had used Firebase before. If your account has never accepted the
Firebase Terms, Firebase may still ask the project owner to accept them once in its console; rerun the
same command afterwards. That is a Firebase account boundary, not a separate deployment step.

## Remove the deployment

The same script reviews and destroys the resources while retaining versioned Terraform state:

```bash
bash deploy/gcp/deploy.sh --project "${GOOGLE_CLOUD_PROJECT}" --destroy
```

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>
