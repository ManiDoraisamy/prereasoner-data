# Documentation Map

This page is the entry point for developers. It separates the code that runs today from optional
deployment features, external artifacts, and planned work. A proposal in a roadmap is not a runtime
contract, and a synchronized source table is not automatically available to the planner.

## Status Vocabulary

| Label | Meaning |
|---|---|
| **Current** | Implemented in this repository and exercised by the named tests |
| **Opt-in** | Implemented, but disabled until configuration, grants, or external services enable it |
| **External** | Required for the complete application but intentionally absent from a public source checkout |
| **Planned** | Design or research work that must not be described as shipped behavior |

Documents that mix current and future work must label each item. When two documents disagree,
prefer the implementation owner and its tests, then correct the documentation.

## Fifteen-Minute Path

1. Follow [GETTING_STARTED.md](GETTING_STARTED.md) through the public-checkout test path.
2. Read [ARCHITECTURE.md](ARCHITECTURE.md) for the request flow and ownership boundaries.
3. Read [PROMPT_TO_SQL.md](PROMPT_TO_SQL.md) for one worked question-to-AST example.
4. Use the owner table in `GETTING_STARTED.md` to find the module and focused test for a change.
5. Run the CI-equivalent command in [TESTING.md](TESTING.md) before adding model or database prerequisites.

Runtime weights are publicly downloadable and hash-verified, but the seeded knowledge database is not
distributed as a snapshot. Reproducing the complete hosted application therefore also requires the
documented source ETL and local database build. That boundary is detailed in
[OPEN_SOURCE_RELEASE.md](OPEN_SOURCE_RELEASE.md).

## Mental Model

PreReasoner has five cooperating layers:

```text
web or MCP request
        |
        v
HTTP/auth + conversation/private-reference selection       engine/server.py, engine/master.py
        |
        v
route ownership + typed candidate search                   engine/knowledge.py, engine/routing.py,
                                                            engine/sql_*.py
        |
        v
guarded SQL execution over request and approved facts      conversation schema + bounded shared tables
        |
        v
rows, SQL, evidence, provenance, and optional trace events

offline: source ETL -> versioned releases -> explicit activation
offline: corpus -> training/calibration -> one promoted artifact bundle
```

Schema.org supplies named semantic coordinates. Wikidata and publisher datasets supply observations
and versioned facts. The model recognizes semantic shapes; mutable answer facts remain in source-owned
database releases.

## What Is Learned And What Is Deterministic

| Boundary | Mechanism | Authority |
|---|---|---|
| Column, intent, profile, and similarity signals | Frozen Qwen encoder plus calibrated heads | Learned evidence; may score or type candidates |
| SQL representation and expansion | Typed AST and schema graph | Deterministic |
| Candidate ordering | Named structural and encoder-derived features with stable tie-breaking | Deterministic for fixed artifacts and inputs |
| Joins, calculations, validation, and execution | Typed rules, calculation specifications, SQL guard, PostgreSQL | Deterministic |
| Entity fallback | Exact lookup, then embedding similarity, followed by grounding checks | Learned retrieval signal plus deterministic gates |
| Conversational presentation | Optional external orchestrator | Opt-in; cannot author SQL facts or numeric answers |

Determinism removes decoder sampling variance. It does not remove ambiguous questions, missing
candidate shapes, ranking mistakes, source gaps, or entity-resolution errors.

## Runtime Status

| Capability | Status | Notes |
|---|---|---|
| Typed own-data SQL planning and calculation verification | **Current** | The main planner path |
| Conversation-scoped uploads and user-scoped private references | **Current** | Organization-wide tenancy is not implemented |
| Wikidata-backed entity grounding | **Current** | Uses legacy `public` and `knowledgebase` storage names pending migration |
| ECB dated currency conversion | **Current** | Uses a release-labelled derived daily projection |
| Schema.org class interpretation | **Current, evidence-only** | Cannot choose a route or release an answer |
| IANA country enrichment | **Opt-in** | Requires code approval, database grants, and `ENRICHMENT_ACTIVE_DATASETS` |
| Other synchronized publisher datasets | **Current storage, not serving** | Physical releases exist; planner activation remains gated |
| Firebase trace streaming | **Opt-in** | Responses still work when RTDB is disabled |
| Anthropic conversational orchestration | **Opt-in** | Requires the deployment switch and operator configuration |
| Runtime weights | **External** | Public, manifest-pinned Hugging Face bundle; fetched on demand |
| Seeded knowledge database | **External** | Not included as a snapshot; built through source ETL |
| `wikidata` schema migration, ontology-clean router retrain, organization tenancy, and generic temporal enrichment | **Planned** | Do not describe as shipped |

## Canonical Documents

| Question | Canonical document |
|---|---|
| How do I install, test, and make a first request? | [GETTING_STARTED.md](GETTING_STARTED.md) |
| What runs for a request, and which module owns each decision? | [ARCHITECTURE.md](ARCHITECTURE.md) |
| How does typed SQL search work? | [SQL_AST.md](SQL_AST.md) |
| How are arithmetic semantics represented and verified? | [CALCULATIONS.md](CALCULATIONS.md) |
| Which source schemas and releases exist? | [SOURCE_DATA.md](SOURCE_DATA.md) |
| How is PostgreSQL bootstrapped and seeded? | [../db/README.md](../db/README.md) |
| How are models trained, calibrated, and promoted? | [TRAINING.md](TRAINING.md) |
| What are the model and corpus limitations? | [MODEL_CARD.md](MODEL_CARD.md), [DATA_CARD.md](DATA_CARD.md) |
| What metadata is published with the weight bundle? | [HUGGING_FACE_MODEL_CARD.md](HUGGING_FACE_MODEL_CARD.md) |
| Which tests prove which boundary? | [TESTING.md](TESTING.md) |
| How do MCP and the optional chat service fit in? | [MCP.md](MCP.md) |
| How is Google Cloud deployment performed? | [../infra/README.md](../infra/README.md) |
| How does the Community Edition deploy button work? | [../deploy/gcp/README.md](../deploy/gcp/README.md) |
| What can an open-source release claim today? | [OPEN_SOURCE_RELEASE.md](OPEN_SOURCE_RELEASE.md) |
| Which marketing claims still need launch evidence? | [MARKETING_WEBSITE_REVIEW.md](MARKETING_WEBSITE_REVIEW.md) |
| What enrichment work is implemented or still planned? | [KNOWLEDGE_ENRICHMENT_ROADMAP.md](KNOWLEDGE_ENRICHMENT_ROADMAP.md) |
| Where are accepted Spider measurements? | [../spider/results/RESULTS.md](../spider/results/RESULTS.md) |

Files under `docs/notes/` are maintainer history and investigation records. They can explain why a
decision was made, but the canonical documents above define the current contract.

## Terms That Must Stay Distinct

- **Schema.org ontology:** the versioned vocabulary of classes, properties, inheritance, domains,
  and ranges.
- **PostgreSQL schema:** a storage and ownership namespace such as `iana`, `chat`, or `c_<id>`.
- **Source release:** one immutable, validated publisher snapshot selected by release ID.
- **Logical dataset:** a registry policy that can expose bounded source rows to a request.
- **Domain profile or role:** request-local semantic evidence such as healthcare intake or food order;
  it is not a storage owner.
- **Materialization:** copying only eligible, bounded source rows into the request's planner table set.
- **Activation:** agreement among code approval, deployment allowlist, database grants, source policy,
  and request evidence. Having rows in PostgreSQL is not activation.

## Change Discipline

Each decision has one owner. Extend that owner, migrate every caller, and remove displaced behavior
instead of adding a parallel router, planner, verifier, corpus, or model bundle. Follow
[../CONTRIBUTING.md](../CONTRIBUTING.md) and the test matrix in [TESTING.md](TESTING.md).
