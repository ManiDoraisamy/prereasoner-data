---
base_model: Qwen/Qwen2.5-0.5B
library_name: peft
license: apache-2.0
tags:
  - base_model:adapter:Qwen/Qwen2.5-0.5B
  - lora
---

# Prereasoner Qwen Adapter

This LoRA adapter is one component of the
[Prereasoner runtime bundle](https://huggingface.co/prereasoner/prereasoner-weights). It is not a
standalone text-to-SQL generator. Prereasoner uses the adapted Qwen2.5-0.5B representation as input
to named semantic readouts; deterministic planner code owns SQL construction and execution.

Install and validate the complete compatible bundle through the source repository:

```bash
python -m engine.fetch_weights
```

Do not copy this subdirectory independently or mix it with a different bundle revision. The source
manifest pins the adapter, readouts, thresholds, and ontology artifacts together. Training data,
evaluation boundaries, known provenance gaps, intended use, and third-party notices are documented
in the [source model card](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/docs/MODEL_CARD.md)
and [`THIRD_PARTY.md`](https://github.com/ManiDoraisamy/prereasoner-data/blob/main/THIRD_PARTY.md).
