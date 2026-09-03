---
license: apache-2.0
base_model: Qwen/Qwen2.5-0.5B
library_name: peft
tags:
  - text-to-sql
  - table-question-answering
  - schema.org
  - lora
---

# PreReasoner Runtime Weights

This repository contains the public, manifest-pinned runtime weights for
[PreReasoner](https://github.com/ManiDoraisamy/prereasoner-data), an interpretable
table-question-answering system. Learned components produce semantic evidence; a deterministic,
typed AST planner owns SQL construction, joins, calculation verification, rendering, and execution.
The bundle is not a standalone text-generating model.

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
The base Qwen model is downloaded separately from its publisher.

## Model Boundary

The model emits named Schema.org property probabilities, calibrated class proposals, embeddings for
structural intent and ranking, and calculation operand signals. A released class may propose a
coarse resolver family, but no score can authorize a table, join, calculation, or answer.
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

- Semantic evidence for the matching PreReasoner source revision.
- Research on deterministic SQL planning informed by named learned dimensions.
- Local table and spreadsheet question answering with inspectable plans.

## Out Of Scope

- Standalone SQL or answer generation.
- Authoritative identity or entity classification.
- High-stakes medical, legal, tax, financial, or safety decisions without source-specific review.
- Using the files with an unverified or incompatible source revision.

## License

The PreReasoner weight bundle is released under Apache-2.0. The base model and source datasets retain
their own licenses and terms; see
[`THIRD_PARTY.md`](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/THIRD_PARTY.md).
