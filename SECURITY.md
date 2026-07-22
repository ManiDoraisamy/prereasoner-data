# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately to mani.doraisamy@gmail.com. Do not open public issues
for security reports. You should receive a response within a few days.

## Security model (summary)

- `/api/reason` and `/api/knowledge` require a Firebase ID token; the per-user Postgres schema is
  always derived from the **verified** token subject, never from client input. The verification
  helpers in `engine/auth.py` are deliberately small and unforked — review them first.
- `/api/dimension` is stateless and unauthenticated by design (no user data is stored).
- Reasoning traces are written to Firebase RTDB under `/runs/{uid}/…`; `database.rules.json`
  restricts reads to the owning user.
- `AUTH_TEST_SUB` bypasses token verification and must never be set in production.
- Request limits (body size, sheet count, row count) are enforced in `engine/server.py`;
  generated SQL is SELECT-only with quoted identifiers (see `engine/tables.py`).
