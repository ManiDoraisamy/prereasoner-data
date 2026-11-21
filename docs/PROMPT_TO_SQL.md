# From question to SQL

This walkthrough follows one own-data request through Prereasoner. The important boundary is simple:
every query that runs is a typed AST the engine built, validated and rendered itself. An external
closed-choice model, the Decisions API (Gemini, called only through [`engine/llm.py`](../engine/llm.py)),
answers closed questions whose options the engine built and returns probabilities only: it never writes
text, SQL or a program. The engine's decoder builds every program from the reading, the verifier runs
each one, and one more closed question weighs the programs that returned rows. No model writes SQL.

We trace **`"total amount for East"`** over an uploaded sheet of orders (Region, Product, Amount, Date).

---

## Stage 1 - the request's tables are one workbook

Every request table carries its **grid** ([`engine/tables.py:table_grid`](../engine/tables.py)): an
uploaded sheet's CSV as plain rows, blank rows kept and every cell typed as the planner types cells, or a
table built from rows (a saved reference, an enrichment table, a Spider database table) as its header and
rows. The table's own rows are not it: they drop blank rows and canonicalize the header, which destroys the
structure the sheet compiler reads. Each table is one tab of the workbook, named as the table is.

[`engine/prepared.py:prepared_workbook`](../engine/prepared.py) prepares the workbook once per content and
keeps it: [`engine/sheet`](../engine/sheet) compiles the tabs into tables (headers, sections, printed
totals, families of copies), one Decisions call types the text dates
([`engine/program/dates.py`](../engine/program/dates.py)), the decoder's indexes are warmed and each
table's targets are listed for the stock reading.

## Stage 2 - the question is read

The stock reading ([`engine/program/stock.py`](../engine/program/stock.py)) scores the program language's
named coordinates: the engine's word reader (digits, periods, the sheet's own words) plus stock bge-m3's
similarity of each target (a table, a column, a value, a printed line's label) to the question's phrases.
It is a shortlist: it decides which options the reading record offers.
[`engine/program/closed.py`](../engine/program/closed.py) builds the record (the sheet's description, and
per slot a closed question over those options, plus the added questions on stated numbers, periods,
written days, years, value counts and row formulas) and one Decisions call answers it; a second answers
the value sets when the value question has an answer, and a third, beside them, reads the words the
question offers as a comparison's two outcomes. The fused reading weighs each API probability against the
stock reading.

## Stage 3 - the decoder builds programs, the verifier runs them

The typed k-best decoder ([`engine/program/decoder.py`](../engine/program/decoder.py)) builds the 50 most
probable programs of the reading in the program language
([`engine/program/language.py`](../engine/program/language.py)): a shown cell, an aggregate, a breakdown,
a rank, a calculation of two of them, or a check. Each program means one typed query
([`engine/program/lowering.py:to_query`](../engine/program/lowering.py)) over the request's store tables
([`engine/program/store.py`](../engine/program/store.py)), on the request's one database:

```
SELECT SUM("_t0"."c2_n") AS "value" FROM "_t0"
WHERE ("_t0"."_role" = 'record' AND "_t0"."c0_f" = 'east')
```

The verifier ([`engine/program/verifier.py`](../engine/program/verifier.py)) runs each one and records
named evidence on its result. The programs that returned rows are the **listed candidates**, each with its
read-as line in the sheet's own words ([`engine/program/output.py:read_as`](../engine/program/output.py)):
`the total of Amount on "orders" where Region is "East"`.

## Stage 4 - one choice, per answer

One more Decisions call weighs the listed candidates (each its read-as line and a preview of its answer,
and "none of these"). Per answer, not per candidate: a candidate's probability is summed over the listed
candidates returning the same answer ([`engine/program/closed.py:choose_answer`](../engine/program/closed.py)).
The likeliest answer is **answered** at 0.575 or more, **warned** from 0.42, and **asked back** below that
or when "none of these" is likelier.

[`engine/tables.py:select_query`](../engine/tables.py) records the outcome as a
[`PoolSelection`](../engine/sql_rank.py): the listed candidates as the pool (each a
[`ScoredQuery`](../engine/sql_candidate.py): its typed AST, its SQL, its answer's probability and its
read-as line as evidence), ranked by that probability; the decided candidate as `selected` (with
`warned`, and the coordinates the runner-up answer reads differently as `uncertain`); the likeliest one as
`declined` when the path asks back. A question longer than 30 words
([`engine/decomposition.py:ONE_QUERY_WORDS`](../engine/decomposition.py)) that the path asks back about asks, in an
analysis, for the decomposition proposal; one it answers is answered.

## Stage 5 - serve, warn or ask

In an analysis, as every served request is, the chosen program's one plan
([`engine/program/lowering.py:to_plan`](../engine/program/lowering.py)) runs through both deterministic
emitters on the request's database (`TableQuery._execute_deterministic`), in exact decimals, and the rows
are shown in the sheet's own spelling. The response's `read_as` is the turn's "read as" line. A warned
answer carries `uncertain` (`coordinates`, `also_read_as`), and the reply gives it as one the engine is not
sure of ([`engine/answer_presentation.py:terminal_reply`](../engine/answer_presentation.py)). When the path
asks back, nothing is served and the reply says how the likeliest reading read the question
([`engine/tables.py:unsure_clarification`](../engine/tables.py)).

---

## Why this shape

- **Interpretable**: the reading behind the answer is shown as its "read as" line, and the selection
  record shows how many candidates were listed and the probability of the answer served.
- **Deterministic where it computes**: the decoder, the lowering and both emitters are deterministic; the
  only external input is the Decisions API's probabilities over closed options.
- **Valid by construction**: every candidate is a well-typed AST over the request's own tables, so the
  planner cannot emit malformed SQL, and a value a filter names is one the sheet holds.

In a served request the workbook is first connected
([`engine/program/connected.py`](../engine/program/connected.py)): the world layer's links for the request
(each routed place column's bridge, each column of named entities, the keys between the request's tables,
the money columns and their rates; [`engine/knowledge_bridges.py:request_links`](../engine/knowledge_bridges.py))
add to each table the columns they reach, `city → country`, `city → country → continent`, `customer → tier`,
and, when one closed question finds the currency the question wants its answer in, `amount → USD`. The
decoder lists them as any column. In the served chain a question the world path claims (a place it filters on,
a world column it projects, a conversion it binds: `engine/knowledge_tables.py`) is answered there first; the
connected columns serve the questions it passes on and the compose probe's reading.

What the path does not do: the program language reads one table at a time, so two tables no key with unique
values links (and a Spider schema's foreign keys, which its evaluator does not link) are answered from one
table or asked back.

## World queries

`"total amount in France"` over an upload of cities is a **world** query: `France` is not a value of the
upload, so it can only be reached by resolving `city -> country` against the knowledgebase. The shared
router ([`engine/routing.py:route`](../engine/routing.py)) detects a necessary world dependency and lets the
`ComposeEngine` build a view stack (`world_join -> world_filter -> group_agg`); the world bindings lower into
the same deterministic plan. See [`docs/ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Where to look in the code

| Concern | File · symbol |
|---|---|
| The request table's grid | `engine/tables.py` · `table_grid`, `normalize_tables` |
| The workbook's preparation, the request database | `engine/prepared.py` · `prepared_workbook`, `RequestDatabase.program_tables`, `RequestDatabase.answered` |
| The question answered | `engine/program/answer.py` · `prepare`, `answer` |
| Closed questions and the per-answer decision | `engine/program/closed.py` |
| The one Decisions client | `engine/llm.py` · `decide` |
| Programs, their typed AST and plan | `engine/program/language.py`, `decoder.py`, `lowering.py` |
| The selection record | `engine/sql_rank.py` · `PoolSelection` |
| Serving entry point (select, plan, execute) | `engine/tables.py` · `select_query`, `_serve_ast`, `serve` |
| Own-data vs. world routing | `engine/routing.py` · `route`, `compose_owns`, `reads_upload_whole` |
