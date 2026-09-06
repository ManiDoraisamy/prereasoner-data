# Marketing Website Review

Status: read-only review of the separate Prereasoner marketing website. This document records
findings; it does not authorize changes to the website repository or a production deployment.
Claims were compared on 2026-09-06 against the live `prereasoner.com` pages, their local source in
the separately owned FormFacade checkout, and the implementation and release contracts in this
repository. Recheck the live pages at release time; this file is evidence, not website source.

The approved, plain-language replacement copy is in
[PREREASONER_MARKETING_COPY.md](PREREASONER_MARKETING_COPY.md). It is kept here, alongside the
implementation evidence, because the website source has a separate repository owner.

## Repository Boundary

The engine, application, ETL, infrastructure, and technical documentation live in this repository.
The marketing pages live in the separate FormFacade repository under
`functions/content/{interpretable,excel-copilot,sheet-copilot,rag,opensource-ai}`.

Treat that repository as read-only unless its owner explicitly requests edits in the current task.
Do not replace product positioning, pricing, or editions there as part of an engine/documentation
review. A website review should produce findings in this repository first.

No website deployment is implied by a change here. Marketing and application deployments are
separate operations.

## Intended Product Structure

The website history contains seven deliberate Prereasoner revisions from 2026-08-30 through
2026-09-01. The current structure is intentional:

- Prereasoner;
- Excel Copilot;
- Sheets Copilot;
- Structured RAG; and
- **Community Edition**.

Community Edition must not be removed or silently redefined. Its intended launch offer is the
Apache-2.0 engine, public runtime weights, CPU operation, and a reproducible shared reference-data
layer. The task is to finish and evidence that offer, not delete it because some launch artifacts
are not public yet.

## Claims Already Supported

| Website claim | Repository evidence |
|---|---|
| Apache-2.0 engine source | `LICENSE` and the public source tree |
| Qwen2.5-0.5B core used without autoregressive SQL generation | `docs/MODEL_CARD.md`; SQL is assembled through the typed AST planner |
| CPU-capable model size | the 0.5B base and runtime configuration support CPU inference, subject to measured latency |
| Inspectable query and source-row path | planner, execution trace, and workbook implementation |
| Deterministic source synchronization | source-specific ETL, releases, checksums, and replay metadata under `db/sync/` |
| Public reference domains | documented Wikidata, IANA, CLDR, GeoNames, ECB, CDC, NLM, and other source pipelines |

Determinism means fixed inputs, configuration, database snapshot, and model artifacts produce the
same ranked plan and result. It does not mean every plan is correct.

## Launch Gaps In The Current Marketing Copy

These are findings for the website owner. They are not changes to make from this repository.

### Community Edition Artifacts

The source repository and the configured
[runtime-weight repository](https://huggingface.co/prereasoner/prereasoner-weights) are now public.
The live Community Edition page links both repositories and no longer contains the former "coming at
launch" placeholders. This item is resolved on the website as reviewed on 2026-09-06.

The weight release includes license metadata and is pinned by immutable revision and file hashes. Its
model card honestly records the historical unified-router checkpoint's remaining corpus, split, seed,
and held-out provenance gaps; the website must not turn artifact reproducibility into a claim that the
historical training run is independently reproducible.

### Knowledgebase Distribution

The website says the global knowledgebase is included. This repository does not distribute a seeded
production database: the Community deployer builds its minimal Wikidata, IANA, and ECB serving data
from publisher artifacts through the deterministic ETL. The website should say this directly so
"included" is not read as a bundled database snapshot. Source licenses, release identities, and
freshness metadata remain part of that build contract.

### Deployment Promise

The repository now provides a guided **Deploy to Google Cloud** button and one canonical deployer at
`deploy/gcp/deploy.sh`. It creates isolated state, builds and tests the public artifact, applies the
cost-reduced infrastructure profile, initializes the minimal world database, and removes the
temporary bootstrap identity. The website can link the exact snippet in `deploy/gcp/button.html`.
The live Community Edition page did not contain that button or a Google Cloud Shell link when checked
on 2026-09-06.

Do not describe this as anonymous, free, or a complete hosted browser application. Google still
requires authentication, a billing-enabled project, IAM authorization, and one cost confirmation.
The deployer creates the protected engine API; Firebase Auth and the browser client remain a
separately documented operator deployment.

### Structured RAG Tenancy

The website advertises a company tenant namespace shared across users. The current reference-table
implementation is user-scoped (`m_<md5(subject)>`) and request-locally materialized. Do not present
organization-wide sharing as live until organization identity, membership, authorization, isolation,
and held-out multi-user tests exist.

### Retrieval Wording

Final result rows are selected by SQL joins and filters, but learned model and embedding signals can
participate in typing, ranking, and entity resolution. "Exact relational result selection" is
supportable. Absolute "no embeddings" or "no LLM in the retrieval path" wording is too broad for
the current implementation.

### Sheets Behavior

The current product imports a user-selected Google Sheet into the Prereasoner workbook. It does not
install as a Workspace add-on, modify the original Sheet, write derivations back to that source, or
automatically refresh when the source changes. Marketing should distinguish the imported workbook
views from the original Google Sheet.

### Pricing And Entitlements

The website publishes monthly prices, question quotas, connected-sheet limits, team workspaces, SSO,
DPA, and support/compliance features. The engine currently enforces operational short-window rate
limits, not those plan entitlements. Publish these only when billing, entitlement enforcement, and
the corresponding commercial offer are live.

### Privacy And Compliance

The shared footer displays a Formesign SOC 2 badge on Prereasoner pages even though the badge does
not establish Prereasoner scope. The linked Community Edition privacy page is currently a generic
Google Forms add-on policy. It says the product receives form responses and uploaded files and names
Stripe, OpenAI, and Sarvam AI, but it does not document Prereasoner's Firebase identity, PostgreSQL
conversations, 90-day inactivity retention, delete-all behavior, minimized logs, or the
operator-controlled Anthropic path. Its stated effective date is 2019. Replace it with
product-specific durable policy text aligned with `PRIVACY.md`; do not add repeated consent dialogs.

## Launch Evidence Checklist

Before the website owner marks Community Edition and the hosted plans generally available:

1. Link the now-public GitHub and Hugging Face repositories and verify the weight manifest from a clean clone.
2. Demonstrate the documented CPU path with latency and memory measurements.
3. Rebuild or download the advertised knowledgebase from a clean environment and record licenses,
   source releases, row counts, and freshness.
4. Add the reviewed Cloud Shell button, retain the billing/authentication wording, and decide whether
   the separately deployed Firebase browser client is part of the advertised Community offer.
5. Limit Structured RAG copy to user-scoped references until organization tenancy is implemented.
6. Verify each advertised quota, billing flow, workspace feature, SSO/DPA offer, and compliance claim.
7. Publish Prereasoner-specific privacy and terms pages and remove unrelated product badges.
8. Test desktop/mobile navigation, overflow, images, CTA destinations, pricing links, privacy links,
   and the complete signup-to-answer flow in production.

The website should retain its Community Edition strategy throughout this work. Unsupported details
should be labelled as launch targets or completed before launch, not solved by deleting the edition.
