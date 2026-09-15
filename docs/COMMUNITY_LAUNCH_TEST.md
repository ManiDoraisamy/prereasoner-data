# Community Edition launch test

This is the repeatable release test for the required UI/chat installation. It verifies that one
canonical `web/public` tree is used locally and by the Firebase Hosting CDN, while `/api/**` and
`POST /chat` reach the deployed Cloud Run services.

## Test inputs

- GCP project: a billing-enabled project owned by the test operator.
- Region: `us-central1` unless the release is explicitly testing another region.
- Deployment name: a fresh, unique lowercase name of 2–20 characters, for example
  `ce-test-0915`. The name scopes Cloud Run, Cloud SQL, Artifact Registry, Secret Manager, state,
  and the Firebase Hosting site.
- Community seed artifact: `community-seed-v4.dump`, imported by the installer after its pinned
  SHA-256 is verified. The dump contains public world/reference schemas only; it must not contain
  `chat`, `c_*`, or `m_*` schemas.
- Chat model: Vertex AI `gemini-3.8-flash`, authorized by the Community chat service account.
- Default browser fixture: `web/public/dataset/customer-orders/orders.csv` with the question
  `total amount in France in US dollars` from `prompt.txt`.

## Preflight

1. Start from a clean checkout at the exact release ref. Confirm the ref contains the required
   engine image, chat image, Hosting build context, and local Compose changes.
2. Confirm the GCP account can use the selected project and that billing is enabled.
3. Confirm the deployment name has no existing `*-api`, `*-chat`, `*-world`, Artifact Registry,
   Secret Manager, state bucket, or Firebase Hosting site. Never use a name belonging to another
   deployment.
4. For the local case, confirm Docker Desktop (or an equivalent Docker engine) is installed and
   running. The local test cannot pass on a machine with only Python/Node because the required
   PostgreSQL + vector service is part of the Compose installation.
5. Do not use the public Cloud Shell button until its `cloudshell_git_branch` points at the exact
   tested release ref. A button pointing at an older tag is a release-blocking documentation bug.

## GCP test: install, browser question, uninstall

Test ID: `CE-GCP-001`

1. Open the README's **Open in Google Cloud Shell** link. Accept the Cloud Shell repository trust
   dialog only after confirming the displayed repository and ref are the intended release.
2. In Cloud Shell, run the tutorial's authentication command and then the guided installer:

   ```bash
   gcloud auth login --update-adc
   gcloud config set project "${GOOGLE_CLOUD_PROJECT}"
   bash deploy/gcp/deploy.sh --project "${GOOGLE_CLOUD_PROJECT}" --region us-central1 \
     --name ce-test-0915
   ```

   The installer must not prompt for an Anthropic key. It enables Vertex AI, grants the chat
   service account `roles/aiplatform.user`, and imports the pinned Community seed artifact.
3. Record the Hosting URL printed by the installer. Verify:

   - `GET /` returns the static home page from the deployment-scoped Firebase Hosting site.
   - `GET /lib/config.js` and the stylesheet return successfully.
   - the HTML shows the `orders` chip and the default question;
   - the UI **Ask (↑)** button is enabled after the demo workbook loads;
   - clicking **Ask (↑)** navigates to `/reason/<conversation-id>`;
   - the result sheet appears without an error and contains a numeric result for France in USD;
   - browser network activity shows `/api/**` for the engine call and `POST /chat` for the required
     Gemini chat service;
   - the Hosting rewrite configuration contains the deployment's API and chat Cloud Run service
     IDs and the selected region.

4. Verify the service boundaries independently:

   ```bash
   gcloud run services describe ce-test-0915-api --region us-central1 \
     --format='value(status.url)'
   gcloud run services describe ce-test-0915-chat --region us-central1 \
     --format='value(status.url)'
   curl -fsS "${ENGINE_URL}/api/healthz"
   ```

   An unauthenticated `POST /api/reason` must return `401`; a ready engine health check must
   return `{"ok": true}`. Do not call the model endpoint without an authenticated browser user
   except for this bounded auth check.
5. Uninstall the test deployment:

   ```bash
   bash deploy/gcp/deploy.sh --project "${GOOGLE_CLOUD_PROJECT}" --region us-central1 \
     --name ce-test-0915 --destroy
   ```

   Type `DESTROY` at the review prompt. Confirm that the deployment's Cloud Run services, Cloud
   SQL instance, scheduled jobs, service accounts, secrets, Artifact Registry images, and
   deployment-scoped Firebase Hosting site are gone. The versioned Terraform state bucket is
   intentionally retained for audit; delete it separately only when the release owner asks for
   complete state erasure.

## Local test: install, browser question, uninstall

Test ID: `CE-LOCAL-001`

1. From the exact same release ref, create `.env` from `.env.example` and set only local values,
   including `ANTHROPIC_API_KEY`. Never commit `.env`.
2. Provision/verify the pinned model bundle and start the required full stack:

   ```powershell
   python -m engine.fetch_weights
   docker compose up -d db
   docker compose --profile seed run --rm seed
   docker compose up --build -d
   ```

3. Open `http://localhost:8090/` and repeat the same default-fixture checks: `orders` is present,
   the question is `total amount in France in US dollars`, **Ask (↑)** is enabled, and the result
   sheet is rendered without an error. Confirm `/config` reports local test auth and that the
   browser request reaches local `POST /chat`; the engine is available at `http://localhost:8080`.
4. Confirm the local chat container serves the canonical mounted `web/public` directory. Change a
   harmless static label in a disposable working copy, reload, and verify the mounted UI changes
   without rebuilding the chat image. Restore the disposable change before teardown.
5. Uninstall only this Compose project after the browser check:

   ```powershell
   docker compose down --volumes --remove-orphans
   ```

   Confirm no project containers or volumes remain. `--volumes` removes the test PostgreSQL data;
   do not use it against a shared Compose project.

## Pass/fail evidence

Attach the release commit/ref, deployment name, Hosting URL, timestamps, browser result screenshot
or trace, Cloud Run/Hosting rewrite evidence, and teardown command output. A run is **PASS** only
when both install paths answer the default question and both teardown paths leave no test runtime
resources. A missing Docker engine, an unaccepted Firebase Terms boundary, a stale Cloud Shell ref,
or a missing Anthropic key is a **BLOCKED PRECONDITION**, not a successful install.
