# From question to SQL

This walkthrough follows one request through Prereasoner. The important boundary is simple:
every query that runs is a typed AST the engine validated and rendered itself. Models contribute
evidence: an encoder reads typed signals about the question and the tables, and a small SQL
proposer suggests candidate queries. A suggestion is text until the importer maps it into the typed
AST and the validator accepts it; only then can it compete, and a fitted arbiter picks the winner.

![The search half of the pipeline: the encoder supplies typed signals, a deterministic search builds typed ASTs, and the chosen AST renders to SQL. Stages 4 and 5 below add the proposer's candidates and the arbiter.](img/readout-to-sql.svg)

We trace **`"total amount in France"`** through the stack. This happens to need public world data,
but the own-data planning steps are the same for an ordinary table question.

This world-data example follows the SQL serving path. Grounded world bindings and supported own-data winners also lower into
one shared plan that emits a SQL view stack and readable ORM/Python stages. The backend is selected
by request context, after planning. See [DETERMINISTIC_EMITTERS.md](DETERMINISTIC_EMITTERS.md) for
that path and its limits; the world example here does not demonstrate Python world execution.

---

## Stage 1 - the question becomes typed signals

The encoder is asked one thing: what does each part of the question and each column of the data mean?

[`engine/encoder_overlay.py:_question_readout`](../engine/encoder_overlay.py) builds a small graph of **units**:

- one node per **schema column name** — `city`, `amount`, …
- one node per **question word** — `total`, `amount`, `in`, `France`

Each unit's text is embedded by **Qwen2.5-0.5B + a LoRA adapter** (896-dim), then the trained *relational
readout* runs for **11 layers** (`cfg = {in_dim: 896, H: 384, nc: 90, n_edge: 10, layers: 10}`; `nL = layers + 1`).
We keep the **last layer**:

```python
final = self._layers(units, x)[-1]     # final[unit_index] -> a vector over the anchors
```

`final` is a **matrix**, not a token sequence. Each row `final[unit]` is a ~100-length vector over the model's
**anchors**: **90 schema.org-property dims + 10 intent dims**, each squashed to `[0, 1]`. A row is a *fingerprint
of meaning*:

| unit | reads as | which anchors light up |
|---|---|---|
| column `amount` | a **type** | `monetaryAmount` / measure ≈ .94 |
| column `city` | a **type** | `address` / place ≈ .91 |
| word `total` | an **intent** | `intent_agg_sum` ≈ .88 |
| word `France` | an **intent** | `filter` (equality) ≈ .86 |

The *column* fingerprints are exactly what the `/api/dimension` endpoint returns. The *word* fingerprints hold
the intents: [`read_op_model`](../engine/encoder_overlay.py) reads the aggregate operator straight off the verb
(`intent_agg_sum` fires on "total"/"sell", `intent_agg_count` on "how many") — **from the model, not a keyword
list**.

## Stage 2 — the matrix becomes role signals

[`engine/tables.py:ast_semantic_signals`](../engine/tables.py) splits the question into **role phrases**
(projection / aggregate / filter / group / order — see `semantic_role_phrases` in
[`engine/sql_rank.py`](../engine/sql_rank.py)), re-encodes each phrase in the same 384-d metric space, and
**cosine-matches** it against every column's vector. The result is a structured
[`SemanticSignals`](../engine/sql_rank.py) record — *which column plays which role, and how strongly*:

```
aggregate: SUM -> amount      filter -> France      projection: ∅      group: ∅
```

## Stage 3 — a deterministic search assembles a typed AST

This is the step people expect an LLM to do by "writing SQL". Prereasoner instead **searches over typed trees**.
[`engine/sql_search.py:SQLSearcher.search`](../engine/sql_search.py) runs a bounded beam search that *constructs*
candidate queries out of the frozen dataclass nodes in [`engine/sql_ast.py`](../engine/sql_ast.py):

```
SelectQuery(
  select   = ( SelectItem( Aggregate("SUM", ColumnRef("orders","amount")) ), ),
  from_table = "orders",
  where    = Comparison( ColumnRef("orders","country"), "=", Literal("France") ),
)
```

The node types are a real grammar: `SelectQuery`, `SelectItem`, `Aggregate`, `ColumnRef`, `Comparison`,
`BooleanExpr`, `OrderTerm`, `Join`, `ScalarSubquery`, `InPredicate`, … Each candidate is wrapped as a
[`ScoredQuery(query, score, evidence, features)`](../engine/sql_candidate.py) — the `evidence` tuple is the
human-readable trace (`"extrema:projection"`, `"aggregate:SUM(...)"`, ...). The search orders its pool with
hand-written, named ranking rules.

## Stage 4 — the proposer adds candidates the grammar rules missed

A bounded search only finds shapes its rules enumerate. [`engine/sql_proposer.py`](../engine/sql_proposer.py)
covers that gap with a Qwen2.5-0.5B LoRA adapter fine-tuned on Spider TRAIN gold SQL. It reads one compact prompt
([`engine/sql_prompt.py`](../engine/sql_prompt.py)):

```
-- schema
orders(order_id, city, amount)
-- question
total amount in France
-- sql
```

and decodes **four deterministic beams** (beam search, no sampling). Each beam's first line goes through
[`engine/sql_import.py:import_sql`](../engine/sql_import.py), which maps it into the same `SelectQuery` nodes
or raises `Unsupported`; the validator and renderer then run exactly as for search candidates. A line the
importer cannot map, the validator rejects, or the renderer cannot reproduce is dropped, so model text never
reaches a database. Accepted proposals join the pool after the search candidates; a proposal identical to a
search candidate is not added twice but marks that candidate `proposer:endorsed`.

## Stage 5 — the arbiter chooses among the queries that run

[`engine/tables.py:select_query`](../engine/tables.py) runs every pooled query on an in-memory copy of the
tables (SELECT guard, fixed step budget); a query that fails is out. The proposer then scores each remaining
query's likelihood under its prompt, and the arbiter ([`engine/sql_rank.py:SQLArbiter`](../engine/sql_rank.py))
computes one number per query:

```
score = sum over 9 features of (value - mean) / scale * coefficient  +  intercept
```

The features are the likelihood, its length and per-token average, the pool score and position, whether the
search, the proposer, or both produced the query, and the pool size. The highest score is served; the earlier
pool position breaks ties. The response's `planner.selection` lists the winner's feature values and each
feature's contribution to its score, so a reader can see why it won.

## Stage 6 — render the tree to a SQL string

The winning AST is rendered to `SELECT SUM("amount") FROM "orders" WHERE "country" = 'France'`, guarded
(SELECT-only), and executed.

---

## Why this shape (the payoff)

Because models only **read signals and suggest candidates**, and the engine **validates, arbitrates and
renders the tree**:

- **Interpretable** — you can see the per-column typing (the matrix), the per-node `evidence` for every
  candidate, whether the search or the proposer produced the winner, and the arbiter's per-feature arithmetic.
- **Deterministic** — the same input yields byte-identical SQL. This is enforced by a cross-process repeatability
  test in [`tests/test_routing.py`](../tests/test_routing.py).
- **Valid by construction** — every candidate is a well-typed AST that passes constraint checks before it can
  win, so the planner cannot emit malformed SQL.

Contrast with a pipeline that executes a decoder's SQL directly: a decoded column or table that does not exist, or
syntax outside the grammar, is simply one rejected suggestion here, and a suggestion that imports still has to beat
the search's candidates on the arbiter's recorded score. The proposer is small (0.5B), so it adds coverage without
becoming the authority.

## The one caveat in this example: world queries

`"total amount in France"` is a **world** query — `France` is *not* a value in the uploaded cities, so it can
only be reached by resolving `city → country` against the Wikidata-backed entity store, with
Schema.org-named typing evidence. The shared router
([`engine/routing.py:route`](../engine/routing.py)) detects a **necessary world dependency** and lets the
`ComposeEngine` build a **view-stack** (`world_join → world_filter → group_agg`) instead of a single
`SelectQuery`. The selected world bindings then lower into the same deterministic plan used by SQL and Python.
For a purely own-data prompt like `"how many customers in Paris"`, you get the single `SelectQuery` exactly as
drawn above (`SELECT COUNT(*) … WHERE city = 'Paris'`). For an analytical own-data prompt such as a top-N or
share, ComposeEngine may supply a multi-view binding; explicit `use=py` or `use=both` lowers that binding through
the same Python/SQL emitter pair rather than accepting an opaque SQLite answer.

See [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) for how routing decides own-data vs. world, and
[`docs/SQL_AST.md`](SQL_AST.md) for the planner's search phases in depth.

---

## Where to look in the code

| Concern | File · symbol |
|---|---|
| Build the unit graph + run the readout | `engine/encoder_overlay.py` · `_question_readout` |
| Operator/intent off the question verb | `engine/encoder_overlay.py` · `read_op_model` |
| Per-column typing (the anchor readout) | `engine/dimension.py` · `analyze` (the `/api/dimension` view) |
| Role phrases → per-column role signals | `engine/tables.py` · `ast_semantic_signals`; `engine/sql_rank.py` · `SemanticSignals`, `semantic_role_phrases` |
| The typed AST node grammar | `engine/sql_ast.py` · `SelectQuery`, `SelectItem`, `Aggregate`, `Comparison`, … |
| The search that assembles the tree | `engine/sql_search.py` · `SQLSearcher.search` |
| One scored candidate | `engine/sql_candidate.py` · `ScoredQuery` |
| Proposer: beams + likelihoods | `engine/sql_proposer.py` · `SQLProposer.propose`, `SQLProposer.likelihoods` |
| Model text → typed AST gate | `engine/sql_import.py` · `import_sql` |
| Pool merge + arbiter | `engine/sql_rank.py` · `merge_proposals`, `SQLArbiter`, `PoolSelection` |
| Serving entry point (select, render, execute) | `engine/tables.py` · `select_query`, `_serve_ast` |
| Own-data vs. world routing | `engine/routing.py` · `route`, `compose_owns` |
