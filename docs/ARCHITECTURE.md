# Architecture

Status: **current runtime architecture**. Future changes are marked as targets or pending work;
they are not shipped features. See the [documentation map](README.md) for the difference between
current, opt-in, external, and planned behavior.

Prereasoner represents a question, its data, and its source evidence as named dimensions. The current
runtime composes those dimensions into a checked query plan, runs it, and returns the result with its
rows and trace. For the supported own-data subset, one immutable plan emits both a SQL view
stack and readable SQLAlchemy/Python source; see
[DETERMINISTIC_EMITTERS.md](DETERMINISTIC_EMITTERS.md). One frozen encoder (Qwen2.5-0.5B with a LoRA
adapter and a relational readout) supplies model evidence: it reads intent and schema signals as named
coordinates. No local model writes SQL. A deterministic search builds every own-data query as a typed
AST, and selection serves the best-ranked one that runs. Only when no candidate runs, and the operator
enabled Gemini, may Gemini rewrite the current question for the deterministic search; Gemini cannot
propose SQL, and the answer is labelled (see
[Labelled Gemini fallback](#labelled-gemini-fallback)). No model writes Python or a numeric answer, and
model text never reaches a database. AST construction, routing, joins, validation, selection,
emission, and execution are deterministic for fixed inputs, configuration, database state, and model
files. An answer the fallback supplied also depends on Gemini's reply.

Read [GETTING_STARTED.md](GETTING_STARTED.md) first when setting up the repository. Read
[SQL_AST.md](SQL_AST.md) for planner internals, [SOURCE_DATA.md](SOURCE_DATA.md) for
publisher-owned references, and [../db/README.md](../db/README.md) for the database contract.

## System Boundaries

```text
browser, Google Sheets add-on, or MCP client
        |
        | authenticated request: tables, question, optional conversation and analysis intent
        v
engine/server.py
        |  bounded JSON parsing, canonical request validation, auth
        |  conversation/analysis ownership and private-reference selection
        |  private-reference adapter
        v
engine/knowledge.py
        |  one serving entry point
        v
engine/routing.py
        |  a question the search reads whole from the upload builds no compose plan and needs no
        |  world lookup (reads_upload_whole, asked of the search probe's pool)
        |
        +-- DELEGATE --> own-data typed AST, local composition, or ordinary world lookup
        |
        +-- COMPOSE  --> world-dependent multi-step view composition
        v
guarded execution in the conversation schema
        |  supported shared plan: bounded Python/SQL, or both for parity
        |  other plans: SQL; explicit Python/verification rejects the fallback
        |
        +-- engine/provenance.py: typed expression and source lineage
        +-- response: rows, SQL, evidence, provenance, and source releases
        +-- optional Firebase RTDB progress events
```

The optional `orchestrator/` service wraps this API in a conversational tool loop. `mcp_server/` exposes the same
engine operation to MCP clients. Neither component owns data reasoning or may invent a numeric result. A chat turn
is normally one Gemini call at LOW thinking, whose tool call carries the question, the analysis action and its
slug; a second call follows only when the engine asks for a split or cannot read a follow-up. Each call waits at
most 20 s and is sent once more after a timeout, a 429 or a 5xx (`engine/llm.py`): Vertex's shared pool holds a
single request for a minute or two now and then while its median stays under 2 s.

## Execution backend boundary

`engine/deterministic/plan.py` owns the shared topologically ordered stage DAG, table identity, scalar attribute names, and
join-edge selection. The emitters own only source generation. `service.py` chooses the backend and
owns a shared database snapshot when given an engine; `runtime.py` loads generated modules, executes
temporary SQL views, and compares materialized stage rows. Production uses the existing PostgreSQL
conversation and knowledgebase schemas in either mode.

The request's `use` preference flows from the browser through `/chat` or `/api/reason` without changing
process environment. `context.py` creates a transient `query` context for every direct request, or uses
the named analysis slug and revision. The server checks the final response before saving an
analysis: explicit Python/verification requests cannot accept a legacy SQL fallback. Successful
responses identify the requested and actual mode; the MCP adapter preserves that evidence.
The knowledge-query adapter also preserves the own-data delegate's generated program and stages.
Its coverage check reads the complete emitted SQL program so a filter in an earlier stage is not
mistaken for a condition missing from the final aggregate. A word the tagger reads as a finite verb or
an adverb ("which item sold the most units") is covered, because it says what the rows did, and so are a light
verb's participle ("made"), the rows an aggregate is taken over ("the average score of the leads") and an
adjective an ordering grades ("the most expensive event"); other participles, words the data holds as values,
and the payment and listing states are still checked. A month word is covered when the SQL compares a date
column with that month or date (`engine/sql_dates.py:realized_month_words`), and a duration's words when the SQL
compares a span by them (`engine/sql_durations.py:realized_duration_words`).

The own-data `TableQuery._serve_ast` winner, world planner's grounded slots, and selected
ComposeEngine primitive records have separate lowering adapters under `engine/deterministic/`.
They feed the same plan, emitters, and runtime; none is another planner. The routing authority still
requires a necessary world dependency before ComposeEngine owns a world answer, but an explicit
`python` or `verify` request can lower a selected local composition as well. World ORM relationships
use persisted cell-to-QID associations and actual knowledgebase tables. Composition retains its
SQLite candidate-materialization step for operand binding/routing, then runs the selected typed
program through the shared runtime. Unsupported shapes still fail closed for explicit Python/verification.

`engine/decomposition.py` is the only compound-question fusion boundary. A normal request always tries
the ordinary planner first. If its selected AST is compound, the engine returns a non-terminal
`decompose` status without answer rows. The orchestrator may make one engine-triggered retry with a
closed natural-language proposal: two to four leaf questions, one to four `cross`/`anti_join` merge
nodes, one output, and a stated grain. Runtime guards require the exact original question and analysis
request identity, reject proactive or repeated decomposition, reject dead nodes, and bound Cartesian products
by explicit leaf limits. A leaf may keep only a row cutoff the decomposed question states, so a proposal
cannot meet that bound by inventing one. The existing AST planner independently binds every leaf; common dimension keys
for an anti-join come from those typed relations, and the evidence relation must preserve every physical
dimension in the left input's grain: an evidence leaf serves the first of its contract-compatible
readings, in ranked order, that does. The result is one `AnalysisPlan`, not independently
generated SQL and Python and not a model-executed chain of partial answers.

Temporary SQL views are evaluated when read, whereas Python stages retain tuples. Current trace
collection materializes every stage in either mode, so SQL mode is not a bounded-memory streaming
executor. `auto` prefers Python for an estimated total of at most 10,000 input rows and SQL above it.
The Python combined query and every materialized stage enforce that cap; underestimated expansion or
another Python failure rolls back a savepoint and records an emitted-SQL fallback. The threshold is a
deployment policy and must continue to be checked against production-shaped benchmarks.

## Data Scopes

The PostgreSQL deployment uses five distinct scopes:

| Scope | Schema | Contents |
|---|---|---|
| Wikidata shared data (legacy names) | `knowledgebase`, `public` | Current resolution index, taxonomy, Wikidata-backed entity tables, and staging/geo data; target migration is described below |
| Synchronized reference sources | See `SOURCE_DATA.md` | Nine publisher-owned schemas with active physical releases; logical datasets remain separately deployment-gated |
| Application metadata | `chat` | Admin-migrated conversations, named analyses and immutable revisions, source manifests, ownership, quota, activity, and expiry tables; serving receives DML only |
| Conversation | `c_<32hex>` | Uploaded tables under their canonical CSV stems and persisted world-resolution bridges |
| User | `m_<md5(subject)>` | Reusable private reference tables |

The verified Firebase subject determines the user scope. A client-supplied conversation id is accepted only after
an ownership check. Client input never selects a user schema directly.

Private references are not added wholesale to a SQL `search_path`. `engine.master.relevant_tables()` reads every
saved key, applies the production foreign-key detector to uploaded and saved tables, and selects only references
connected to the request. Selection runs to a fixed point, so a valid multi-hop chain can be included. A selected
reference larger than the request's row budget (`engine.server.MAX_REFERENCE_ROWS`) keeps every row whose key the
working tables name, fills the budget with the others in key order, and says in a warning what it left out; one
naming more rows than the budget is not used, and a warning says so. Selected references then become ordinary typed
planner tables; there is no reference-specific SQL generator. The detector compares keys as the executed join does
(`engine.relations.join_keys`: a column of numbers by exact magnitude, any other column by each cell's exact text),
so a reference the uploads match only when case is ignored is not joined; the same detector over case-folded copies
names it in a warning.

## Semantic Contract: Vocabulary, Observations, And Facts

Keep these three layers separate:

1. **Schema.org is the vocabulary.** The pinned Schema.org 30.0 contract defines the classes,
   properties, inheritance, domains, and ranges that training and serving can name. A coordinate
   can be represented even when the current corpus has no examples for it.
2. **Source-owned datasets supply observations.** Wikidata properties are mapped to Schema.org
   properties, and publisher relations are exposed through explicit source mappings. Wikidata is
   the main public entity-identity bridge; it is not the authority for facts owned by IANA, CLDR,
   GeoNames, ECB, CDC, NLM, or another publisher.
3. **Versioned source releases supply answer facts.** Models learn how a question and a column fit
   together, not mutable values. A model may recognize an exchange-rate relation, but the dated rate
   still comes from a pinned ECB release and must pass temporal and calculation checks.

There are currently two learned stages over this contract. The shared encoder uses a historical
71-property compatibility basis derived primarily from capped Wikidata observations, plus
structural, intent, and calculation training. The active named-dimension head uses the newer
multi-source Schema.org corpus and emits calibrated class proposals consumed by `engine/router.py`.
It can propose a resolver family but cannot authorize a join or change a numeric answer. These are
distinct artifacts, and their corpus and metric claims must remain separate. See
[TRAINING.md](TRAINING.md) and [MODEL_CARD.md](MODEL_CARD.md).

## Domain Semantics And Enrichment

Market-led domain profiles and deterministic shared-reference enrichment are specified in
[KNOWLEDGE_ENRICHMENT_ROADMAP.md](KNOWLEDGE_ENRICHMENT_ROADMAP.md). The active publisher
inventory is recorded in [SOURCE_DATA.md](SOURCE_DATA.md). A physical source release being
active does not make it planner-visible. `iana_country` is the first code-approved logical
dataset in the generic enrichment registry. Raw Terraform still requires
`ENRICHMENT_ACTIVE_DATASETS=iana_country`; the guided Community deployment sets it after the
database grant step. The raw Terraform default remains empty.
The production request flow now contains the guarded integration boundary: explicit intent,
domain-role recognition, database-backed registry selection, request-local materialization,
trusted-edge propagation, and serving provenance. Currency conversion is a deliberately
bounded exception to the still-disabled generic temporal registry: the ECB sync is transformed
offline into a release-labelled daily `knowledgebase.exchange_rate` projection, and the world
planner joins dated facts on the exact `(currency, date)` pair. It carries the prior published
business-day value only during projection construction, never through request-time network or
latest-prior SQL. The calculation verifier proves direction and aggregate arithmetic; missing
coverage fails closed. Each row is converted before the rows are combined, so a conversion is a SUM
(`total_<ccy>`) or an AVG (`average_<ccy>`) as the question asks; either is computed per the uploaded
column or world attribute the question groups or ranks by, and its evidence records that grain. No embedded or demo FX fixture is a production fact source.

The target source-materialization design makes the database boundary explicit: physical shared
schemas are source-owned (`wikidata` after its pending migration and the publisher schemas in
`SOURCE_DATA.md`), while domain roles describe request-private operational tables and classify
source datasets in the planner registry. Each new publisher source owns a versioned `release`
table. The target `public` schema contains no application data, but the current runtime still uses
legacy `public` Wikidata staging and geo tables declared by `db/init.sql`. `engine.relations` remains the
relationship-graph owner, and the existing typed AST remains the only SQL planner. Enrichment does not add a router,
planner, or request-time network path.

Existing Wikidata country, currency, timezone, city, and administrative-area tables migrate from legacy
`knowledgebase` and `public` into source-owned `wikidata`. IANA and CLDR facts remain in `iana` and `cldr`; they are
linked to Wikidata by explicit registry edges and are not copied into domain schemas. Each source fact has one
writable owner and each exposed relation has one planner role.
The measured baseline found incomplete/mixed Wikidata code data and empty timezone staging, so M1 uses pinned IANA
and CLDR snapshots. Each implemented refresh inserts a complete immutable release and activates it atomically;
upserting into active reference rows is prohibited because it retains upstream deletions and breaks deterministic
replay. The legacy Wikidata schema migration is still pending.

## Request Flow

1. `db/init.sql` plus the privileged application migration command prepare shared `chat` metadata before deployment.
2. `engine.request_validation` validates both engine and orchestrator request shapes. It bounds questions, history,
   table count, per-table size, and aggregate size, then converts display names into one 34-byte canonical table
   identifier. Names that collide after canonicalization are rejected before parsing or paid inference.
3. A client uploads a conversation's sheets once per change: `POST /api/conversation/sync` stores them (starting the
   conversation when there is none) and returns their `source_hash`. A question then names them by
   `conversation_id` and `source_hash` instead of carrying them; `/chat` reads the stored copy for its own checks and
   names it on each engine call, and the engine answers 409 with the stored hash when the sheets were replaced since,
   so the client uploads again. `engine.server` verifies the Firebase principal and ownership on every call and
   parses each stored snapshot once per instance (an inline upload in the request is stored the same way). A request
   repeated with
   the jobId of one the same principal already sent is answered with that request's response, waiting while it
   still runs (`engine.request_replay.DurableResponseReplay`: one `chat.request_job` record shared by every engine
   instance, kept ten minutes after its response and removed when the user deletes a conversation); the chat's
   engine client repeats a request once, with its jobId, only when the response is lost in transport. Browser XLSX
   parsing is isolated in `web/public/lib/xlsx-worker.js`, which uses a vendored SheetJS build with compressed, expanded,
   row, column, worksheet, and time limits. Host grids (the Excel add-in's and the Google Sheets add-on's cells)
   are written as the workbook those cells would export to and take the same worker, limits and layout rule, so a
   sheet reads exactly as an upload of it would. `sheets-addon/` is a read-only Apps Script add-on. Its sidebar is
   its own UI in Sheets' style and renders turns with the web rail's shared component (`web/public/lib/turn-renderer.js`).
   Live steps come from the web's subscriptions (`web/public/lib/firebase-init.js`), and the sidebar reads the cells
   with the upload importer (`WORKBOOK_IMPORT.convert`). Its Apps Script server returns the visible, non-empty tabs'
   cells and calls `/chat` and the conversation APIs server to server with the user's Firebase identity. It does not
   implement reasoning and never writes to the spreadsheet.
4. `engine.master` validates or selects private references. `engine.relations.discover_fks()` is the canonical
   relationship detector used here and by planning (`relate` merges declared keys with it: a declaration blocks a
   discovered parent for its column only where its name names its own parent at least as well), and
   `engine.relations.join_keys` is how it and join grounding (`engine/sql_grounding.py`) read a column's keys. Within
   one request, discovery, the planner's schema and the schema graph are each derived once
   (`engine.relations.memoized`).
5. `engine.server` resolves the conversation id and verifies ownership before selecting its working schema.
   Uploaded data changes advance a monotonic dataset version and mark prior analyses stale. `engine.pg` hashes
   each materialized working table and leaves unchanged uploaded, private-reference, and enrichment tables in place.
6. For named requests, `engine.analysis` validates the create/modify/inspect intent and
   `engine.conversations` reserves the engine-owned analysis id and revision. An inspect request loads an exact
   completed revision without invoking the planner.
7. `engine.knowledge.KnowledgeReasoner` receives the complete working table set and the question. Tables that are
   copies of one layout (an export kept per product or month) are searched as one, and the answer names the table
   it read (`SchemaGraph.layout_copies`).
8. `engine.routing.route()` makes the single world-composition ownership decision. A necessary world
    dependency may be owned by ComposeEngine; self-contained data remains authoritative in the typed AST,
    while selected local analytical compositions can still be lowered into the shared plan for explicit
    SQL/Python execution.
9. The selected planner emits a typed query. Supported shapes lower to one shared plan and deterministic SQL/Python
   programs; the request policy executes bounded Python, SQL, or both against the conversation schema plus the
   explicitly reachable shared knowledge tables. A selected compound own-data AST may request the one bounded
   decomposition retry described above; all leaf planning and merge binding remains inside the engine. Other
   unsupported shapes remain on guarded, quoted, read-only SQL.
10. Cross-route calculation verifiers inspect typed planner evidence before a result is released. Without changing
   scores, the shared registry selects the best-ranked candidate that realizes every detected calculation. An
   unmet or ambiguous calculation replaces the numeric result with a structured clarification.
11. `engine.provenance` maps typed output expressions to their exact table/column operands and propagates source
    identity through emitted views. Publisher adapters provide release IDs. The browser renders this contract and
    never classifies a column by its name.
12. A successful named request prefixes every derived view with the canonical analysis slug, stores the exact
    response in an immutable revision, and returns the engine-owned descriptor. A failed or clarified request does
    not replace the last completed revision.
13. The engine returns rows, SQL, route evidence, intermediate views, and provenance. Trace writes are best effort
    and do not determine the answer. Every emitted view stack follows the one derivation-trail contract in
    `docs/SHEETS_AS_REASONING.md` (step grammar, no forward references, executed program only); emitters extend that
    grammar in place rather than inventing per-path sheet shapes.

## Request And Provenance Contracts

There is one request validator, `engine.request_validation`; the orchestrator imports it directly. A table
display name never reaches SQL directly: `engine.request_validation.canonical_table_name()` removes known file
extensions, normalizes to ASCII, reserves space for runtime bridge suffixes, and hashes overlong names. The engine
revalidates orchestrator calls at its own trust boundary.

Provenance has two inputs. Source records describe uploaded tables, selected private references, and activated
publisher snapshots. Computation records come from the selected typed AST and name each output expression,
operator, and qualified operand. Intermediate view records propagate those origins through the emitted operation
stack. `column_provenance` is aligned positionally with `columns`; an absent or malformed record is not inferred by
the browser. A quantity the engine computed (an aggregate or arithmetic output) carries `measure: true`; the reply
renderer (`engine/answer_presentation.py`) formats only those numbers and writes a value taken from the data, such
as a year or an ID, as it is. Provenance explains the computation that ran. It does not certify that the selected query was the
correct interpretation of the question.

## Own-Data SQL Planner

The own-data path serves one typed SQL AST chosen from the deterministic search's candidates
(`engine/tables.py:TableQuery.select_query`), reading a question's typographic apostrophes and quotes as the ASCII ones, a comparison symbol before a number as its
words and "prior to" as "before" (`sql_expansion.plain_marks`). No local model writes or scores SQL:

| Owner | Responsibility |
|---|---|
| `engine/sql_ast.py` | Immutable query nodes, type/visibility validation, rendering |
| `engine/sql_schema.py` | Typed schema graph and deterministic join-tree enumeration |
| `engine/sql_search.py` | Projection, filter, aggregate, grouping, order, limit, and base candidate expansion |
| `engine/sql_dates.py` | Calendar phrases ("in August", "after August 10, 2026") as typed comparisons on a date column |
| `engine/sql_durations.py` | Duration phrases ("more than 6 months") as comparisons of how long each row lasted (`DateSpan`) |
| `engine/sql_recursive.py` | Subqueries, `EXISTS`/`IN`, set operations, and self-join shapes |
| `engine/sql_constraints.py` | HAVING, disjunction, and relationship constraints |
| `engine/sql_extrema.py` | Row, aggregate, frequency, and zero-inclusive extrema |
| `engine/sql_parsimony.py` | Bounded projection/table variants of pooled candidates (minimal join, binding, drop/add column, operand swap, DISTINCT) |
| `engine/sql_rank.py` | Search ranking features (`CandidateRanker`), the pool contract (`SEARCH_CANDIDATES`, `execution_op_limit`), and the selection record (`PoolSelection`, `FallbackRecord`) |
| `engine/sql_grounding.py` | Pool eligibility: text literals that fit their column, and joins the foreign keys allow; which SUM or AVG reads only rows its joins repeat (a selection preference) |
| `engine/tables.py` | Planner facade (`select_query`), SQL guard, pool and local SQLite execution |
| `engine/question_rewrite.py` | The labelled, stateless Gemini wording rewrite; deterministic search still owns SQL |
| `engine/sql_prompt.py` | The schema text and rewrite prompt sent to Gemini |
| `engine/decomposition.py` | Closed model proposal validation and fusion of planner-selected leaf ASTs into one shared DAG |

1. The deterministic search builds up to 25 validated candidates (`SEARCH_CANDIDATES`) and orders
   them with named rules; the encoder contributes table, column-role, and structural-profile
   similarities. Rows grouped by columns that repeat, joined to the rows they count, are grouped by
   their own key too (`engine/sql_expansion.entity_groups`; in the search only beside the name of a
   table the question says, so a shared job title stays one group), and a table holding two keys to one
   other table is read from its owner's key (`engine/sql_rank.relation_sides`): "high schoolers who
   have 3 friends" own the Friend rows, and the two named Jordan count apart. An average per the rows'
   own noun ("average amount per order" over a table of orders) averages the rows, and a number the
   question names no field for, after a noun naming a table, compares that table's money measure
   ("orders over 20": amount, not unit price). In a workbook of one table a count counts its rows
   whatever the question calls them ("which country has the most leads"). One aggregate word takes
   every field it coordinates, after "of" too when the words before it are both fields' ("the average
   age of losers and winners", "the number of different series names and contents",
   `sql_search._coordinated_targets`), several aggregate words coordinated before one field share it ("the
   average, minimum, and maximum age", `sql_rank._heads_phrase`) whatever field an aggregate word itself names
   (a stadium's Average), and a "number" that begins a field's name with the rest of the
   name after it asks for the field ("the number of tickets" of Num_of_Ticket; "No" and "Num" read
   "number"). A word of a field's name said in another form names the field, by the shared stem reading the
   completeness check reads words by (`sql_expansion.says_in_another_form`): "the arriving date" date_arrived,
   "their directors" Directed_by, unless the word names another field itself ("the gradepoints" is no Grade); and a
   word of seven letters or more spells out a three- or four-letter name word that begins it ("account balance"
   acc_bal, "latitude" lat; `sql_expansion.spells_out`), unless it names a table ("students" is no ENROLL.STU_NUM). A
   field whose first word abbreviates its table's name (cust_name, stu_gpa), or whose name is its table's word but for
   its kind (date_of_treatment), is named beside its table: "their apartment numbers", "the date of treatment". A
   sort verb opening the question is its command ("Sort employee names by their age"), and "number", "all" or
   "each" in lower case is no cell value. A nation is a country, a store a shop and a possessive its word ("Brazil's
   population" states 'Brazil'; `sql_schema.canon`); a field of
   one-letter codes holds the words they stand for only with the field-kind evidence for M/F and L/R
   (`sql_schema.code_word_matches_column`: sex/gender, hand/side/direction/orientation). A sampled Size 'M' or Stage F/M
   is no sex. The qualified-code constructor uses the same rule, and completeness witnesses the word from the typed
   comparison on that field, including aliases and LOWER, rather than a matching literal elsewhere in the SQL.
   Explicit codes and the existing binary Y/N reading remain available. A participle before
   a compared year ("produced in 1980") is read by the answer; and a count of "different winners" counts the
   entity's name or key before the other fields the noun names. "In total" after "how many" restates the count,
   and a field whose words another field of its table spells whole is not named inside that spelling ("the ranking
   date" is no ranking). The ranker credits no field the question does not ask to show as the group it asks when
   the answer shows a field the question asks of the same entity (`CandidateRanker._named_beside`). The
   completeness check reads a requested field by its name without its kind too ("role" of role_code), never by the
   question's command ("Return ..."). A count counts the rows of the table the noun after its count word
   names ("how many countries"), a field whose first word abbreviates its table's name is shown where the question
   says the table (`sql_expansion.abbreviated_table`: "the id of students" is StuID), and of shown fields ending in a
   word said once, the one whose owner the question never says is not asked. A sentence after the first that lists the
   outputs ("List the continent id, continent name and the number of countries.") names them, their owner and their
   order once for construction, ranking, the answer's order and the completeness check (`sql_expansion.output_requests`,
   `request_fields`), and so does an opening sentence that asks for two or more coordinated outputs ("What are the
   names, themes, and number of singers for every concert?"), the owner decided over all items before an item prefers
   the field its words name exactly, and so does a clause a list verb opens after a comma ("..., and return the name,
   location and district of each shop"; `sql_expansion.instruction_starts`), a possessive opening its items naming
   their owner; "along with", "together with" and "as well as" add an item as "and" does, a list asking for fields
   and a count asks the count in each of those fields' groups, and the reading showing exactly the list's fields starts
   ahead of one adding a field; "find" is a list verb, and an item followed by "of" and the rows of one table realizing
   it is that table's field; an item's last word may stand for the kind its field's name ends in ("the cell phone" is
   cell_number), and a "which" after a preposition opens a relative clause, not a command. An answer shows each output
   a list names where it lists a count beside fields, or fields each its own; values a list joins ("the continents Asia
   and Europe") are no outputs. An answer showing only
   fields the question says only to filter on gets a twin showing
   the name of the rows it lists (`SQLSearcher._display_twin`, `sql_rank.filter_only`: "the cities whose population
   is between ...").
   A key named as the table it refers to names that table: "the name of the project for which a scientist ... is
   assigned to" shows Projects.Name, not also AssignedTo.Project (`SQLSearcher._simplified`).
   Fields asked again in the same words (`sql_expansion.repeated_requests`: "List the id, name and id.") retain each
   output occurrence in construction and list ordering; two requests one field could realize ask for two fields;
   the checker requires each occurrence of an unambiguous repeated field, resolving table aliases. List ordering
   can copy an already bound column value; anonymous count slots never create another count. A row count drops a
   leaf join to a table nothing reads, nothing counts and the question does
   not name (`sql_expansion.counted_noun_tables`), which only multiplies the counted rows.
   For a simple scalar COUNT(*) with one named population, construction and
   admission share `sql_expansion.scalar_counted_table`: a potentially multiplying join counts the population's proven unique,
   non-NULL identifying column distinctly, retaining the qualifying joins. This bounded policy excludes grouped,
   ranked, aliased and distinct-request readings; field counts and threshold populations retain their own policies.
   `count_preserves_population` retains plain row counting when every joined key identifies at most one row and the
   root is the counted population, preserving qualification joins without unnecessary distinct counting.
   These identity/cardinality proofs concern the current request's table values; changed data rebuilds the graph.
   `AnalysisPlan.AggregateValue.distinct` represents distinct counting once for both emitters, with an explicit
   `COUNT_DISTINCT` accumulator/finalizer in Python; AST lowering accepts the scalar single-column COUNT form.
   The verb a sentence opens
   with is its command and names no table (`sql_expansion.command_positions`: "Show the names of conductors" joins no
   show table), and a relationship self-join is built only where a word of its key besides the parent table's name is
   said, whole or abbreviated (`sql_recursive`: "the course whose prerequisite is ..."). A grouped
   answer that names a group as a field to show lists its fields in the question's
   order (`sql_search._in_question_order`); a joined listing whose join repeats its rows gets a DISTINCT twin, and
   the ranker prefers it (`sql_rank.repeats_shown_rows`), and a field the
   question names only to filter on is not shown, nor one it names only in the phrase that orders the
   rows ("the first name of students in the descending order of age"), nor a field named as its table
   where the question says that name only after "of", as the owner of the fields it asks for ("the
   carriers of devices"). A count over an exclusion counts the rows kept ("how many dogs have not gone
   through any treatment"), a count beside a comparison with the average counts the rows on its side,
   in the groups the table's own field makes ("the number of cities in each district whose population
   is greater than the average"; `sql_expansion.average_comparison`), and a number said beside a
   numeric field's name is that field's value ("high schoolers in grade 10", "a cat that is age 3"),
   unless the shared `sql_extrema.ranking_cutoff` reader reserves that occurrence for a ranking ("the mountains
   with the top 3 prominence"). A separate occurrence of the same number can still supply its own filter.
   "The full name" of rows holding a first and a last name is both fields, unless a field is named
   for it in that same table (`SQLSearcher._full_names`); an unrelated table's FullName cannot suppress the
   requested table's name parts. A name's part a field abbreviates is listed where the question
   says its two words ("the first name and age" lists Fname first), and "the full name" lists the first name before the
   last, wherever else the question says a part. A field the question compares
   before naming it ("projects that require between 100 and 300 hours") or picks the row by after
   "with the highest" is not shown, nor an aggregate it says only in a relative clause keeping the
   groups ("the studios whose average gross is above 4500000"), for which the search builds the
   reading without it (`sql_search._condition_twin`); "cities with total sales over 100" still shows
   each total. Questions opening with "how many" or "count" must show COUNT in an output, including a projected
   scalar query, or SUM of an explicitly requested stored-count field (`query_contract._shows_count`, sharing
   `sql_expansion.counted_measure_columns` with the operand reader). An unrelated aggregate in a filter,
   ordering, HAVING or source subquery supplies no count output.
2. Every candidate runs on an in-memory SQLite copy of the request's tables under the SELECT guard
   and a budget of SQLite VM steps that grows with the cells of the tables it reads: 20 a cell, at
   least 10,000,000 and at most 100,000,000 (`execution_op_limit`). The Gemini rewording's pool runs
   on the same copy and reruns no query the first pool ran. A query that fails
   cannot be chosen. Neither can one that tests a text column against a literal the column never
   holds while another column does (`engine/sql_grounding.py`): a SQL model once wrote
   `customer_name = 'Lyon'` for "Lyon customers". Nor can one that equates two columns the foreign
   keys keep apart, between tables they connect: two foreign-key columns of different keys, or a key
   and a column that shares no value with it. A SQL model once joined
   `orders.order_id = products.product_id`, skipping the `order_items` bridge; the query ran and
   matched no row. Eligibility is not evidence that the query answers the question.
3. The best-ranked eligible candidate is served. A date the question names keeps the choice to
   the candidates that realize it, when one does (`engine/sql_dates`). A candidate whose SUM or AVG
   reads only rows its joins repeat is served only when every eligible candidate does
   (`engine/sql_grounding.double_counted`): the best-ranked reading of "the total Amount by Plan and
   Currency" once summed a report's Total Amount once per subscription the report row matched. A
   registered calculation intent (`engine/calculations`) takes the best-ranked eligible candidate
   that satisfies it, and a money noun that names its table ("what's the sales in London") takes the
   best-ranked eligible candidate that aggregates a money column
   (`engine/sql_expansion.money_total_columns`). These preferences,
   the single-branch serving contract, and the decomposition leaf contract filter the ranking; none
   rescores it. A served top-1 ranking keeps every row tied with its first row (`sql_ast.keep_ties`,
   rendered with `RANK() OVER`), unless it shows only what it is ordered by: its tied rows show the same values.
4. For a named request, the search's top candidate is read first, before any candidate runs: a
   set operation (a compound question) requests decomposition instead of executing a single query.
   Otherwise one dual-emitter branch serves the request: the best-ranked eligible single query.

A candidate reads a question word (`engine/query_contract.py:read_question`) when the word names a table
the query reads or a column of one, with the tables and the columns it uses split as the search splits names
("LifeExpectancy" is "life expectancy", `engine/sql_schema.name_words`), or spells such a name in two words
("high schoolers"); when it is a value the query compares; a spelled number realized in its role (a cutoff in a
question that ranks is the query's LIMIT, "the two oldest"; after a comparison cue it is a compared number); an
order word in a query that orders its rows, and "alphabetical" only over an order on a text field, A to Z
unless reversed; a participle relating the rows to a compared value ("departing from APG") when the data holds
the participle as no value and the value has one relationship to the rows, or, with several (a departure and
an arrival airport), when the column relating it carries the participle's travel direction
(`engine/sql_rank.travel_direction`); a graded word the query realizes in its direction on a field of its kind
(a comparative before "than" is a comparison with its operator, complemented when negated,
`engine/sql_expansion.comparative_operator`; "the most common" orders groups by their count; any other superlative
orders by, or takes the extreme of, a field in `engine/sql_extrema.superlative_direction`, so "the youngest" is the
least age or the latest birth date and "the highest rank" the smallest one; an adjective naming its dimension ("the
tallest", `sql_extrema.SUPERLATIVE_DIMENSIONS`) is read by a field of that dimension of the rows its noun names, where
no field is named at it; and "the maximum" after "with", "has" or "have the" selects the row holding it
as "the highest" does, `engine/sql_expansion.selects_extreme`); or when it is grammar, a counted noun naming the rows
counted, or one of the closed lists
the check keeps. A word that names only some other table's column is unread. The query also reads, though nothing of
it spells them (`query_contract._witnessed_words`): a travel direction by the column of that direction ("flights
departing from APG" by SourceAirport); a name's part a field abbreviates ("first name" by Fname); "name" by a field
named as its table is (airlines.Airline); a verb relating two tables it reads, one named before the verb and the
other after; a word sharing a stem of five letters or more with a table or field it reads ("students enrolled" by
Student_Enrolment), a word of an operation excepted; "one" after a determiner ("the one whose name"); a passive
participle after a copula whose subject it reads, with nothing after it but "in the world" ("How many different degrees
are offered?"), unless the data's names spell it ("How many regions are affected?" names affected_region); a verb
relating a table named before it to a field it reads named after it; "of origin" after a field it reads; "first" and
"last" grading a date as "earliest" and "latest" do; and a noun right after the one-word name of a numeric field it
reads, which is what the field measures ("the minimum snatch score"); "member" after the name of a table it reads,
its rows ("the staff member"); and the people a population it reads counts ("How many people live in Gelderland
district?" by Population, `sql_expansion.counted_population`). A superlative or aggregate word that is a numeric field's
whole one-word name grades the field named after it and asks for no field of its own ("the highest capacity" asks for
no Highest), and before a noun a superlative or an ordering grades it is the field and asks no aggregate ("the highest
total score", `sql_expansion.cue_names`). "Available"
asks nothing where no table or field is named for availability, nor do "the results" a sort verb orders, and an
ordering "from old to young" is read by an ordering in that grade's direction (`sql_extrema.span_grade`). A verb is
read by the field its doer names ("won" by winner_id, `closed_class.agent_noun`), "corresponding" where it pairs an
output the answer shows ("corresponding to each", a shown field after it), "names" by the title of a table holding no
name, and "time" by a date field it reads. A
lower-case verb names no stored value in the search (`closed_class.predicate_words`), a measure's trailing unit is left
unsaid in a field's name (`sql_expansion.naming_words`), and a yes/no flag field named by its words is the condition
field = its true value (`sql_expansion.flag_comparisons`). Two words
that begin the name of a table it reads are read ("the high school student" by Highschooler), as is a
word the English vocabulary lacks one letter from a word it reads, misspelled ("the cartools" by Cartoon). So is a word
the search
names a read field by ("gender" by Sex, `sql_expansion.column_matches`), "the full name" by a first and a last name of
one table, and a word saying how rows are counted by a query counting them: "times" and "records" after a word of a
count, and a verb a count follows after "by" ("the hometowns shared by at least two teachers") by one that groups them.
"Equal to" is read by a query comparing for equality, "record" after the name of a table or field the query reads
names its rows ("from the trip record"), and the words of a request for every field ("all the information") by a
query showing every field of its table.

The constraint check (`engine/query_contract.constraint_violations`) judges a set query branch by branch for the
result's shape (aggregates, projection, ordering, grain; a field an aggregate word takes, as in "top 2 cities by
total amount", is the measure the groups are ranked by and asks no grain) and as a whole for what the question asks
of the rows: a
comparison may sit in either branch of an INTERSECT, in an OR or a UNION only of alternatives the question offers, in
a HAVING COUNT for a threshold on counted rows (the only place a number the name of a table follows is met: "more
than 1 car maker", `sql_expansion.counted_thresholds`, each threshold stated needing its own count, which
construction builds as one membership test per requirement, `ConstraintQueryExpander._requirement_candidates`,
connected by the explicitly read AND or OR (`sql_expansion.count_requirement_operator`). Mixed/unbound coordination
remains unresolved. The checker also proves the joint Boolean relationship between distinct counted populations;
individually correct membership counts do not justify substituting AND for OR. The HAVING is
read as a formula, AND and OR, of the counts of the counted rows, COUNT(*) or a column of theirs, a distinct one
telling them apart, and must admit the same numbers of rows, tried at the boundaries its bounds make, over the
counted table without joins that repeat its rows or groups that hold its key; one admitting none, "fewer than 2",
only as a NOT IN of the groups failing it, which keeps the entities without rows, or the counted table grouped alone
where no other table holds what it groups by), or in an IN or EXISTS subquery
that binds the returned rows (an IN of the operand's own column, a foreign-key pair, a same-named column or one named
for its table and itself, never a computed value, and a largest or smallest value only where the question asks for
an extremum; an EXISTS that refers to the outer row, `query_contract._binds`),
and never counts from a scalar subquery or an unbound one, which filter no returned row. A count grouped by the key
of the one table it reads, which other tables refer to, counts one row in each group and is refused, and so is a
count of a group's own distinct values, a key its joins equate with the group's included (1 in every group). Groups
list each value once, so "different" is read by a grouped answer, the counts beside its groups included. A listing
answers the rows the question's opening names, after a command it opens with ("what are airlines | that have
flights"): it shows a field of such a table, of one a foreign key joins to it, or a key referring to it
(`query_contract._asked_tables`), so airport names do not answer for airlines. A "number"
ending a field's name ("the phone number") asks for the field, not a count (`sql_expansion.field_numbers`, read by
`count_requested`, the search's aggregate cues and the ranker's question roles), and so does a "number of" before a
numeric field's whole name ("the maximum number of floors", "in order of number of police officers"), the fields'
types coming from the schema; a table's name or a text field's after it is still counted. "All the information about
hiring", "all the details", "all columns" ask for every field (`sql_expansion.complete_projection_requested`), and a
superlative states the cutoff such an answer may keep. A threshold on an aggregate a thing has ("customers with a total amount
over 10", `ExpansionSupport.aggregate_thresholds`) is met only on that aggregate, and a filter moves into a HAVING
only where the aggregate's word stands before its field's name near the number ("whose average life expectancy is
longer than 72", not "the average weight of the cars with 4 cylinders"). An average the rows are compared with
("how many dogs have an age below the average") is computed by the query, in its scalar subquery. A number said right after a field
whose cells are not all numbers ("horsepower more than 150") is compared with that field and no other
(`query_contract._compares_another_field`), and a field named after its table's name ("the highest savings
balances") is that table's, not a same-named field of a table the question does not name. "How many" answered by groups a
HAVING keeps, without "each", "per", "by" or "for different" (`sql_expansion.groups_requested`), counts the groups
that qualify, one number (`ConstraintQueryExpander.qualifying_count` builds it as a COUNT over the grouped query);
a count in each group is refused. The rows a ranking keeps are read in one place by the search and the check
(`sql_extrema.ranking_cutoff`: "the top 3", "the 3 oldest", "the two people with the largest age"), and a ranking
whose number names a table shows its rows, not only the field that ranks them (`sql_extrema.ranked_table`). An
EXCEPT makes an exclusion, its subtracted branch keeping the values
excluded; a comparison there, or in a NOT IN or NOT EXISTS, realizes its complement, or itself when the question
negates before it (`query_contract._select_queries`, `_scoped_match`). An exclusion removes the rows the question
names: a table joined inside it only to filter, which the question does not name, narrows it and is refused
(`query_contract._dangling_unnamed_join`). A set difference subtracts entities by fields that tell them apart
(`SchemaGraph.identifies`: together they hold no combination twice in their own table, a missing value counting as
one; a foreign key to a unique lookup names the lookup's rows, not its own): the projected values when they do,
else the entity's key, filled in every row, NOT IN the related rows'.
A field's name inside a longer field's name the question also says ("the name of the song" is `Song_Name`) or
inside a table's name the query reads ("car makers") asks for that longer field or table: said together and in order
("first name and last name") whatever other table holds the shorter name, said apart where the longer field's table
is the one the question names as the owner after "of" ("the name of the breed", `query_contract._owner_tables`) or,
where it names none, the rows it asks for; a camel-cased name split further needs one of those ("at least 2 models ... the id" is no
ModelId). A field named as its table answers "the names of orchestras", and a self-join's shown field is its table's.
A question opening "how many" or "count" is answered by an aggregate, never a listing. The column an aggregate phrase
names most fully ("the total amount", "the total estimated amount") is its operand
(`engine/sql_rank.aggregate_operand`); when it is text and its cells are not all numbers the question is
refused naming them (`engine/query_contract.unreadable_operand`), by compose, the planner, both world routes
(`KnowledgeQuery.serve`) and the currency check alike, and no other column, a saved reference's included,
nor a row count stands in for it. `engine/routing.route` makes compose's refusal the answer in serving and
the Spider evaluator.

The response's `planner.selection` records the pool counts (`pool_size`, `executable`,
`misgrounded`, `eligible`), the served member's place among the eligible ones and its search score,
whether a date, double-counting, calculation or money-total preference chose it, and `served_by`. The winning AST then
executes against the conversation schema through the shared SQL/Python plan.

### Labelled Gemini fallback

When no candidate is eligible or the selected plan leaves request wording unresolved, and the operator
enabled Gemini (`EXTERNAL_LLM_ENABLED` plus a Vertex AI project, checked by `engine/llm.py`),
`select_query` may call `engine/question_rewrite.py` once. Gemini receives the current question and a
schema description containing table and column names, inferred types, and declared foreign keys. In the
question, the phrases the search reads as cell values are quoted, and one it reads as held inside a column's
values ("all inspection checklist") is spelled as that column containing the value
(`engine/sql_prompt.py:prompt_question`): with only names to go on, Gemini read tabs named Inspection and
Checklist into "all inspection checklist". It receives no other cell value, no full rows and no conversation
history, and it is told not to name a table the question does not name. The call asks for LOW thinking and
stops after 20 s: at the model's default thinking a rewording once outlasted the 30 s client timeout. Gemini is asked for a plain English question: comparisons of size or order in
words, never > or <, and columns without their table ("Student with Age > 20" lost the "more than" the next check holds). The
rewriter checks that numbers, quoted text, and the source values the search recognized in the question remain in
its rewrite (a value a word names may stand for the word: "Asian countries" as "countries in Asia"), and so does every source value in a script the search cannot read (東京); an ASCII cell value said as a
word ("IN", a country code) is the search's to recognize. It also keeps the computations the question asks: an
aggregate, comparison, ranking or share word, and a count as the engine itself reads one
(`sql_expansion.count_requested`); the grain a grouping word asks is held by the answer check after the reworded
search. Replies are not cached. The deterministic typed search alone builds candidates and SQL. A rewritten reading is served
only when it runs, keeps every constraint of the original question and leaves no word of the rewording
unread.

The response labels an answer served after a rewrite with `fallback.kind: rewrite`, the model name,
and the rewritten question. Gemini cannot write SQL, choose a candidate, or provide a result. When no
eligible candidate is found after rewriting, a rewrite is unsafe, or Gemini is unavailable for an
unreadable baseline, no query is served. If the best-ranked runnable reading reads some of the question's
tables, columns or values, the reply names the words it left unread and offers the columns of its tables they
could mean, numeric ones for a quantity word such as "volume" (`engine/tables.py:unread_clarification`).
Decomposition leaves continue to use `allow_fallback=False`.

`EXTERNAL_LLM_ENABLED` defaults to off, and with it off selection is deterministic and no model
writes SQL. The guided Community deployment turns Gemini on together with chat, so its engine has
the fallback. The Spider headline is measured with it off; the evaluator records the setting in its
contract ([`spider/README.md`](../spider/README.md)).

## World Grounding And Composition

World grounding handles facts absent from uploaded data. For example, a table may contain cities while the question
filters by country. The path is:

1. route a column to a grounded entity type;
2. resolve cell values to QIDs through exact normalized lookup, then embedding fallback;
3. read the required rows from the pinned, offline-built Wikidata projection;
4. persist a conversation-scoped cell-to-QID bridge;
5. join through QID foreign keys and execute the requested operation.

`engine/knowledge_query.py` owns ordinary world lookup. A non-geographic entity column (a bank, a restaurant, a
hospital) grounds through the same bridge to its Wikidata table, whose country answers a filter, a grouped or
ranked dimension ("which country has the most deposits"), and through `country.continent` a continent filter; an
uploaded column ranks inside such a filter. A quantity superlative ranks totals of the column it names ("the most
deposits") or counts the rows it names ("the most banks"), and a count of a noun a numeric column already counts is
that column's total ("how many transfers"; `engine/sql_expansion.py:ranked_measure_columns`, `ranked_row_tables`,
`counted_measure_columns`). A sheet with its own place column answers places through that column. `engine/knowledge_compose.py` and
`engine/compose.py` own multi-step operations that genuinely require a world dependency. `engine.routing.compose_owns()` is the authority;
a primitive prediction alone cannot seize a self-contained own-data question.

World lookup does not contact Wikidata at request time. Missing rows or ambiguous keys are an explicit
coverage/abstention outcome; synchronization and release activation are offline operational responsibilities.

## Named-Dimension Typing And Its Evidence

Typing uses URI-named Schema.org coordinates, and every class decision reports the computation that
produced it. `engine/schema_model.py` summarizes a table or column through the shared Qwen encoder and
emits calibrated probabilities for 80 trained property URIs. `engine/schema_decode.py` computes each
class as:

```text
sigmoid(class_bias + sum(property_probability * signed_property_weight))
```

The response evidence contains the bias, class threshold, each property probability, signed weight,
and contribution. `tests/test_schema_decode.py` independently recomputes the score and tests causal
interventions on named coordinates. This is the actual calculation path, not a generated explanation.

The compiled Schema.org 30.0 contract (`engine/schema_org.py`,
`engine/data/schema_org_v30.json`) represents all 1,521 properties and 926 classes. The promoted head
trains 80 property coordinates, validation-qualifies 56, and releases 11 classes after validation-only
selection and untouched-test checks. Unsupported coordinates and classes explicitly abstain.

`engine/router.py` is the one learned column-routing owner. It maps a released class through ontology
inheritance to a coarse resolver family. A class proposal is never sufficient for a join:
`engine/knowledge_query.py` verifies that the values ground to source keys. When the model abstains,
the inherited exact source-membership route can recover grounded coverage. This matters for geo
classes that do not yet clear model release gates. The old 71-coordinate family consensus remains in
the bundle only for shared-encoder compatibility and diagnostics; it is not a competing production
router.

Two corpus invariants are enforced at build time, because a leaking corpus trains cleanly and scores *better*
for leaking. **Splits are drawn per derivation group, never per instance:** the group is everything left of the
first `#` in an instance id, so a table, its columns, its presentation variants and its row windows share one
draw. `SemanticInstance.validate` re-derives the split and rejects any assigned value, which makes the previous
"inherit the parent's split" patch — one policy with two implementations — unrepresentable. **Every labelled
property must be evidenced in the text the encoder actually reads:** labels whose values appear nowhere, or only
past the `max_len=128` truncation point, are wrong supervision rather than weak, so the builder drops them and
packs wide relations into facets that each fit the budget. The build additionally refuses to write a corpus in
which identical text spans two splits, or in which the realized split shares drift out of bounds.

Mutable facts stay outside model weights. The model learns that `quote_currency + effective_date + units_per_eur`
is an exchange-rate shape; the rate for a date still comes from the pinned `ecb.exchange_rate` release. Class
evidence is captured through the typing buffer in `engine/knowledge_query.py` and attached to the answer by
`engine/knowledge.py`; a load or decode failure falls back to deterministic grounding and never fabricates a
classification.

## Deterministic Reference Enrichment

Publisher-owned reference facts remain in publisher-named schemas. `engine/enrichment/registry.py` is the single
policy owner and describes each logical dataset with a `DatasetDefinition`: qualified relations, identity and lookup
keys, lookup cardinality, ambiguity behavior, temporal selection, licensing, row-level restrictions, and activation.
Registration never grants planner visibility.

`engine/enrichment/store.py` resolves exactly one active source release and returns a typed `SnapshotPin` containing
the source schema, release ID, schema version, and definition hash. Reads are key-bounded, release-qualified, ordered,
and network-free. Replays may read a retired pinned release; ordinary requests resolve the active release once at the
start of enrichment.

Source capabilities are intentionally different: exact dimensions, ambiguous relations, numbering-pattern metadata,
temporal series, temporal rule sets, bounded calendars, terminology hierarchies, and rights-bearing document graphs.
`engine/enrichment/intents.py` extracts only explicit requested attributes. `engine/calculations/` owns
typed arithmetic intent, AST expansion, and post-ranking proof. Its registered specifications currently
cover currency filter/conversion, ratios including per-capita measures, and flat tax, commission, and
explicit annual one-year simple-interest amounts. The shared encoder can order eligible operands; typed
columns, unit rules, complete join keys, and all-branch evidence determine admissibility. A planner route
without typed computation evidence fails closed. `engine/currency_intent.py` remains the currency
specification's ISO syntax and `rate_to_<code>` convention, not a second verifier. Ordinary own-data
grouping does not activate enrichment.
`engine/enrichment/adapters.py` returns typed match, absence, ambiguity, policy-denial, and ineligibility
outcomes while retaining snapshot and license provenance. Temporal capabilities still abstain
because row-level temporal AST semantics are not implemented in the generic enrichment planner.
The ECB daily projection above avoids that unsupported operation by materializing exact-date rows
offline; VAT rules, holidays, and other temporal source definitions remain disabled until their
own semantics and evaluation gates are complete.

Trusted composite edges pass separately to `TableQuery.ingest`; they are not accepted from a client table payload.
`SchemaGraph` stores their ordered column pairs as one foreign key, and `sql_ast.Join` validates and renders them as one
atomic `ON a.x = b.x AND a.y = b.y` clause. Legacy scalar fields remain available to existing planner components.
Source activation has two keys: the registry definition must be code-approved and the deployment allowlist must
name it. `db/reference_grants.py` derives read-only relation grants from that same registry; a deployment must use
a non-superuser serving role. Snapshot rollback uses `db/sync/releases.py` and accepts only a previously validated
retired release.

`engine/domain_profiles.py` owns seven versioned market profiles and their Schema.org mappings;
`engine/domain_typing.py` emits conservative request-local role evidence. Dataset definitions may declare
`compatible_roles`, which is a mandatory runtime gate for domain-sensitive references. `engine/enrichment/runtime.py`
then combines role evidence with explicit request intent, value type, activation, snapshot, exact row coverage, and
policy outcomes. A match becomes an ordinary bounded planner tab plus a trusted edge. The server attaches
`provenance.enrichment` only when a dataset was actually used, so disabled/no-intent requests retain their prior
response shape.

## Private Reference Lifecycle

Reference tables have a narrow contract: the first column is a non-empty unique key and remaining columns are
attributes.

1. The browser marks an edited reference dirty.
2. Before querying, it saves dirty references through `POST /api/master`.
3. `engine.master.save_master()` validates and normalizes the shape, rejecting unsafe identifiers or keys before
   opening its write transaction.
4. The same operation atomically replaces the saved table.
5. A save failure stops the query instead of silently using an older version.
6. Deletion removes both browser state and the server-side table.

`engine/master.py` owns this behavior and its database transactions. `engine/server.py` only translates HTTP
requests and responses.

## Conversation Lifecycle

`engine/conversations.py` is the storage owner. The server mints identifiers in the fixed
`c_<32 lowercase hex>` shape, checks ownership on every reopen, update, and delete, and enforces the same shape with
a PostgreSQL constraint before an identifier can name a schema. Conversation lists use a stable
`(created_at, conversation_id)` cursor so equal timestamps do not skip rows.

The source tables in a conversation retain their canonical upload names (`orders.csv` becomes `orders`). A
content manifest prevents follow-up turns from dropping and reloading unchanged data. Replacement is serialized
with a conversation advisory lock across service instances. Derived sheets are not PostgreSQL views: they are the
executed SQL, rows, and provenance returned by the one planner and stored as an immutable analysis revision. Their
workbook identities use the analysis-prefixed name; `logical_name` and `sql` retain the exact ephemeral relation
names and statement that were executed, so naming a workbook never rewrites its evidence.

Each revision records both the conversation's monotonic dataset version and a hash of every effective planning
table, trusted relationship, and dataset-semantic declaration used for that request. Uploaded data or a new declaration advances the dataset
version and marks older revisions stale. The effective-input hash is immutable audit/replay evidence; it is not a
conversation-global freshness flag, because two valid workbooks may select different publisher datasets.

The orchestrator sees a compact, engine-owned catalog and proposes one action: `create` for a distinct result,
`modify` for a refinement of an existing result, or `inspect` to reopen it. It cannot assign IDs, claim another
conversation's analysis, or overwrite a revision. A `modify` may carry the stored name less some of its words: a
follow-up drops the filter words its question no longer asks about (`orchestrator._without_dropped_filters`), and the
engine renames the analysis in place, keeping its id, links and revision history (`conversations._renamed_analysis`). The browser link includes both analysis id and revision, so a
historical turn always restores the workbook that produced that answer. Shared input and private-reference tabs
remain visible while only the derived stack is switched.

Defaults are 1,000 active durable conversations and 1,000 spreadsheet-session bindings per user, 256 MiB of
serialized source and workbook state per user, 1 MiB per browser snapshot, 32 MiB per analysis revision, at most 50
analyses and 100 revisions per analysis, and deletion after 90 days of inactivity. Expired records stop consuming
conversation and spreadsheet-session slots immediately; the daily cleanup removes their stored data. Configuration
is bounded in `engine.config`; the per-analysis and spreadsheet-session limits are constants in
`engine.conversations` and `engine.sheet_sessions`. Creation, revision reservation, and source replacement use
PostgreSQL advisory transaction locks. Failed revisions are retained as zero-payload tombstones so a revision
number is never reused; an abandoned pending revision becomes a tombstone after one hour. Reopening, querying, or
saving refreshes the conversation expiry.

`python -m engine.retention_cleanup` is the single scheduled cleanup owner. It deletes expired conversation
metadata and the corresponding schemas in bounded batches, then removes expired RTDB traces when RTDB is enabled.
Terraform runs it daily with the non-superuser serving role; the engine container entrypoint exempts only this
maintenance command from loading model artifacts.

## Services

| Package | Role | Must not own |
|---|---|---|
| `engine/` | Authenticated reasoning, planning, grounding, execution | Presentation-only chat policy |
| `web/` | Workbook state, uploads, references, trace rendering; the rail component and importer the Sheets add-on loads | SQL semantics |
| `sheets-addon/` | Read-only Sheets sidebar and its server: cells, sign-in, server-to-server Prereasoner calls | Step presentation, import rules, reasoning, or sheet mutation |
| `mcp_server/` | Typed adapter over the engine HTTP API | A second planner or result synthesis |
| `orchestrator/` | Optional conversation and tool invocation | Arithmetic or factual answers without engine evidence |
| `db/` | Reproducible schema and knowledge synchronization | Request routing |
| `training/` | Offline datasets, fitting, and calibration | A competing serving implementation |
| `spider/` | Serving-faithful evaluation and research artifacts | Production selection shortcuts |

The engine container includes the complete runtime. The orchestrator is opt-in in Docker Compose and Terraform.
The engine runs without Gemini: with `EXTERNAL_LLM_ENABLED` off it makes no external model call, and
selection has no fallback. The orchestrator container copies only the auth, trace, request
validation, currency-intent, configuration, and adapter modules it imports; it does not contain planner weights or
planner implementation modules.

## Concurrency And Failure Behavior

- Model instances are loaded once per service process and shared by the serving stack. Questions run in parallel
  on them: what belongs to one request (its conversation schema, connections, question text, typing evidence and
  trace stream) is request-local (`engine/request_state.py`, `engine/trace.py:request_stream`), and the shared
  tokenizer and encode cache take short locks.
- Cloud Run sends an engine instance at most two requests at once and runs two to four instances
  (`infra/main.tf`). When none can take a request it answers 429 "no available instance", and the chat's
  engine client asks again with backoff for about a minute (`mcp_server/engine_client.py:_send`).
- Requests on one conversation are serialized (a local lock stripe and a conversation advisory lock), because a
  request rewrites that conversation's bridge tables.
- Database operations use bounded inputs and explicit transaction ownership.
- Cross-instance conversation quotas use a per-user PostgreSQL advisory transaction lock.
- Cross-instance source replacement and analysis revision assignment use conversation-scoped advisory locks.
- SQL is read-only, identifiers are quoted, and conversation schemas are ownership checked.
- Reference writes are atomic and failures are visible to callers.
- Optional trace or presentation failures do not fabricate a successful answer.
- A failed Gemini call yields nothing for its fallback step; selection never serves an unchecked query in its place.
- External lookups use bounded retry behavior; live integration tests can therefore be slower than hermetic tests.

## Artifacts And Configuration

`engine/config.py` is the owner for runtime environment variables. `.env.example` documents deployable defaults.
Runtime model artifacts live under `engine/data/` and are validated by a manifest; they are not source files and are
not duplicated under versioned names. The bundle holds the encoder (`qwen_lora/`, `encoder.pt`,
`encoder_meta.pt`), its calibrated heads (`anchor_assignment.npz`, `primitives.npz`), and the Schema.org
property head (`schema_property_head.pt`). It contains no SQL model and no fitted selector. Gemini is an
external service configured in `engine/config.py`, not a bundle artifact. Training output becomes
serving input only through the documented promotion process in [TRAINING.md](TRAINING.md).

The default [weight repository](https://huggingface.co/prereasoner/prereasoner-weights) is public. The
source manifest pins an immutable revision and every runtime-file hash, so a fresh clone can provision and
verify the compatible bundle without credentials. Reproducing the full hosted application still requires a
locally built knowledge database; hermetic planner, routing, reference, MCP, and browser tests require neither.

## Change Rules

The repository has one owner per decision:

- routing: `engine.routing.route()`;
- relationship discovery: `engine.relations.discover_fks()`;
- own-data SQL representation: the typed AST;
- own-data query selection (search, pool execution, grounding, and the labelled Gemini fallback):
  `engine.tables.TableQuery.select_query`;
- the one external model client (Gemini on Vertex AI): `engine.llm`;
- dual SQL/Python plan, source emission, and parity: `engine.deterministic`;
- bounded compound-question proposal validation and typed-plan fusion: `engine.decomposition`;
- private-reference behavior: `engine.master`;
- runtime configuration: `engine.config`;
- request validation and canonical table names: `engine.request_validation`;
- response and trace lineage: `engine.provenance`;
- conversation storage and expiry: `engine.conversations` plus `engine.retention_cleanup`;
- measured Spider claims: `spider/results/RESULTS.md`.

When replacing behavior, migrate every caller and remove the displaced path. Do not add another planner, router,
evaluation contract, or model bundle. See [../CONTRIBUTING.md](../CONTRIBUTING.md) and [../CLAUDE.md](../CLAUDE.md)
for the required validation and change discipline.
