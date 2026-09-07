# Dataset Semantics — design spec (v1 APPROVED SCOPE, not yet implemented)

Status: scope agreed 2026-09-07 after external review; implementation awaits an explicit go.
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

Everything else abstains. In particular: a REAL currency column in the upload beats conversation
metadata (SRC data outranks conversation claims); a metadata op naming a missing table/column is
rejected with a clarify; no op ever changes a cell.

## Where it runs

A tool-using chat turn is two Sonnet rounds (tool request, then final prose). The op rides the
EXISTING first round — the query tool's schema gains `dataset_ops` next to `question` — so v1 adds
ZERO new model calls. The orchestrator validates ops against the closed grammar, persists them
with the conversation, and passes them to the engine with the tables. The true direct path
(`?chat=0`, no model in front of the engine) stays deterministic: it accepts already-persisted
metadata but never mints it.

Marginal cost: ~50-150 output tokens on a round that already runs. Gates: `EXTERNAL_LLM_ENABLED`
and the existing paid-call budgets, unchanged.

## Determinism and provenance

- The engine remains the only calculator: metadata feeds the SAME currency machinery a real
  currency column feeds (per-row rate, rate date, source in the conversion trail).
- The UI shows a badge on the column — `EUR · supplied by user` — never a fake data column.
- Ops persist in conversation state, so follow-ups and replays see the same effective dataset,
  and the upload/bridge content hashes include applied ops (metadata changes what a bridge and a
  cached analysis are allowed to reuse).
- Proposed owners (ownership-map rows to add when implementation starts): op schema + validation +
  application in ONE new engine module; emission inside the existing orchestrator turn
  (`orchestrator/system_prompt.py` contract + tool schema); persistence with the conversation.

## Later, separately: the upload normalizer

Reshaping messy uploads (totals rows, banner headers, matrix layouts) stays a SEPARATE design,
deferred until a real messy-upload example lands. Sketch retained from review: runs once per file
at ingest, cached by content hash; ops like `drop_rows`/`promote_header`/`unpivot` need stable row
identities, duplicate-header handling, and cell-preservation checks that row-count arithmetic
alone cannot provide — which is exactly why they are not in v1. Input representation when it
happens: a bounded parsed-grid sample WITH merge ranges, cell types, formulas and sheet names
(the client already parses .xlsx via SheetJS); flattened CSV loses the layout evidence.
