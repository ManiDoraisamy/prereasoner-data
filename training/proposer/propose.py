"""Candidate-source runtime for a trained proposer adapter (evaluator injection only).

Greedy, frozen, deterministic: one decode per question, parsed back through the SAME
importer that built the training targets, validated by the engine's own validator, and
appended to the pool with a generation penalty — the proposer proposes, it never selects.
Lives in training/ until a promotion moves it into the engine with an ownership-map update.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.sql_ast import render_query, validate_query
from engine.sql_candidate import ScoredQuery
from training.proposer import BASE_MODEL_ID, BASE_MODEL_REVISION
from training.proposer.import_gold import Unsupported, import_gold_sql
from training.proposer.serialize import sample_column_values, schema_prompt

# Same scale as the parsimony expander: pooled for selection, never outranking the
# deterministic order on the prior alone.
_PROPOSER_PENALTY = 5.0


class Proposer:
    def __init__(self, model, tokenizer, base_id, adapter_dir):
        self.model = model
        self.tokenizer = tokenizer
        self.base_id = base_id
        self.adapter_dir = adapter_dir

    @classmethod
    def load(cls, adapter_dir, base_id=BASE_MODEL_ID, revision=BASE_MODEL_REVISION):
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(base_id, revision=revision)
        base = AutoModelForCausalLM.from_pretrained(base_id, revision=revision,
                                                    torch_dtype=torch.float32)
        model = PeftModel.from_pretrained(base, adapter_dir)
        model.eval()
        return cls(model, tokenizer, base_id, adapter_dir)

    def propose(self, tables, question, graph, min_score=0.0, beams=1):
        """Return validated typed-AST candidates in beam-score order (possibly empty).

        beams=1 is greedy (d1/d2 behavior). With beams>1, deterministic beam search returns
        up to `beams` sequences best-first; each valid import becomes a candidate, so the
        first list element is always the beam-best proposal."""
        import torch

        # include_values must match the prompt format the adapter was TRAINED with
        # (bare = d1/d2, value-linked = d4+); the evaluator flag sets it per run.
        values = (sample_column_values(tables)
                  if getattr(self, "include_values", False) else None)
        prompt = schema_prompt(tables, question, values)
        inputs = self.tokenizer(prompt, return_tensors="pt")
        generate_args = dict(max_new_tokens=96, do_sample=False,
                             pad_token_id=self.tokenizer.eos_token_id)
        if beams > 1:
            generate_args.update(num_beams=beams, num_return_sequences=beams)
        with torch.no_grad():
            output = self.model.generate(**inputs, **generate_args)
        proposals, seen = [], set()
        for sequence_index, sequence in enumerate(output):
            text = self.tokenizer.decode(sequence[inputs.input_ids.shape[1]:],
                                         skip_special_tokens=True).strip()
            sql = text.splitlines()[0].strip() if text else ""
            if not sql:
                continue
            try:
                query = import_gold_sql(sql, graph)
                validate_query(query)
                rendered = render_query(query)
            except (Unsupported, TypeError, ValueError):
                continue
            if rendered in seen:
                continue
            seen.add(rendered)
            # beam order preserved; later beams sit deeper below the deterministic order
            proposals.append(ScoredQuery(
                query, rendered,
                min_score - _PROPOSER_PENALTY - 0.1 * sequence_index,
                (f"proposer:beam{sequence_index}" if beams > 1 else "proposer:greedy",),
            ))
        return proposals
