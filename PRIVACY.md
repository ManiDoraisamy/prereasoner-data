# Privacy and Data Processing

This document describes the behavior of the open-source software. A hosted operator must publish
its own legally reviewed privacy notice, processor list, retention schedule, contact details, and
applicable terms before accepting customer data.

## Data Processed By The Core Engine

The deterministic engine processes uploaded table names, column names, cell values, questions,
generated SQL, query results, and reasoning traces. Depending on deployment configuration:

- PostgreSQL stores conversation metadata, uploaded/derived tables, and saved workbook state in
  user-scoped schemas.
- Firebase Realtime Database can temporarily store reasoning traces under `/runs/{uid}/{jobId}`.
- Application logs can contain operational errors. Deployers must not add raw request bodies,
  credentials, or customer rows to logs.

Firebase identity is verified server-side. Client-supplied user IDs do not select storage
ownership.

## External LLM Processing

External LLM features are disabled by default. The engine and orchestrator call Anthropic only
when both conditions are true:

1. the operator sets `EXTERNAL_LLM_ENABLED=true`; and
2. the authenticated request contains the literal boolean `external_llm_consent: true`.

Depending on the feature, an Anthropic request can contain:

- the user's message and conversation history;
- table and column names;
- entity names and existing reference-table cells;
- generated SQL, result columns, and up to 40 result rows; and
- trimmed reasoning/tool output.

The purpose is conversational presentation, ambiguity handling, tool orchestration, or filling
explicitly requested reference cells. The deterministic SQL path does not require Anthropic.

Enabling these features for a hosted service requires a user-facing consent flow, data
minimization review, applicable Anthropic terms and data-processing agreement, and a documented
retention policy. Setting the environment variable alone is not a substitute for those controls.

The reference deployment (chat.prereasoner.com) has **no in-app consent control**. The in-rail
notice-and-choice line and its one-click "Use local-only" switch were removed on 2026-08-30 by
operator decision; the client now asserts `external_llm_consent: true` on every gated call. The
disclosure obligation therefore rests entirely on the operator's published privacy notice, which
must state that a user's question and sheet data are sent to Anthropic — the application no
longer says so, and offers the end user no way to decline.

Two controls remain, and neither is an end-user choice:

- `EXTERNAL_LLM_ENABLED` — the operator's server-side switch, and the authoritative one. Unset it
  and every gated endpoint refuses with 503, so nothing reaches Anthropic whatever a client sends.
- `?chat=0` — runs the deterministic `/api/reason` path with no orchestrator. This is a debugging
  flag, not a discoverable or documented privacy control.

Self-hosters who need a user-facing consent flow — which is the norm wherever the data is personal
or regulated — must keep `EXTERNAL_LLM_ENABLED` unset, or reinstate a client-side flow, before
exposing the service.

## Retention and Deletion

Conversation deletion removes owned PostgreSQL metadata, its per-conversation schema, and RTDB
jobs indexed to that conversation. Delete-all removes the verified Firebase user's entire
`/runs/{uid}` subtree. A configured RTDB deletion failure aborts the operation rather than
reporting privacy deletion as successful.

When RTDB is enabled, new trace jobs carry a seven-day expiry by default
(`RTDB_TRACE_RETENTION_DAYS`, bounded to 1–365 days). The Terraform deployment creates a
Cloud Run cleanup job and a daily Cloud Scheduler trigger; deployments managed outside Terraform
must run `python -m engine.trace_cleanup` with Firebase Admin credentials on the same cadence.
Older traces without expiry metadata require a one-time operator cleanup.

Deleting a Firebase account is not currently an application-level request to erase PostgreSQL or
RTDB data. A hosted operator must implement and test that workflow before claiming account
deletion.

## Public Reference Data and Model Training

Public Schema.org, Wikidata, and publisher datasets are documented in `THIRD_PARTY.md`,
`docs/SOURCE_DATA.md`, and `docs/DATA_CARD.md`. Customer rows are not part of the released training
corpora. Consent-bound private evaluation metadata remains in ignored local paths and must not be
published or used for training without explicit permission.

## Security

See `SECURITY.md` for vulnerability reporting. Do not include personal or customer data in a
public issue.
