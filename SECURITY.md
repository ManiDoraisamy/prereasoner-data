# Security policy

## Supported versions

Security fixes are applied to the current `main` branch. Historical commits, private model
candidates, and untagged deployment images are not supported releases.

## Reporting a vulnerability

Please report vulnerabilities privately to mani.doraisamy@gmail.com. Do not open public issues
for security reports. You should receive a response within a few days.

## Security model (summary)

- `/api/reason` and `/api/knowledge` require a Firebase ID token. Conversation schemas are accepted
  only after an ownership check, and private reference schemas are derived from the **verified** token
  subject, never from client input. The verification helpers in `engine/auth.py` are deliberately
  small and unforked — review them first.
- `/api/dimension` is stateless but still requires a verified Firebase ID token before running the taxonomy
  model; statelessness is not an authorization boundary.
- Reasoning traces are written to Firebase RTDB under `/runs/{uid}/…`; `database.rules.json`
  restricts reads to the owning user.
- `AUTH_TEST_SUB` is honored only when `APP_ENV` is explicitly `development` or `test` and must never be
  set in a public deployment. Production defaults to fail-closed.
- Request limits, per-principal rate limits, and in-flight bounds are enforced in the HTTP entry points;
  generated SQL is SELECT-only with quoted identifiers (see `engine/tables.py`).
- Saved reference identifiers and unique first-column keys are validated in `engine/master.py` before
  an atomic table replacement. Only FK-connected references enter a request's planner table set.
- The Firebase web configuration and Google Picker key in `web/public/lib/config.js` are necessarily
  visible to browsers. Restrict those keys to the production HTTP referrers and required Google APIs,
  apply quotas, and never use them as server credentials or authorization checks.
