# Proposer training pipeline (Phase D)

The ONE pipeline for the grammar-constrained learned proposer experiment
(`spider/results/RESULTS.md`, Phase D). Steps:

1. `import_gold.py` — imports Spider TRAIN gold SQL into the engine's typed AST
   (`engine/sql_ast.py`) via sqlglot, keeping an example ONLY when the re-rendered AST
   executes on the capped tables with a strict denotation match against the gold execution.
   The measured import coverage is the ceiling of this phase and gates everything after it.
   Train gold is training data only; dev is never read here.
2. Supervised fine-tuning of a small causal model (Qwen2.5-0.5B class) on
   (schema serialization + question) → rendered-AST SQL targets. Frozen greedy decoding at
   inference; every proposal must re-import, validate, and execute before joining the
   candidate pool with a generation penalty — the proposer is a candidate SOURCE inside the
   one planner, never a second planner.
3. Evaluator injection for measurement (like `--rank-head`), promotion separate and
   user-approved. CPU serving requires a quantized runtime per the Phase D step-1 latency
   measurement (torch dynamic-int8 at 0.5B measured ~4s/query: too slow; llama.cpp/ONNX
   class runtime required).

Artifacts live ONLY in `training/proposer/data/experiments/<id>/` (gitignored).
