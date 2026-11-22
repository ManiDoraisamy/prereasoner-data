# Documentation Map

Start here if you are new to the repository. Prereasoner is built around named dimensions: explicit
classes, properties, relationships, and calculations that connect language to data and source facts.
The current runtime uses typed SQL to compose and check those dimensions. This page tells you what
runs today, what needs an external service, and what is still a plan. A roadmap is not a runtime
contract. A table in PostgreSQL is not automatically visible to the planner.

## Status Vocabulary

| Label | Meaning |
|---|---|
| **Current** | Implemented in this repository and covered by the named tests |
| **Opt-in** | Implemented, but disabled until configuration, grants, or an external service is enabled |
| **External** | Needed for the complete application, but intentionally not included in this checkout |
| **Planned** | Design or research only; do not describe it as shipped behavior |

When two documents disagree, check the implementation owner and its tests first, then update the
docs. Do not preserve two competing descriptions of the same behavior.

## Fifteen-Minute Path

1. Follow [GETTING_STARTED.md](GETTING_STARTED.md) through the public-checkout test path.
2. Read [ARCHITECTURE.md](ARCHITECTURE.md) to see who owns each decision.
3. Read [PROMPT_TO_SQL.md](PROMPT_TO_SQL.md) for one question-to-query example.
4. Use the owner table in `GETTING_STARTED.md` to find the module and focused test for a change.
5. Run the CI-equivalent command in [TESTING.md](TESTING.md) before adding model or database prerequisites.

Runtime weights are public and hash-verified. The hosted production database is not distributed.
The Community deploy restores a minimal public, SHA-256-pinned seed (`community-seed-v4.dump`); a
fuller database needs the documented source ETL. That boundary is described in
[OPEN_SOURCE_RELEASE.md](OPEN_SOURCE_RELEASE.md).

## Mental Model

The request path has five parts:

```text
browser or MCP request
        |
        v
HTTP, authentication, and private-reference selection
        |
        v
canonical request validation and conversation ownership
        |
        v
route ownership; own data: closed choices over engine-built options, decoded into typed SQL programs
        |  an answer most likely wrong: retried by one guarded query Gemini writes
        |
        v
guarded execution over uploaded and approved reference rows
        |  supported shared-plan subset: bounded emitted Python/SQL, optional parity
        |  other shapes: SQL; explicit Python/verification rejects the fallback
        |
        v
rows, SQL, evidence, provenance, and optional trace events

offline: source ETL -> versioned releases -> explicit activation
offline: corpus -> training/calibration -> one promoted artifact bundle
```

Schema.org supplies shared names for classes and properties. Wikidata and publisher datasets supply
observations and versioned facts. The model recognizes useful shapes; mutable answer facts stay in source-owned
database releases.

## What Is Learned And What Is Deterministic

| Boundary | Mechanism | Authority |
|---|---|---|
| Column, intent, profile, and similarity signals | Frozen Qwen encoder plus calibrated heads | Learned evidence; it types columns and reads intent for composition and world questions |
| Own-data reading (the Decisions path) | The sheet compiler (`engine/sheet/`) compiles the workbook; stock bge-m3 and the engine's word reader shortlist what the question may mean; Gemini's Decisions API, an external closed-choice model (`engine/llm.py:decide`), answers closed questions whose options the engine built | External evidence: probabilities only, never text, SQL or a program |
| SQL representation and construction | Typed AST, schema graph, and the deterministic decoder that builds each program of the reading as a typed AST | Deterministic |
| Named-analysis SQL/Python emission | One immutable feed-forward plan and two source emitters | Deterministic for the supported subset |
| Own-data query choice | The verifier runs every program; one more closed question weighs those that returned rows, per answer: answered at 0.575 or more, warned from 0.42 (or when two readings are worded alike), most likely wrong below 0.42 or when "none of these" is likelier | Deterministic thresholds over the external model's probabilities; never asked back for the engine's own uncertainty |
| Retry of an answer most likely wrong | Gemini 4 Neon (`engine/retry.py`) writes one SQLite SELECT over the workbook (its compiled tables and the columns the request's links reach); also run when the person presses Retry on a marked answer | The one place a model writes SQL: guarded, run read-only, and shown with its SQL; the person is asked only when the retry says the sheet does not hold what is asked |
| Joins, calculations, validation, and execution | Typed rules, calculation specifications, guarded emitters, and PostgreSQL | Deterministic |
| Entity fallback | Exact lookup, then similarity, followed by grounding checks | Retrieval signal plus deterministic gates |
| Factual answer presentation | Shared deterministic renderer | Displays structured engine results; the optional chat's Gemini may make a follow-up's wording explicit using schema context |

Determinism removes sampling variance. It does not remove ambiguous wording, missing candidates,
schema-linking mistakes, selection mistakes, source gaps, or entity-resolution errors.

## Runtime Status

| Capability | Status | Notes |
|---|---|---|
| Typed own-data SQL planning and calculation verification | **Current** | Main planner path |
| Deterministic SQL and readable Python emission | **Current, bounded** | All supported direct and named plans use the selected policy; `auto` prefers Python through 10,000 estimated input rows and falls back safely; unsupported explicit Python/verification requests error |
| Conversation-scoped uploads and user-scoped private references | **Current** | Organization-wide tenancy is not implemented |
| Bounded conversation storage and inactivity expiry | **Current** | Daily cleanup; defaults and limits are in `ARCHITECTURE.md` |
| Multiple named analysis workbooks per conversation | **Current** | Immutable revisions; historical rail links retain shared input tables |
| Browser XLSX upload | **Current** | Version-pinned parser in a bounded Web Worker; release-journey tested |
| Wikidata-backed entity grounding | **Current** | Uses legacy `public` and `knowledgebase` storage names pending migration |
| ECB dated currency conversion | **Current** | Uses a release-labelled daily projection |
| Schema.org named-dimension interpretation | **Current** | See the model and data cards for current trained and servable coverage |
| IANA country enrichment | **Current in guided Community deploy** | Raw Terraform remains opt-in; the guided deployment applies grants and activates it |
| Other synchronized publisher datasets | **Current storage, not serving** | Planner activation remains gated |
| Firebase trace streaming | **Opt-in** | Completed HTTP responses still render when RTDB is disabled |
| Gemini: conversational orchestration, starter questions, and reference generation on Vertex AI; own-data closed choices on the Gemini Developer API's Decisions endpoint, and the own-data retry's query (Gemini 4 Neon) on the same API | **Current** | Always on, with no switch; needs a Vertex AI project and a Gemini Developer API key, which Terraform creates and keeps in Secret Manager |
| Runtime weights | **External** | Public, manifest-pinned Hugging Face bundle |
| Seeded knowledge database | **External** | Community restores a minimal pinned seed dump; the production database is built through source ETL and not distributed |
| `wikidata` migration, organization tenancy, and generic temporal enrichment | **Planned** | Do not describe these as shipped |

## Canonical Documents

| Question | Canonical document |
|---|---|
| How do I install, test, and make a first request? | [GETTING_STARTED.md](GETTING_STARTED.md) |
| What runs for a request, and which module owns each decision? | [ARCHITECTURE.md](ARCHITECTURE.md) |
| How are own-data questions read and built into typed SQL? | [PROMPT_TO_SQL.md](PROMPT_TO_SQL.md) and [ARCHITECTURE.md](ARCHITECTURE.md#own-data-sql-planner); [SQL_AST.md](SQL_AST.md) for the typed AST (its readers sections are history) |
| How do the matching SQL and Python emitters work? | [DETERMINISTIC_EMITTERS.md](DETERMINISTIC_EMITTERS.md) |
| How are arithmetic semantics represented and checked? | [CALCULATIONS.md](CALCULATIONS.md) |
| Which source schemas and releases exist? | [SOURCE_DATA.md](SOURCE_DATA.md) |
| How is PostgreSQL bootstrapped and seeded? | [../db/README.md](../db/README.md) |
| How are models trained, calibrated, and promoted? | [TRAINING.md](TRAINING.md) |
| What are the model and corpus limits? | [MODEL_CARD.md](MODEL_CARD.md), [DATA_CARD.md](DATA_CARD.md) |
| What metadata is published with the weight bundle? | [HUGGING_FACE_MODEL_CARD.md](HUGGING_FACE_MODEL_CARD.md), and [HUGGING_FACE_ADAPTER_CARD.md](HUGGING_FACE_ADAPTER_CARD.md) for `qwen_lora/` |
| Which tests prove which boundary? | [TESTING.md](TESTING.md) |
| How do MCP and the optional chat service fit in? | [MCP.md](MCP.md) |
| How is Google Cloud deployment performed? | [../infra/README.md](../infra/README.md) |
| How does the Community Edition deploy button work? | [../deploy/gcp/README.md](../deploy/gcp/README.md) |
| What can an open-source release claim today? | [OPEN_SOURCE_RELEASE.md](OPEN_SOURCE_RELEASE.md) |
| What copy is approved for the separate website? | [PREREASONER_MARKETING_COPY.md](PREREASONER_MARKETING_COPY.md) |
| Which website claims still need evidence? | [MARKETING_WEBSITE_REVIEW.md](MARKETING_WEBSITE_REVIEW.md) |
| What enrichment work is implemented or planned? | [KNOWLEDGE_ENRICHMENT_ROADMAP.md](KNOWLEDGE_ENRICHMENT_ROADMAP.md) |
| Where are accepted Spider measurements? | [../spider/results/RESULTS.md](../spider/results/RESULTS.md) |

Files under `docs/notes/` are maintainer history. They can explain why a decision was made, but the
canonical documents above define the current contract.

## Terms That Must Stay Distinct

- **Schema.org ontology:** the versioned vocabulary of classes, properties, inheritance, domains, and ranges.
- **PostgreSQL schema:** a storage and ownership namespace such as `iana`, `chat`, or `c_<id>`.
- **Source release:** one immutable, validated publisher snapshot selected by release ID.
- **Logical dataset:** a registry policy that can expose bounded source rows to a request.
- **Domain profile or role:** request-local evidence such as healthcare intake or food order; it is not a storage owner.
- **Materialization:** copying only eligible, bounded source rows into the request's planner table set.
- **Activation:** the agreement among code approval, deployment allowlist, database grants, source policy, and request evidence. Rows in PostgreSQL alone are not activation.

## Change Discipline

Each decision has one owner. Extend that owner, migrate every caller, and remove displaced behavior
instead of adding a parallel router, planner, verifier, corpus, or model bundle. Follow
[../CONTRIBUTING.md](../CONTRIBUTING.md) and the test matrix in [TESTING.md](TESTING.md).
