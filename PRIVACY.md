# Privacy and Data Processing

This document is the technical privacy contract for the open-source software and the reference
deployment at `chat.prereasoner.com`. The published, user-facing notice is
`web/public/privacy.html`, served at `/privacy`. Keep the two documents consistent whenever a data
flow, processor, retention rule, or deletion path changes.

The reference service is operated by MailRecipe LLC, 340 S Lemon Ave #9974, Walnut, CA 91789,
United States. The Guesswork team builds Prereasoner; it is not a different data controller for
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
- Application logs record stable operation names, sizes, and exception classes. Request bodies,
  prompts, conversation history, generated SQL, credentials, customer rows, source values, and
  full exception messages are not logged by the serving or orchestrator request paths.

Firebase identity is verified server-side. Client-supplied user IDs do not select storage
ownership. The Marketplace add-on uses `spreadsheets.currentonly` and reads only the spreadsheet in
which it is invoked. The separate web picker uses the narrow `drive.file` scope and reads only a
file the user selects.

## Google API Limited Use And AI/ML

The reference service's use of information received from Google Workspace APIs adheres to the
Google User Data and Developer Policy, including its Limited Use requirements. Raw, aggregated,
anonymized, and derived Google user data is used only for the visible user-facing features the user
requests and the security and support needed to operate them. It is not used, transferred, or sold
to create, train, or improve generalized or non-personalized AI or ML models. The external model
(Gemini on Vertex AI) receives request data only to produce the requested feature output; the
operator must not authorize Google or third-party model training on that data.

## Data Protection

The production reference deployment protects user data through the following controls:

- HTTPS/TLS protects browser, Apps Script, service, and provider traffic in transit. Google Cloud's
  managed encryption controls protect application data stored in Cloud SQL and Firebase at rest.
- Short-lived Firebase identity tokens are verified server-side. Per-conversation ownership checks
  and RTDB rules prevent one user from reading another user's stored data.
- Cloud SQL accepts runtime access through the IAM-authenticated connector with no authorized
  public networks. Dedicated service accounts and a non-superuser serving role enforce least
  privilege.
- Credentials are stored in Secret Manager, never in browser code or application logs. Gemini is
  reached with the service account's own Google Cloud identity; no model API key exists.
- Request, row, rate, and storage limits bound exposure. Production logs exclude raw request bodies,
  questions, conversation history, generated SQL, credentials, spreadsheet rows, source values,
  and full exception messages.
- Human access to user content is prohibited except with explicit user permission for support, for
  a necessary security or abuse investigation, or to comply with law.

## External LLM Processing

The open-source default is `EXTERNAL_LLM_ENABLED=false`. When the operator enables it, Prereasoner
uses one external model: Google's Gemini on Vertex AI, called in the operator's own Google Cloud
project under that project's service account. Deploying the chat service enables it for the engine
as well. The reference hosted deployment uses Gemini for the conversational assistant, tool
orchestration, presentation, ambiguity handling, explicitly requested reference-cell generation,
and the engine's query fallback. Depending on the feature, a Gemini request can contain:

- the user's message and conversation history;
- attached table names, columns, and contents for the chat assistant;
- entity names and existing reference-table cells;
- generated SQL, result columns, and up to 40 result rows; and
- trimmed reasoning or tool output.

When the engine cannot build a query for a question on its own, it can ask Gemini to reword the
question once or to propose one SQL query. That request contains the question, the table and column
names, column types, foreign keys, and up to three example values per column. The engine still
checks, runs and labels any query that results, and the database computes every number.

The deterministic SQL path computes every answer: Gemini never executes a query or calculates a
result. Google's handling of this data is governed by the operator's Google Cloud agreement and the terms
and data-governance commitments that apply to Vertex AI in that project. Do not make training,
retention, residency, or deletion claims on Google's behalf unless they are verified against the
applicable agreement and configuration.

The server-side `EXTERNAL_LLM_ENABLED` switch is authoritative for every Gemini call. When false,
gated endpoints refuse the call and the engine runs without its fallback. Self-hosters can
therefore run the deterministic engine without an external model.

The architectural target is to replace external presentation and orchestration with a locally
operated model once a candidate passes the repository's quality, latency, security, and cost gates.
That migration is an operator responsibility and must be transparent to users: it must not create a
new popup or expose provider selection as part of the analysis workflow. Calculation semantics,
SQL execution, and verification remain deterministic.

## Retention and Deletion

Stored conversations expire after 90 days of inactivity by default (`CONVERSATION_RETENTION_DAYS`,
bounded to 1-3650 days). Reopening, querying, or saving a conversation refreshes that expiry. The
default per-user limits are 100 durable conversations and 256 MiB of serialized source tables plus
workbook state. Each persisted workbook snapshot is limited to 1 MiB. These limits bound application
storage; PostgreSQL backups follow the operator's separately configured backup policy.

Conversation deletion removes owned PostgreSQL metadata, its per-conversation schema, and RTDB jobs
indexed to that conversation. Delete-all removes the verified Firebase user's conversations and
entire `/runs/{uid}` subtree. A configured RTDB deletion failure aborts the operation rather than
reporting privacy deletion as successful.

When RTDB is enabled, new trace jobs carry a seven-day expiry by default
(`RTDB_TRACE_RETENTION_DAYS`, bounded to 1-365 days). Terraform creates one daily Cloud Run retention
job for PostgreSQL conversations and RTDB traces. Deployments managed outside Terraform must run
`python -m engine.retention_cleanup` with database access and, when RTDB is enabled, Firebase Admin
credentials on the same cadence. Older traces without expiry metadata require a one-time operator cleanup.

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
