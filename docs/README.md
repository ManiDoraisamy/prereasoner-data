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

Runtime weights are public and hash-verified. A seeded knowledge database is not distributed as a
snapshot, so reproducing the hosted application also requires the documented source ETL and a local
database build. That boundary is described in [OPEN_SOURCE_RELEASE.md](OPEN_SOURCE_RELEASE.md).

## Mental Model

The request path has five parts:

```text
browser or MCP request
        |
        v
HTTP, authentication, and private-reference selection
        |
        v
route ownership and typed SQL candidate search
        |
        v
guarded SQL execution over uploaded and approved reference rows
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
| Column, intent, profile, and similarity signals | Frozen Qwen encoder plus calibrated heads | Learned evidence; it scores or types candidates |
| SQL representation and expansion | Typed AST and schema graph | Deterministic |
| Candidate ordering | Named structural and encoder-derived features with stable tie-breaking | Deterministic for fixed artifacts and inputs |
| Joins, calculations, validation, and execution | Typed rules, calculation specifications, SQL guard, and PostgreSQL | Deterministic |
| Entity fallback | Exact lookup, then similarity, followed by grounding checks | Retrieval signal plus deterministic gates |
| Conversational presentation | Optional external orchestrator | Cannot author SQL facts or numeric answers |

Determinism removes decoder sampling variance. It does not remove ambiguous wording, missing
candidates, schema-linking mistakes, ranking mistakes, source gaps, or entity-resolution errors.

## Runtime Status

| Capability | Status | Notes |
|---|---|---|
| Typed own-data SQL planning and calculation verification | **Current** | Main planner path |
| Conversation-scoped uploads and user-scoped private references | **Current** | Organization-wide tenancy is not implemented |
| Wikidata-backed entity grounding | **Current** | Uses legacy `public` and `knowledgebase` storage names pending migration |
| ECB dated currency conversion | **Current** | Uses a release-labelled daily projection |
| Schema.org named-dimension interpretation | **Current** | See the model and data cards for current trained and servable coverage |
| IANA country enrichment | **Current in guided Community deploy** | Raw Terraform remains opt-in; the guided deployment applies grants and activates it |
| Other synchronized publisher datasets | **Current storage, not serving** | Planner activation remains gated |
| Firebase trace streaming | **Opt-in** | Completed HTTP responses still render when RTDB is disabled |
| Anthropic conversational orchestration | **Opt-in** | Requires the deployment switch and operator configuration |
| Runtime weights | **External** | Public, manifest-pinned Hugging Face bundle |
| Seeded knowledge database | **External** | Built through source ETL rather than distributed as a snapshot |
| `wikidata` migration, organization tenancy, and generic temporal enrichment | **Planned** | Do not describe these as shipped |

## Canonical Documents

| Question | Canonical document |
|---|---|
| How do I install, test, and make a first request? | [GETTING_STARTED.md](GETTING_STARTED.md) |
| What runs for a request, and which module owns each decision? | [ARCHITECTURE.md](ARCHITECTURE.md) |
| How does typed SQL search work? | [SQL_AST.md](SQL_AST.md) |
| How are arithmetic semantics represented and checked? | [CALCULATIONS.md](CALCULATIONS.md) |
| Which source schemas and releases exist? | [SOURCE_DATA.md](SOURCE_DATA.md) |
| How is PostgreSQL bootstrapped and seeded? | [../db/README.md](../db/README.md) |
| How are models trained, calibrated, and promoted? | [TRAINING.md](TRAINING.md) |
| What are the model and corpus limits? | [MODEL_CARD.md](MODEL_CARD.md), [DATA_CARD.md](DATA_CARD.md) |
| What metadata is published with the weight bundle? | [HUGGING_FACE_MODEL_CARD.md](HUGGING_FACE_MODEL_CARD.md) |
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
