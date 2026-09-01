# Privacy and Data Processing

This document is the technical privacy contract for the open-source software and the reference
deployment at `chat.prereasoner.com`. The published, user-facing notice is
`web/public/privacy.html`, served at `/privacy`. Keep the two documents consistent whenever a data
flow, processor, retention rule, or deletion path changes.

The reference service is operated by MailRecipe LLC, 340 S Lemon Ave #9974, Walnut, CA 91789,
United States. The Guesswork team builds PreReasoner; it is not a different data controller for
the reference service.

## Product Rule: Disclosure Without Consent UI

Ordinary service processing must not be presented as a modal, banner, repeated notice, or model-
provider choice. The product publishes one durable privacy notice and links it unobtrusively from
every user-facing surface. The operator is responsible for choosing and documenting an applicable
legal basis, maintaining processor agreements, minimizing data, and honoring privacy rights.

If a particular deployment or data category legally requires an opt-in, the operator must not
enable that processing until it has a suitable lawful workflow. Do not add a generic prompt to the
reference product as a substitute for that assessment.

## Data Processed By The Core Engine

The deterministic engine processes uploaded table names, column names, cell values, questions,
generated SQL, query results, and reasoning traces. Depending on deployment configuration:

- PostgreSQL stores conversation metadata, uploaded and derived tables, and saved workbook state
  in user-scoped schemas.
- Firebase Authentication processes identity and session information.
- Firebase Realtime Database can temporarily store reasoning traces under `/runs/{uid}/{jobId}`.
- Application logs can contain operational errors. Deployers must not add raw request bodies,
  credentials, or customer rows to logs.

Firebase identity is verified server-side. Client-supplied user IDs do not select storage
ownership. Google Sheets imports use the narrow `drive.file` scope and read only a file the user
selects.

## External LLM Processing

The open-source default is `EXTERNAL_LLM_ENABLED=false`. The reference hosted deployment can use
Anthropic for the conversational assistant, presentation, ambiguity handling, tool orchestration,
and explicitly requested reference-cell generation. Depending on the feature, an Anthropic request
can contain:

- the user's message and conversation history;
- attached table names, columns, and contents for the chat assistant;
- entity names and existing reference-table cells;
- generated SQL, result columns, and up to 40 result rows; and
- trimmed reasoning or tool output.

The deterministic SQL path computes answers and does not require Anthropic to generate SQL or
numbers. Anthropic's current handling of commercial-customer data is governed by the operator's
agreement with Anthropic and Anthropic's published privacy materials. Do not make training,
retention, residency, or deletion claims on Anthropic's behalf unless they are verified against the
applicable agreement and configuration.

The server-side `EXTERNAL_LLM_ENABLED` switch is authoritative. When false, gated endpoints refuse
the call. Self-hosters can therefore run the deterministic engine without an external model.

The architectural target is to replace external presentation and orchestration with a locally
operated model once a candidate passes the repository's quality, latency, security, and cost gates.
That migration is an operator responsibility and must be transparent to users: it must not create a
new popup or expose provider selection as part of the analysis workflow. Calculation semantics,
SQL execution, and verification remain deterministic.

## Retention and Deletion

Conversation deletion removes owned PostgreSQL metadata, its per-conversation schema, and RTDB
jobs indexed to that conversation. Delete-all removes the verified Firebase user's entire
`/runs/{uid}` subtree. A configured RTDB deletion failure aborts the operation rather than reporting
privacy deletion as successful.

When RTDB is enabled, new trace jobs carry a seven-day expiry by default
(`RTDB_TRACE_RETENTION_DAYS`, bounded to 1-365 days). The Terraform deployment creates a Cloud Run
cleanup job and a daily Cloud Scheduler trigger; deployments managed outside Terraform must run
`python -m engine.trace_cleanup` with Firebase Admin credentials on the same cadence. Older traces
without expiry metadata require a one-time operator cleanup.

Deleting a Firebase account is not currently an application-level request to erase PostgreSQL or
RTDB data. The hosted policy therefore directs complete deletion requests to the operator. A
deployment must implement and test account-linked erasure before claiming that account deletion
alone removes all service data.

## Public Reference Data and Model Training

Public Schema.org, Wikidata, and publisher datasets are documented in `THIRD_PARTY.md`,
`docs/SOURCE_DATA.md`, and `docs/DATA_CARD.md`. Customer rows are not part of the released training
corpora. Consent-bound private evaluation metadata remains in ignored local paths and must not be
published or used for training without explicit permission from the data owner.

## Operator Checklist

Before accepting customer data, a hosted operator must:

1. Publish `/privacy` with the operator identity, contact, actual processors, and deployed data
   flows.
2. Review the lawful basis and contracts for the intended users and data categories.
3. Configure and verify deletion, trace cleanup, backup retention, access control, and incident
   handling.
4. Keep external processing disabled for regulated data unless the deployment has the necessary
   contractual and technical controls.
5. Update both privacy documents before changing processors or materially changing a data flow.

## Security And Contact

See `SECURITY.md` for vulnerability reporting. Do not include personal or customer data in a public
issue. Privacy questions and data-rights requests for the reference deployment can be sent to
`mani.doraisamy@gmail.com`.
