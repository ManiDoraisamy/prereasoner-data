# PreReasoner — Research positioning

> **Why the approach is novel, and what we can and cannot claim.** For *how the system works*,
> see [ARCHITECTURE.md](ARCHITECTURE.md); for *how to reproduce the model*, see
> [../training/README.md](../training/README.md). This doc is the argument and the evidence —
> written so a reviewer can find every claim's ground and every caveat.

## Contents

1. [The core claim](#1-the-core-claim-name-the-dimensions-then-query-them) ·
2. [Two kinds of interpretability](#2-two-kinds-of-interpretability) ·
3. [Why this is not RAG](#3-why-this-is-not-rag-its-closer-to-the-inverse) ·
4. [Why this is not agentic text→SQL](#4-why-this-is-not-agentic-textsql-and-not-a-post-hoc-probe) ·
5. [Named dimensions as foreign keys](#5-named-dimensions-as-foreign-keys) ·
6. [The taxonomy re-anchor: from non-discriminative to AUC 1.0](#6-the-taxonomy-re-anchor-from-non-discriminative-to-auc-10) ·
7. [Scope and generalization risks](#7-scope-and-generalization-risks-where-to-red-team) ·
8. [Reproducing the model](#8-reproducing-the-model) ·
9. [Summary for reviewers](#9-summary-for-reviewers)

---

## 1. The core claim: name the dimensions, then query them

Reserve hidden dimensions of a model and **anchor** each one to an interpretable target (an
entity type, a datatype, a query intent) via **mean-squared error on raw activations** — no
sigmoid, no BCE. The result is a representation that is **learned yet directly readable**: you
can inspect what the model believes each input is, and then *query it exactly* rather than
approximately. Interpretability is a property *of the representation*, imposed at training time —
not a post-hoc probe bolted on after the fact. And because the readout *is* the representation,
the reasoning is **observable as it happens**: the system streams each step of the trace — the
resolved entity QIDs, the SQL, every intermediate view as it is built — to the client in real
time (ARCHITECTURE.md §9), so the faithfulness is something a reviewer *watches the model do*,
step by step, not only inspects in an artifact afterward.

The research program ran this mechanism across several settings before this system (color
channels in a small language model, parsed code); those earlier proofs-of-concept are outside
this repository. The released incarnation is the tabular one: an uploaded spreadsheet + a
natural-language question become **interpretable SQL** over the user's own data joined to an
implicit Wikidata "world" knowledge DB. The model is a small encoder with named, interpretable
dimensions; the named dims *are the product* — they drive the typing, the operator choice, and
the entity resolution that assemble the query. Concretely, "total amount in France" over
`customers + orders` becomes
`… JOIN knowledgebase."city" ON "city".qid = bridge.world_key WHERE "city".country = 'Q142'`
and computes **270** — no autoregressive generation anywhere in the loop.

**Scope, stated honestly.** This is valuable and shippable for the **declarative** slice
(CSV/Sheets Q&A → SQL): declarative targets work with small models because the parse tree is
close to the execution tree. It is **not** a general interpretable replacement for LLMs — it has
no machinery for genuinely ambiguous/underspecified intent→plan mapping, which is exactly where
generation earns its keep. The scope is deliberate, not a gap.

---

## 2. Two kinds of interpretability

The system factorizes table understanding into two components with **deliberately different
epistemic status**. Conflating them overstates the learned content of the joins — this
distinction is the crux.

| | **A. Learned-but-legible readout** | **B. Specified, auditable structure** |
|---|---|---|
| Answers | *What is this cell/column?* | *How do they relate (the joins)?* |
| Source | **Learned** (anchored named dims) | **Deterministic algorithm** (not learned) |
| Code | `engine/data/alloc.json`, the relational readout (`engine/dimension.py`, `engine/router.py`) | `engine/fk_edges.py`, `engine/relations.py` |
| Analogy | learned representations you can read off | a rule engine you can audit |
| Moat | the novel research contribution | solid engineering, but not the novelty |

**The honest framing:** claim the *readout* (A) as learned-yet-legible; claim the *structure*
(B) as specified-and-auditable. The model does **not discover** the joins — the "intelligence of
what joins what" lives in `engine/relations.py`'s inclusion-dependency algorithm and
`engine/fk_edges.py`'s index comparisons, not in the neural weights. The network's only
relational parameter is a per-edge-type additive attention bias: it learns *how strongly* to
attend along a *given* edge (`same_col` / `same_row` / `same_cell` / `fk`), never *which* units
relate. A reviewer who hears "interpretable learned joins" and then reads the code will find a
hand-built graph prior — so we don't claim that.

> Terminology bridge: "metadata attention" = the `same_col` edge (a value ↔ its column name);
> "relational attention" = the `fk` edge (order → customer; a city value → its row in
> `knowledgebase."city"`). Both are *given*, then weighted.

---

## 3. Why this is not RAG (it's closer to the inverse)

The only trait shared with RAG is "fetch world facts from an external store instead of
memorizing them in weights" — but that is just *using a database*, which predates RAG by decades.
On every operative axis the two are opposites:

| | **RAG** | **PreReasoner** |
|---|---|---|
| Dimensions | **unnamed** embedding dims | **named** dims (`city`, `hospital`, `currency`, `intent_agg_sum`, …) |
| Retrieval | **approximate** cosine / ANN top-k | **exact** SQL equality join on `qid` |
| Answer | dump text chunks into an LLM that **generates** (can hallucinate) | **deterministic compute** (SUM/JOIN/WHERE), no LLM in the loop |
| Failure mode | hallucination | a traceable wrong query |

**The framing that captures the contribution:** *naming the dimensions is what makes precise
querying possible — RAG can only approximate because its dimensions are unnamed (you cannot
write `WHERE dim_247 = …`). Anchor the dimension and the approximate-retrieve-then-generate
stack collapses into one exact query. RAG searches; this queries.*

**One honest caveat.** There is a single learned, *soft* step: **type classification + entity
resolution** — which world table a column routes to (via its anchored taxonomy dims, gated by
per-leaf calibrated thresholds, `engine/data/route_thresholds.json` /
`dim_thresholds.json`) and which world entity a cell resolves to (`knowledgebase."words"` exact-norm
match first, then a bge-small cosine nearest-neighbour above threshold). Both steps are
interpretable, and they are *classification / resolution*, not answer-generation; once the cell
is resolved to a **QID**, the row match and the computation that follow are exact equality joins.

---

## 4. Why this is not agentic text→SQL (and not a post-hoc probe)

Two adjacent approaches are worth contrasting explicitly, because PreReasoner's contribution is
precisely what it declines to do.

**Versus instruct-LLM / agentic text→SQL.** The mainstream recipe prompts a large instruct model
to *emit a SQL string* autoregressively (optionally in an agent loop with retries and
self-critique). That string is a black-box generation: it can hallucinate columns, silently drop
a clause, or invent a join, and the only "explanation" available is a second after-the-fact
rationalization from the same model. PreReasoner inverts this. There is **no model that writes
SQL**. The query is assembled by a deterministic template from (a) the learned-yet-legible
readout — *what each column/cell is* and *what the question asks for* — and (b) the specified FK
+ world structure. The operator (`SUM`/`COUNT`/`AVG`) is read off the anchored `intent_agg_*`
dims, not decoded as tokens; the world filter is a QID equality, not a generated literal. Because
every piece is read from a named dimension or computed by an audited rule, a wrong answer is a
**traceable wrong query**, not an unexplained hallucination — and the **clarify gate** refuses
rather than bluffs: if a content word resolved but never reached the SQL (an entity that resolved
but isn't filtered, or a measure word with no aggregate), the system returns a "did you mean?"
rephrasing instead of a confidently wrong number.

**Versus post-hoc probing / SAEs.** The standard interpretability move trains a *separate*
linear probe (or a sparse autoencoder) on a frozen model's activations *after* training, then
claims the probe's accuracy reveals what the model "knows." That is a read *of* the
representation, not a property *of* it: the probe can be a wide linear classifier fitting
structure the model never used, and nothing constrains the model to keep the concept legible.
PreReasoner moves the interpretability *into the training objective*: the named dims are anchored
by MSE on raw activations *while the model trains*, so legibility is enforced, not discovered.
The readout is the model's own output dimensions — there is no auxiliary probe to disagree with
the deployed path (`training/calibrate/validate_route.py` tests the *served* model's grounded
routing, not a side-probe).

---

## 5. Named dimensions as foreign keys

A named dimension (e.g. the taxonomy leaf `city` or `hospital`) is the **type tag** that routes a
column to a world table; the join itself is then a normal SQL foreign-key join. This is literal,
not analogy: the `wikipedia` schema is **QID-keyed end to end** — every table
(`knowledgebase."city"`, `knowledgebase."country"`, `knowledgebase."hospital"`, …) has **`qid` as PRIMARY
KEY**, and item-valued property columns hold the related entity's **QID as a true FOREIGN KEY**
(`city.country` = the country's QID; `country.continent` = the continent's QID). So "named
dimensions act as foreign keys into the world model" is accurate twice over: the anchored leaf
dim picks the *table*, and the resolved cell QID is the *key* that joins to it. A 2-hop world
fact ("orders in Europe") is just following two QID FKs — `city.country` then
`country.continent = 'Q46'`. The `knowledgebase."<type>"` naming (the exact Wikidata label) also
keeps these world tables from clashing with the user's own table names.

---

## 6. The taxonomy re-anchor: from non-discriminative to AUC 1.0

The clearest evidence that anchoring is doing real work — and the clearest cautionary tale about
*what you anchor on* — is the taxonomy re-anchor.

The taxonomy family replaced earlier hand-named type dims with **real Wikidata P279 taxonomy
nodes**, one 0/1 dim per node, co-firing down a token's root→leaf path (a city fires
`geolocatable_entity` … `populated_place` … `city`; the shipped allocation has 74 such dims with
42 live entity leaves). The first training pass anchored these on noisy data: the firing values
came from embedding-mapped CSV cells, where a `referral` column had mapped to `hospital` and
proper-noun columns had taught `street` to fire on *any* proper noun. The result was
non-discriminative — for "Mayo Clinic" / "Cleveland Clinic" the `street` dim fired **0.11–0.16**,
*out-firing* `hospital`. The named dim existed but did not separate the types it named;
**taxonomy AUC was 0.886**.

The fix isolates exactly the variable that mattered: the *anchoring targets*, not the
architecture. `training/corpus/fetch_type_instances.py` pulled **6,665 clean Wikidata P31
instances across 46 non-geo leaves**; the corpus builder **replaced** the noisy pool
(deliberately *skipping the geo spine* so city/country/state stayed intact); and
`training/anchor/reanchor.py` re-trained **only the relational readout** — frozen encoder,
disk-cached embeddings, taxonomy-oversampled MSE. After: **taxonomy AUC 0.886 → 1.0**, intent
dims unchanged at 1.0, the encoder byte-for-byte identical. `hospital` now fires 0.85,
`software → software`, `bank → bank`, geo preserved.

Two claims fall out of this, and both are load-bearing:

- **Anchoring genuinely shapes the representation.** Holding the encoder fixed and re-training
  only the readout on clean targets moved AUC from "barely better than chance ordering" to
  perfect separation. If the dims were cosmetic, clean data could not have rescued them.
- **The named dim is only as honest as its anchor.** A dim labelled `hospital` that fires on
  streets is *not* interpretable, whatever its name says. The discipline the method demands is
  anchoring on instances that actually instantiate the concept — and calibrating the readout's
  firing thresholds on the *served* model (`training/calibrate/calibrate_dims.py`,
  `calibrate_route.py`) rather than trusting stale ones.

**The unified encoder.** The encoder is a **frozen-base Qwen2.5-0.5B + a LoRA adapter** feeding a
10-layer bidirectional relational transformer, trained jointly with **contrastive InfoNCE** on
Wikidata altLabel pairs (the metric geometry for entity resolution) and **MSE anchoring** of the
named dims on the column-graph corpus (the typing) plus reused SQL/join graphs (the
operator/intent dims). The allocation is **93 content dims**: 9 struct + 74 taxonomy + 10 intent.
The decoupling discipline matters: the dense contrastive loss overwhelms the sparse anchoring
gradient if mixed naively, so the taxonomy readout is re-anchored with the encoder **frozen**
(above). The claim is therefore: the encoder is learned-by-contrastive-objective AND
legible-by-MSE — the two objectives are compatible if and only if they are decoupled at training
time.

---

## 7. Scope and generalization risks (where to red-team)

The tabular system's load-bearing fragilities, stated plainly:

1. **FK heuristics on messy real-world schemas** (`engine/relations.py`) — the join graph is
   only as good as the inclusion-dependency + name/type heuristics.
2. **Intent dims vs. real phrasings** — the 10 `intent_*` dims (`intent_agg_sum/count/avg`,
   `filter_eq/gt/lt`, `group`, `sort_desc/asc`, `limit`) drive the operator; out-of-distribution
   phrasing behaviour (sell/earn/spend, how much/many, mean/typical/per) should be pre-registered
   before wiring deeper into a production product.
3. **Taxonomy routing on the long tail.** The re-anchor fixed the 46 non-geo leaves it covered
   (§6), but the `wikipedia` schema lazy-syncs more types than that and the taxonomy is broader
   still. Column *names* fire inconsistently, which is why routing reads cell **values**, not
   headers — watch the tail as long-tail types are added to the lazy-sync.
4. **Resolution threshold + lazy-sync freshness.** A cell resolves to a QID by exact-norm match
   then embedding NN ≥ threshold, then triggers a lazy Wikidata fill; the near-miss band
   (sub-threshold entities the clarify gate should catch) and the cold-fill latency deserve
   measurement before relying on them under load.

> Note: the operator/plan is read from the anchored `intent_*` dims; there is no separate
> auxiliary "small model on a handful of golden pairs." The generalization risk is real but lives
> in (1)–(4), not in a hidden side model.

---

## 8. Reproducing the model

The full pipeline that produced the shipped artifacts (`engine/data/qwen_lora`, `encoder.pt`,
`encoder_meta.pt`, `alloc.json`) is released in [`training/`](../training/README.md):

1. **World DB** (`training/world/`, `db/sync/`) — the Wikidata import, taxonomy sync, and
   resolution index.
2. **Corpus discovery** (`training/corpus/`) — CSV type discovery, column clustering, and the
   clean-instance corpus.
3. **Taxonomy** (`training/taxonomy/`) — reconcile → rollup → the dim allocation.
4. **Encoder training** (`training/train/`, GPU) — the multitask → unified → taxonomy chain that
   produced the LoRA adapter.
5. **Readout anchoring** (`training/anchor/`) — produced the shipped relational readout.
6. **Calibration + release gates** (`training/calibrate/`) — routing/threshold calibration,
   artifact-consistency validation, and the served-model routing gate; orchestrated
   transactionally by `training/tools/pipeline.py` (snapshot → retrain → promote only if the
   gates pass, roll back otherwise).

`training/README.md` states plainly which stages re-run self-contained from this release and
which historical bootstrap artifacts ship as data rather than as regenerable code.

---

## 9. Summary for reviewers

> PreReasoner factorizes table understanding into two interpretable components with deliberately
> different epistemic status. First, a **learned-but-legible representation**: a single unified
> encoder — a frozen Qwen2.5-0.5B base with a LoRA adapter, trained jointly by contrastive
> InfoNCE on Wikidata altLabel pairs (a metric space for entity resolution) — feeds a small
> bidirectional relational transformer whose reserved output dimensions are anchored, via
> mean-squared error on raw activations, to named interpretable targets: nine datatype dims, 74
> Wikidata P279 taxonomy-path nodes (co-firing root→leaf, so a value reads as *populated_place →
> city*), and ten query-intent dims. Each clean unit (a column name, or a single cell value) is
> encoded and its subtokens mean-pooled into one vector, so names and numbers are never split at
> the anchoring level even though a pretrained subword tokenizer is used. The two training
> objectives are decoupled: the dense contrastive loss shapes the encoder, after which the
> encoder is frozen and only the relational transformer's readout is re-anchored on clean
> Wikidata P31 instances — a step that moved taxonomy separability from AUC 0.886 to 1.0 without
> touching the encoder. These dimensions are *learned* yet directly readable, so one can inspect
> what the system believes each cell and column to be, with no separate post-hoc probe. Second, a
> **specified, auditable relational structure**: the joins among cells, columns, and tables —
> including cross-table foreign keys and the links into an external Wikidata world store whose
> tables are qid-primary-keyed with qid foreign keys — are *not* learned but computed by a
> deterministic algorithm (inclusion-dependency tests with name/type heuristics) and supplied to
> the network as a fixed attention prior; the network learns only *how strongly* to weight each
> given relation, never *which* relations exist. The final query is assembled by a deterministic
> template from the learned readout (type, resolved qid, operator) and the specified structure,
> then executed against the database; there is no autoregressive generation at inference, and a
> coverage gate refuses with a clarifying rephrasing rather than emit a query that silently
> dropped part of the question. The system's interpretability is therefore of two kinds —
> learned-yet-legible for *what the data is*, and specified-and-auditable for *how it relates* —
> and conflating them overstates the learned content of the joins.
