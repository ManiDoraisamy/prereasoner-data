# Sheets as reasoning — the derivation-trail contract

The workbook is not a query debugger: **the sheet stack IS the reasoning**. Each sheet is one
materialized step a careful spreadsheet user would have built by hand, ending in the one-cell
Result. If a user reads the tabs left to right they must be able to re-derive the answer with
no hidden inputs. Every emitter of views (`engine/compose.py`, the conversion trail in
`engine/knowledge_tables.py`, any future path) and the renderer (`web/public/lib/workbook.js`)
follow this contract. Do not restate these rules elsewhere; change them here.

An uploaded table belongs to the conversation and keeps its canonical CSV stem (`orders`, `customers`). A
conversation may contain several named analyses over those shared inputs. Each completed analysis revision stores
the exact returned SQL, rows, and provenance. The UI shows short logical tab labels, while the wire-level derived
view names are prefixed by the analysis slug: `total_sales_combined`, `total_sales_filtered`, and
`total_sales_total`. This makes traces unambiguous without turning the tabs into long machine names.
Supported own-data analyses, named or explicitly selected by `use`, also expose these stages as readable Python; the
dual-source contract is defined in [DETERMINISTIC_EMITTERS.md](DETERMINISTIC_EMITTERS.md).

## The step grammar

A trail is a subset of these steps, always in this order, each one a real sheet:

| # | op | sheet name (UI label) | appears when | must show |
|---|----|----|----|----|
| 1 | (upload) | the user's sheet names | always | the uploaded rows, untouched (green: the AI never writes here) |
| 2 | `join` | `combined` | **two or more uploaded sheets are actually joined** | the joined row set with the columns of every participating sheet |
| 3 | `world_join` | `knowledgebase_lookup` (renders as "enriched") | a knowledgebase reference table is joined | the base rows **plus every reference column any later step uses** (e.g. `country`), badged with the reference source |
| 4 | `filter` / `world_filter` / `time_filter` / `having` | `filtered` | rows are dropped | the kept rows; the step label names the human-readable condition |
| 5 | `convert` | `calculated` | per-row arithmetic (e.g. currency) | each input value beside the exact factor used (rate + its publication date) and the derived column, so the Result is visibly that column aggregated |
| 6 | `group_agg` / `topn` / `sort` / `yoy` / `running` / `divide` / `share` | `total` / `top_results` / … | the final shaping | the aggregate the Result overlays |

## The rules

1. **Executed SQL only.** A sheet's SQL is the statement that actually produced its rows —
   `con.execute(view_sql)`, rows off that cursor. Never a prettified or reconstructed query.
2. **No forward references.** Any table or column a step's SQL joins, filters, or projects must
   already be visible to the user: on an earlier sheet, or browsable in the **Reference** tab
   (the knowledgebase table the lookup used). The reference-lookup sheet exists precisely so a
   later `WHERE "city"."country" = …` filters a column the user has already seen.
3. **No no-op sheets.** A step that neither adds a visible column nor changes the row set is not
   shown. Concretely: `combined` requires ≥2 uploaded sheets in a real join — a world lookup is
   NOT a combine; a single-sheet trail starts at the reference lookup (or at `filtered` when no
   reference is needed).
4. **Projection may narrow, usage may not widen.** A later sheet may drop columns it no longer
   needs (`calculated` need not re-show `country`), but it may never *use* a column that no
   earlier sheet displayed (rule 2).
5. **Entity values display as human labels.** Reference columns store QIDs (`Q142`); displayed
   rows and step labels resolve them (`France`) via `knowledgebase."words"`. The SQL text keeps
   the storage literal — the SQL is the proof, the label is the explanation, and the step label
   bridges them (`where country = 'France'`).
6. **Result overlays the last sheet.** The final aggregate is its own sheet (`total`) so the
   per-row `calculated` grid is never replaced by a single number.
7. **Provenance on every column.** Each sheet's columns carry their source badge — SRC (user
   upload), KB (knowledgebase reference), FX/AI (derived) — from the server-authored
   `column_provenance`; the UI never guesses.
8. **One grammar for every path.** The compose engine, the world-grounded conversion trail, and
   any future emitter produce the same ops, names, and ordering above. If a path cannot express
   its work in this grammar, fix the path, not the grammar.
9. **Named revisions are immutable.** `modify` creates the next revision under the same analysis id; it does not
   overwrite the prior response. `create` receives a distinct engine-owned id and a collision-free slug.
10. **A workbook link is exact.** “Reasoning steps for total sales” links to both the analysis id and revision.
    Selecting it retains the source and private-reference tabs and replaces only the derived stack. Stale analyses
    are marked when a source table changes and must be recomputed before their old values are treated as current.
11. **Dual emitters stay stage-aligned.** When an analysis is in the dual-emitter subset, every
    SQL view and Python `View` has the same slug-prefixed name and consumes the immediately prior
    stage. Operators are visible in the generated function at that transition. Grouping is one
    named `group_reduce` stage even though its implementation maintains an in-memory group map.

## Verification checklist (run against a live conversation)

- [ ] Tabs read left to right as a derivation: uploads → (combined) → (reference lookup) →
      (filtered) → (calculated) → total, with Result overlaid on the last.
- [ ] No `combined` tab when only one sheet was uploaded.
- [ ] Every column named in any sheet's SQL WHERE/JOIN/SELECT is visible on an earlier sheet or
      in the Reference tab.
- [ ] Step labels are human-readable (no bare QIDs).
- [ ] The Result equals the last sheet's aggregate; for conversions, the `calculated` column
      recomputes by eye (amount × rate).
- [ ] Provenance badges name the true source (SRC/KB/FX), not a generic AI.
- [ ] A modify follow-up keeps the analysis id and increments its revision; a distinct question creates a new id.
- [ ] Each historical rail link restores its exact result while the input-table tabs remain present.

Registered in `CLAUDE.md`'s ownership map. Demo datasets under `web/public/dataset/` are the
standing fixtures for this checklist.
