---
license: apache-2.0
base_model:
  - Qwen/Qwen2.5-0.5B
library_name: peft
tags:
  - text-to-sql
  - table-question-answering
  - schema.org
  - lora
---

# Prereasoner Runtime Weights

This repository contains the public, manifest-pinned runtime weights for
[Prereasoner](https://github.com/ManiDoraisamy/prereasoner-data), an interpretable
table-question-answering system. The bundle's learned components produce semantic evidence: named
coordinates for columns and question words, and Schema.org property and class scores. Deterministic
code owns query construction, joins, calculation verification, rendering, and execution. No model in
this bundle writes SQL, and the bundle is not a standalone text-to-SQL model.

## Install

No Hugging Face account or token is required:

```bash
git clone https://github.com/ManiDoraisamy/prereasoner-data.git
cd prereasoner-data
python -m engine.fetch_weights
```

`engine/data/weights_manifest.json` pins this repository at an immutable commit and records the
SHA-256 digest of every required file. The fetcher downloads into a temporary directory, verifies
the complete bundle, and only then installs it.

## Bundle Contents

| File | Purpose |
|---|---|
| `encoder.pt` | State dictionary for the relational semantic readout |
| `encoder_meta.pt` | Readout allocation and constructor configuration |
| `qwen_lora/adapter_model.safetensors` | LoRA adapter for `Qwen/Qwen2.5-0.5B` |
| `qwen_lora/adapter_config.json` | PEFT adapter configuration |
| `anchor_assignment.npz` | Calibrated named-dimension thresholds |
| `primitives.npz` | Learned primitive-composition head |
| `schema_property_head.pt` | Calibrated Schema.org named-property evidence head |

The source repository contains the small ontology, calibration, taxonomy, and manifest artifacts.
The base Qwen model is downloaded separately from its publisher. `python -m engine.fetch_weights`
downloads only the files the source manifest lists. Revisions published while the trained own-data
readers served, including the one the current manifest pins, also hold their files under `readers/`
(XLM-R schema-item, reading and re-scoring models and their choice). The Decisions path replaced
those readers on 2026-11-20: the manifest no longer lists them, the fetcher does not download them,
and no code loads them. Prereasoner loads no local SQL-generating model; the retired SQL proposer
adapter (`sql_proposer/`) was removed from this repository on 2026-10-02 and remains in its history.

## Model Boundary

The encoder adapter emits named Schema.org property probabilities, calibrated class proposals, and
embeddings for column typing and the structural intent of composition and world questions. A released
class may propose a coarse resolver family, but no score can authorize a table, join, calculation, or
answer. Own-data questions take the Decisions path, which uses no file in this bundle: stock bge-m3
(pinned by hash in the source manifest) and the engine's word reader shortlist what a question may mean,
Gemini's Decisions API answers closed questions whose options the engine built with probabilities only,
and the engine's deterministic decoder builds every query it decides as a typed AST. An answer the
engine holds most likely wrong is retried with one SQLite query that Gemini 4 Neon writes, guarded,
run read-only and shown with its SQL; that is the one place a model writes SQL. Gemini also serves the
chat orchestrator, starter questions and reference generation. The Gemini models are external services
and are not part of this bundle.
Deterministic code applies ontology mapping, exact source grounding, typed constraints, abstention
rules, and execution checks.

Schema.org 30.0 supplies the named semantic coordinate system. Wikidata and publisher datasets
supply observations mapped into those coordinates; mutable source facts are not intended to be
memorized as answers.

## Evaluation And Provenance

See the source repository's
[model card](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/docs/MODEL_CARD.md),
[data card](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/docs/DATA_CARD.md), and
[Spider results](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/spider/results/RESULTS.md)
for component boundaries, denominators, and limitations.

The bytes in this bundle are immutable and hash-verified. The promoted Schema.org head has a
machine-readable corpus, split, seed, dependency, encoder, and held-out metric manifest. The shared
encoder's historical training run has less completely recorded source-corpus provenance; do not combine metrics from the two
tracks. All 926 Schema.org classes are representable, but only the released calibrated subset is
servable. Unsupported and under-calibrated coordinates abstain.

## Intended Use

- Semantic evidence for the matching Prereasoner source revision.
- Research on deterministic SQL planning informed by named learned dimensions.
- Local table and spreadsheet question answering with inspectable plans.

## Out Of Scope

- Standalone SQL or answer generation.
- Authoritative identity or entity classification.
- High-stakes medical, legal, tax, financial, or safety decisions without source-specific review.
- Using the files with an unverified or incompatible source revision.

## License

The Prereasoner weight bundle is released under Apache-2.0. The base model and source datasets retain
their own licenses and terms. The retired readers' files in `readers/` (not part of the current bundle)
are fine-tuned from XLM-RoBERTa (MIT) on Spider's train and dev questions (Yu et al., EMNLP 2018;
CC BY-SA 4.0), BIRD train (Li et al., NeurIPS 2023; CC BY-SA 4.0) and Spider-Syn (Gan et al., ACL
2021; MIT), and on questions Gemini on Vertex AI wrote over those training databases; keep those
attributions when redistributing them. The Spider test set appears in no training step. The retired
`sql_proposer/` adapter in earlier revisions was trained on Spider TRAIN (Yu et al., EMNLP 2018;
CC BY-SA 4.0); keep that attribution when redistributing it. See
[`THIRD_PARTY.md`](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/THIRD_PARTY.md).
