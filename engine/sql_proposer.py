"""The SQL proposer: a frozen LoRA adapter on Qwen2.5-0.5B that suggests candidate queries.

The deterministic search (engine/sql_search.py) builds candidates from typed parts; it cannot
reach shapes its grammar rules never enumerate. The proposer covers that gap. It reads one
prompt (engine/sql_prompt.py), decodes a fixed number of deterministic beams, and hands every
decoded line to the typed-AST importer (engine/sql_import.py). A line becomes a candidate only
if it imports, validates and re-renders through the engine's own renderer; everything else is
dropped. The raw model text never reaches a database.

The same model also scores SQL: ``likelihoods`` returns the teacher-forced log-probability of a
query given the prompt. The arbiter (engine/sql_rank.py) uses it to compare search candidates
and proposals on one scale.

All inference is fp32 on every device, because the arbiter was fit on fp32 likelihoods.
Decoding is greedy beam search (no sampling), so the proposer is deterministic for fixed
weights, inputs and numeric backend.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
from pathlib import Path

from engine import request_timing
from engine.artifact_provenance import sha256_tree
from engine.model_revisions import QWEN_MODEL_ID, QWEN_REVISION
from engine.sql_ast import render_query, validate_query
from engine.sql_candidate import ScoredQuery
from engine.sql_import import Unsupported, import_sql
from engine.sql_prompt import schema_prompt
from engine.sql_rank import proposal_score

# Padded target tokens per scoring forward pass: bounds the [rows, tokens, vocabulary] logits.
_SCORING_TOKENS = 512


class SQLProposer:
    # Model outputs depend only on the prompt (and the frozen weights), so they are cached on
    # the instance: a question planned twice in one request (the decomposition probe, then the
    # answer) or asked again pays for the model once. Bounded LRUs.
    _DECODE_CACHE_CAP = 256
    _LIKELIHOOD_CACHE_CAP = 16384

    def __init__(self, model, tokenizer, *, beams: int, max_new_tokens: int, device: str,
                 adapter_sha256: str):
        self.model = model
        self.tokenizer = tokenizer
        self.beams = beams
        self.max_new_tokens = max_new_tokens
        self.device = device
        self.adapter_sha256 = adapter_sha256
        self._decoded: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self._scored: OrderedDict[tuple[str, str], tuple[float, int]] = OrderedDict()

    @classmethod
    def load(cls, adapter_dir: str | Path, *, beams: int, max_new_tokens: int,
             device: str = "cpu") -> "SQLProposer":
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if device == "cuda":
            # fp32 means fp32: TF32 tensor-core matmuls would shift the likelihood features
            # the arbiter was fit on.
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
        tokenizer = AutoTokenizer.from_pretrained(QWEN_MODEL_ID, revision=QWEN_REVISION)
        base = AutoModelForCausalLM.from_pretrained(
            QWEN_MODEL_ID, revision=QWEN_REVISION, dtype=torch.float32,
        ).to(device)
        model = PeftModel.from_pretrained(base, str(adapter_dir)).eval()
        return cls(model, tokenizer, beams=beams, max_new_tokens=max_new_tokens,
                   device=device, adapter_sha256=sha256_tree(adapter_dir))

    def propose(self, tables: list[dict], question: str, graph, floor: float) -> list[ScoredQuery]:
        """Validated proposals in beam order (best first); possibly empty.

        `floor` is the lowest search-candidate score, so proposals rank after the search pool.
        A beam's position counts every decoded sequence, including ones that fail import, so a
        proposal's score depends only on where the model ranked it.
        """
        proposals, seen = [], set()
        for position, line in enumerate(self._beam_lines(schema_prompt(tables, question))):
            if not line:
                continue
            try:
                query = import_sql(line, graph)
                validate_query(query)
                rendered = render_query(query)
            except (Unsupported, TypeError, ValueError):
                continue
            if rendered in seen:
                continue
            seen.add(rendered)
            proposals.append(ScoredQuery(
                query, rendered, proposal_score(floor, position), (f"proposer:beam{position}",),
            ))
        request_timing.count("proposals", len(proposals))
        return proposals

    def likelihoods(self, tables: list[dict], question: str,
                    sqls: list[str]) -> list[tuple[float, int]]:
        """Teacher-forced (log-probability, token count) of each SQL after the proposer prompt.

        The prompt is encoded once and its key/value cache is shared by every query; queries
        are scored in padded batches. Each row's causal attention never reaches its own padding,
        so batching changes cost, not values. An empty string scores (-inf, 0).
        """
        prompt = schema_prompt(tables, question)
        known = {}
        for sql in dict.fromkeys(sqls):
            value = self._scored.get((prompt, sql))
            if value is not None:
                self._scored.move_to_end((prompt, sql))
                known[sql] = value
        missing = [sql for sql in dict.fromkeys(sqls) if sql not in known]
        if missing:
            request_timing.count("likelihood_sql", len(missing))
            with request_timing.span("likelihood"):
                fresh = self._score(prompt, missing)
            for sql, value in zip(missing, fresh):
                known[sql] = value
                self._scored[(prompt, sql)] = value
            while len(self._scored) > self._LIKELIHOOD_CACHE_CAP:
                self._scored.popitem(last=False)
        return [known[sql] for sql in sqls]

    def _beam_lines(self, prompt: str) -> tuple[str, ...]:
        """The first line of every decoded beam, best beam first (cached per prompt)."""
        cached = self._decoded.get(prompt)
        if cached is not None:
            self._decoded.move_to_end(prompt)
            return cached
        with request_timing.span("propose"):
            lines = self._decode(prompt)
        self._decoded[prompt] = lines
        while len(self._decoded) > self._DECODE_CACHE_CAP:
            self._decoded.popitem(last=False)
        return lines

    def _decode(self, prompt: str) -> tuple[str, ...]:
        """Deterministic beam search; the first line of each returned sequence."""
        import torch

        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        with torch.inference_mode():
            sequences = self.model.generate(
                **inputs, max_new_tokens=self.max_new_tokens, do_sample=False,
                num_beams=self.beams, num_return_sequences=self.beams,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        prompt_length = inputs.input_ids.shape[1]
        lines = []
        for sequence in sequences:
            text = self.tokenizer.decode(sequence[prompt_length:], skip_special_tokens=True).strip()
            lines.append(text.splitlines()[0].strip() if text else "")
        return tuple(lines)

    def _score(self, prompt: str, sqls: list[str]) -> list[tuple[float, int]]:
        import torch

        prompt_ids = self.tokenizer(prompt, add_special_tokens=False).input_ids
        targets = [self.tokenizer(sql, add_special_tokens=False).input_ids for sql in sqls]
        scores: list[tuple[float, int]] = [(float("-inf"), 0)] * len(sqls)
        with torch.inference_mode():
            encoded = self.model(input_ids=torch.tensor([prompt_ids], device=self.device),
                                 use_cache=True)
            first = torch.log_softmax(encoded.logits[0, -1], dim=-1)
            for rows in self._scoring_batches([i for i, target in enumerate(targets) if target],
                                              targets):
                self._score_batch(rows, targets, encoded.past_key_values, len(prompt_ids), first,
                                  scores)
        return scores

    @staticmethod
    def _scoring_batches(indexes: list[int], targets: list[list[int]]):
        batch: list[int] = []
        longest = 0
        for index in indexes:
            length = len(targets[index]) - 1
            if batch and (len(batch) + 1) * max(longest, length) > _SCORING_TOKENS:
                yield batch
                batch, longest = [], 0
            batch.append(index)
            longest = max(longest, length)
        if batch:
            yield batch

    def _score_batch(self, rows, targets, prompt_cache, prompt_length, first, scores):
        import torch

        width = max(len(targets[index]) - 1 for index in rows)
        if width == 0:
            for index in rows:
                scores[index] = (float(first[targets[index][0]]), 1)
            return
        pad = self.tokenizer.eos_token_id
        input_ids = torch.full((len(rows), width), pad, dtype=torch.long)
        attention = torch.zeros((len(rows), prompt_length + width), dtype=torch.long)
        attention[:, :prompt_length] = 1
        for row, index in enumerate(rows):
            continuation = targets[index][:-1]
            input_ids[row, :len(continuation)] = torch.tensor(continuation)
            attention[row, prompt_length:prompt_length + len(continuation)] = 1
        cache = copy.deepcopy(prompt_cache)
        cache.batch_repeat_interleave(len(rows))
        output = self.model(input_ids=input_ids.to(self.device),
                            attention_mask=attention.to(self.device), past_key_values=cache)
        logprobs = torch.log_softmax(output.logits, dim=-1)
        for row, index in enumerate(rows):
            target = targets[index]
            total = float(first[target[0]])
            steps = len(target) - 1
            if steps:
                picked = logprobs[row, torch.arange(steps, device=self.device),
                                  torch.tensor(target[1:], device=self.device)]
                total += float(picked.sum())
            scores[index] = (total, len(target))
