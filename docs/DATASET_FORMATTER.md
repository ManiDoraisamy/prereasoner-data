# Dataset Semantics — design spec (v1 IMPLEMENTED 2026-09-07)

Status: v1 is implemented in the serving code. Owners: `engine/dataset_semantics.py`
(grammar/validate/replay/apply), `engine/dataset_attestation.py` (orchestrator-to-engine trust),
`engine/conversations.py` (persistence, chat migration v4), the orchestrator tool round (emission),
and `workbook.js` (badge). Acceptance scenario: the euros transcript below — 58,000 EUR (Europe)
converted to USD with the rate in the trail, and the claim re-applied on a follow-up turn.
Formerly "Dataset Formatter"; renamed because v1 is deliberately NOT a data-cleaning system — it is
a metadata layer. Upload reshaping is a separate, later design ("upload normalizer", sketched at
the bottom).

## The problem, from a real transcript (2026-09-07, formfacade-leads)

The user asked "total budget in Europe" (answered: 62,000), then: *"This is in euros. Whats in
USD"*. The engine HAS the ECB rates and refused correctly — but for the right reason badly stated.
The enriched view even showed a `currency WIKI` column (Germany→EUR, US→USD, Japan→JPY): that is
knowledgebase."Countries" reference data about each COUNTRY, not the denomination of the uploaded
`budget` values, and treating it as the denomination would have converted row 3 as USD and row 4
as JPY — silently wrong. The engine already distinguishes these two meanings: in the
customers-orders demo it converts from the upload's own `currency SRC` column while ignoring the
`currency WIKI` one (verified live: €970 → $1,127.33 with the rate in the trail).

What is missing is only a channel for CONVERSATION to supply a SRC-grade fact: "the budget
column is denominated in EUR."

| Metadata | Meaning |
|---|---|
| `budget.currency = EUR` | every uploaded budget value is denominated in euros (v1 adds this) |
| `country.currency = USD/EUR/...` | reference fact about the country (already exists, WIKI) |
| conversion target `USD` | requested output currency (already parsed from the question) |

## v1 grammar: two operations, nothing else

```json
{ "op": "set_measure_metadata", "table": "responses", "column": "budget",
  "metadata": { "currency": "EUR", "date_column": "submitted" },
  "basis": { "source": "conversation", "text": "This is in euros." } }
```

- `set_measure_metadata` — attach denomination semantics to a measure column. `date_column` is
  optional: when present, each row converts at the ECB rate for ITS date; when absent, the engine
  uses the request's `as_of` date and must disclose that choice in the trail.
- `clear_measure_metadata` — users correct themselves ("actually those were GBP"). A later `set`
  replaces the effective value; the audit history retains every operation.

Every operation carries a `basis` object with `source: "conversation"` and the user's quoted text.
The orchestrator checks the quote against the current user message and signs the exact operation
list together with the authenticated principal. The engine verifies that HMAC and persists its own
`attested: true` marker. A model field or direct browser request cannot create that marker.

Everything else abstains. In particular: a REAL currency column in the upload beats conversation
metadata (SRC data outranks conversation claims); a metadata op naming a missing table/column is
rejected with a clarify; no op ever changes a cell.

## Where it runs

A tool-using chat turn is two Sonnet rounds (tool request, then final prose). The op rides the
EXISTING first round — the query tool's schema gains `dataset_ops` next to `question` — so v1 adds
ZERO new model calls. The orchestrator verifies the user quote and passes a principal-bound
attestation; the engine validates the closed grammar and table binding, then persists the operations
with the conversation. The true direct path
(`?chat=0`, no model in front of the engine) stays deterministic: it accepts already-persisted
metadata but never mints it.

Marginal cost: ~50-150 output tokens on a round that already runs. Gates: `EXTERNAL_LLM_ENABLED`
and the existing paid-call budgets, unchanged.

## Determinism and provenance

- The engine remains the only calculator: metadata feeds the SAME currency machinery a real
  currency column feeds (per-row rate, rate date, source in the conversion trail). Each active
  measure gets its own private synthesized source column internally, so two monetary columns in
  one table cannot borrow each other's denomination.
- The UI shows a badge on the column — `EUR · supplied by user` — never a fake data column.
- A supplied `date_column` is passed into the rate binder and is the date used for each row's rate;
  an absent date column uses the request's `as_of` date.
- The orchestrator verifies the basis quote against the current user message before the engine
  accepts it. Dataset-operation history is bounded to 200 operations / 64 KiB per conversation.
- Persisted claims are re-validated when a previously detached sheet returns, so a replacement
  upload cannot be shadowed by stale metadata or a changed schema.
- Ops persist in conversation state, so follow-ups and replays see the same effective dataset,
  and the upload/bridge content hashes include applied ops (metadata changes what a bridge and a
  cached analysis are allowed to reuse).
- Ownership is split deliberately: grammar, validation, replay, and application stay in
  `engine/dataset_semantics.py`; `engine/dataset_attestation.py` owns authenticated transport; the
  orchestrator emits and verifies quotes; persistence stays in `engine/conversations.py`;
  `workbook.js` renders the effective state.

## Later, separately: the upload normalizer

Reshaping messy uploads (totals rows, banner headers, matrix layouts) stays a SEPARATE design,
deferred until a real messy-upload example lands. Sketch retained from review: runs once per file
at ingest, cached by content hash; ops like `drop_rows`/`promote_header`/`unpivot` need stable row
identities, duplicate-header handling, and cell-preservation checks that row-count arithmetic
alone cannot provide — which is exactly why they are not in v1. Input representation when it
happens: a bounded parsed-grid sample WITH merge ranges, cell types, formulas and sheet names
(the client already parses .xlsx via SheetJS); flattened CSV loses the layout evidence.
