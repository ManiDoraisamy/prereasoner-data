# SQL proposer training

This pipeline produces the proposer adapter that serving loads from `engine/data/sql_proposer/`
(`engine/sql_proposer.py`). The proposer is a LoRA adapter on the pinned Qwen2.5-0.5B base. It
suggests candidate SQL; serving accepts a suggestion only if it imports into the typed AST and
validates, and a fitted arbiter decides whether it beats the search's candidates
(`training/rank/`).

## Steps

1. **Targets** — `import_gold.py` maps Spider TRAIN gold SQL into the typed AST with the serving
   importer (`engine/sql_import.py`). An example becomes a target only when the rendered AST executes
   on the capped tables and strictly matches the gold execution, so the targets are exactly the SQL the
   served proposer is allowed to emit. Dev is never read.

   ```bash
   python -m training.proposer.import_gold \
       --targets training/proposer/data/experiments/<id>/targets.jsonl
   ```

2. **Fine-tune** — `train_sft.py` trains the adapter on (serving prompt, rendered SQL + EOS) pairs:
   seed 7, prompt tokens masked from the loss, databases in the `is_validation_db` bucket held out.
   The prompt is `engine/sql_prompt.py:schema_prompt`, the one serving uses. GPU leases go through
   `training/tools/runpod_api.py lease`.

   ```bash
   python -m training.proposer.train_sft \
       --targets training/proposer/data/experiments/<id>/targets.jsonl \
       --out-dir training/proposer/data/experiments/<id> --max-steps 6000
   ```

3. **Scorer check** — after any change to the likelihood code, `verify_scorer.py` confirms the
   batched scorer equals one-at-a-time scoring exactly and matches a naive full forward pass.

   ```bash
   python -m training.proposer.verify_scorer
   ```

4. **Arbiter and promotion** — label pools with the candidate adapter and fit an arbiter
   (`training/rank/README.md`); `training/rank/promote.py` installs the adapter and its arbiter
   together, because an arbiter is only valid for the adapter whose pools it was fit on.

Artifacts live only in `training/proposer/data/experiments/<id>/` (gitignored). Never train into
`engine/data/`.

## The shipped adapter

The served adapter is experiment `d2`: 6,000 steps at effective batch 16 on the 90.0%-importable Spider
TRAIN targets, seed 7. Its measured effect on the served pipeline is in
`spider/results/RESULTS.md`, and its runtime identity (sha256 of `sql_proposer/`) is recorded in
`engine/data/sql_arbiter.json` under `fit.proposer_adapter_sha256`.
