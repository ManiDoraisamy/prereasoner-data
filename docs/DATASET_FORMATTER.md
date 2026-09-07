# Dataset Formatter — design spec (PROPOSED, not implemented)

Status: awaiting approval. Nothing in this document is built; the ownership map does not yet list
these owners. The open questions at the bottom need answers before implementation starts.

## The problem, from a real transcript (2026-09-07, formfacade-leads)

The user asked "total budget in Europe" (answered: 62,000), then: *"This is in euros. Whats in
USD"*. The engine HAS the ECB exchange rates in the knowledgebase — the customer-orders demo
converts with them — but the `budget` column carries no currency, so the conversion machinery
never engages. The user's message *is* the missing metadata, and today it dies as conversational
context: the reply was "I don't have an exchange rate on hand," which is false about the system
and true about the dataset.

The second face of the same problem is upload shape. A `.xls` or Google Sheet formatted for human
eyes — merged headers, a totals row, a remarks column, matrix layout — is not the tidy
one-row-per-record CSV the engine ingests. Those need reshaping once, at upload.

## Architecture: split by LIFECYCLE, not by function

Two Sonnet responsibilities, two different lifetimes, therefore two different call sites — but
never two calls per turn:

| Responsibility | When it runs | Cost profile |
|---|---|---|
| State completion (standalone question) + instruction-derived dataset ops | Every turn, ONE combined call | pays the shared context once |
| Upload reshaping (tidy-format a messy sheet) | Once per file, at ingest | cached by content hash; amortizes to zero |

**Per turn, one call.** The question rewrite and the instruction-derived ops both need the same
context (conversation, schema, sample rows) — that shared input is the bulk of the tokens, so two
calls pay it twice for no latency win, and two calls can *disagree* (the rewrite keeps "in US
dollars" while a separate formatter guesses GBP). One model turn = one interpretation. The
orchestrator's existing turn extends to return both:

```json
{ "question": "total budget in US dollars",
  "dataset_ops": [{ "op": "set_column_hint", "table": "responses", "column": "budget",
                    "hint": {"currency": "EUR"},
                    "basis": "user: 'This is in euros'" }] }
```

**At ingest, one cached call.** Reshaping is a property of the FILE, not the turn. It runs when
the sheet lands, keyed by `sha256(file content)`, stored with the conversation so no later turn
re-pays it. This reuses the same hash-gate pattern as the bridge ledger (`_bridge_state`).

## The rule that outranks everything: ops, never data

Sonnet emits **small typed operations from a closed, versioned grammar**. It never emits a
rewritten dataset. Re-emitting CSV through a model is slow (output tokens), unbounded at the
5,000-row cap, and can silently corrupt cells — which breaks the product's contract that answers
are auditable derivations over the user's actual data.

Proposed v1 grammar (deliberately minimal — each op exists because a motivating case exists):

| Op | Motivating case | Deterministic effect |
|---|---|---|
| `set_column_hint` | "this is in euros" | attaches semantics (currency/unit) consumed by the existing currency machinery; no cell changes |
| `drop_rows` | a totals/remarks row in a .xls | removes named rows before ingest |
| `promote_header` | header on row 3 under a title banner | selects the header row |
| `unpivot` | months-as-columns matrix layout | wide→long reshape with named id/value columns |

Everything else abstains: an op outside the grammar, a hint conflicting with an existing column
(a real `currency` column beats a hint), or a reshape the applier cannot verify row-count
arithmetic for → clarify, never silent application.

## Determinism, provenance, and where the pieces live

- The APPLIER is deterministic engine code: validate the ops, apply them before ingest, and
  surface every applied op as a derivation step under the docs/SHEETS_AS_REASONING.md grammar —
  the euros hint renders as a provenance chip (`kind: instruction, source: conversation`, the
  quoted user message as basis), a dropped totals row renders as a step showing what was removed.
  The user always sees what was done to their data and why.
- The engine stays the only calculator. `set_column_hint` feeds `currency_intent` /
  the ECB conversion path exactly as a real currency column would; the conversion trail already
  knows how to show the rate and its publication date.
- Ops are recorded in the conversation state, so follow-up turns and the replayed trail see the
  same effective dataset. The bridge/upload hash inputs must include applied ops (a hint changes
  what the bridge should contain).
- Proposed owners (ownership-map additions, pending approval): op schema + applier in ONE new
  engine module; the per-turn emission inside the existing orchestrator turn (extending
  `orchestrator/system_prompt.py`'s contract); the ingest-time call in the upload path with its
  hash cache. No second planner, no second SQL path.

## Cost and latency

Per-turn: the combined call replaces today's rewrite-only turn — same round trip, ~1–3k input
tokens (context it already pays), ~100–300 output tokens for ops. Marginal cost ≈ the ops tokens.
Ingest: one call per uploaded file (~2–5s for a messy sheet), then cached; zero on every turn
after. The rejected alternative — two per-turn calls — doubles input cost for equal-or-worse
latency and adds an interpretation-divergence failure mode.

## Gates

`EXTERNAL_LLM_ENABLED` covers both call sites (operator switch, per the privacy rules — no user
consent ceremony). Request budgets reuse the existing paid-call gates. A turn with the LLM
unavailable degrades to today's behavior: no ops, engine answers or clarifies from the data as-is.

## Open questions (need answers before building)

1. **Direct-path Sonnet.** The /reason direct path currently has NO model in front of the engine.
   Instruction-derived ops there would add one. Acceptable, or should ops exist only on the
   orchestrated path (where a model already runs) and the direct path keep clarifying?
2. **Hint vs materialized column in the UI.** Show the euros hint as a badge on the `budget`
   column, or materialize a visible `currency` column in the sheet? (The applier supports either;
   provenance rendering differs.)
3. **v1 op set.** Is the four-op grammar above the right starting floor, or should v1 ship
   `set_column_hint` alone (the euros case) and add reshaping ops when a real messy-upload case
   lands?
4. **Ingest trigger for xlsx.** The browser already parses .xlsx client-side (vendored SheetJS).
   Should the formatter see the raw parsed grid (preserves merged-cell/layout signal) or the
   flattened CSV the client produces today?
