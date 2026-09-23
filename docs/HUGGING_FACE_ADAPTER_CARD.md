---
base_model: Qwen/Qwen2.5-0.5B
library_name: peft
license: apache-2.0
tags:
  - base_model:adapter:Qwen/Qwen2.5-0.5B
  - lora
---

# Prereasoner Qwen Adapters

The bundle carries two LoRA adapters on `Qwen/Qwen2.5-0.5B`, each one component of the
[Prereasoner runtime bundle](https://huggingface.co/prereasoner/prereasoner-weights):

- `qwen_lora/` adapts the base as an encoder whose representation feeds named semantic readouts.
- `sql_proposer/` adapts the base as a causal LM that decodes candidate SQL for own-data questions.
  Prereasoner maps each candidate into its typed AST, validates it, and lets a fitted arbiter choose
  among candidates that execute; the adapter is not a standalone text-to-SQL generator.

Install and validate the complete compatible bundle through the source repository:

```bash
python -m engine.fetch_weights
```

Do not copy this subdirectory independently or mix it with a different bundle revision. The source
manifest pins the adapter, readouts, thresholds, and ontology artifacts together. Training data,
evaluation boundaries, known provenance gaps, intended use, and third-party notices are documented
in the [source model card](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/docs/MODEL_CARD.md)
and [`THIRD_PARTY.md`](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/THIRD_PARTY.md).
