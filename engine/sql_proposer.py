"""Propose validated SQL candidates using a frozen, manifest-pinned model runtime.

The deterministic search (engine/sql_search.py) builds candidates from typed parts; it cannot
reach shapes its grammar rules never enumerate. The proposer covers that gap. It reads one
prompt (engine/sql_prompt.py), decodes a fixed number of deterministic beams, and hands every
decoded line to the typed-AST importer (engine/sql_import.py). A line becomes a candidate only
if it imports, validates and re-renders through the engine's own renderer; everything else is
dropped. The raw model text never reaches a database.

The Hugging Face runtime also scores SQL with teacher-forced log-probabilities. The GGUF CPU
runtime currently exposes an explicit neutral-likelihood control and must not be paired with an
arbiter calibrated for likelihood scores.

Each runtime declares its numeric/scoring contract. Decoding is deterministic for fixed weights,
inputs and backend; exact token output can still vary across numeric backends.
"""
from __future__ import annotations

import copy
import json
from collections import OrderedDict
from pathlib import Path

from engine import request_timing
from engine.artifact_provenance import adapter_sha256
from engine.model_revisions import QWEN_MODEL_ID, QWEN_REVISION
from engine.sql_ast import render_query, validate_query
from engine.sql_candidate import ScoredQuery
from engine.sql_import import Unsupported, import_sql
from engine.sql_prompt import schema_prompt
from engine.sql_rank import proposal_score

# Padded target tokens per scoring forward pass: bounds the [rows, tokens, vocabulary] logits.
_SCORING_TOKENS = 512


def normalize_decoded_sql(text: str) -> str:
    """Remove an optional Markdown fence without truncating multiline SQL."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def validate_proposer_arbiter_contract(arbiter_payload: dict, proposer_identity: dict,
                                       arbiter_sha256: str) -> None:
    """Fail closed when the shipped proposer is not the one named by the arbiter artifact."""
    fit = arbiter_payload.get("fit")
    if not isinstance(fit, dict) or not isinstance(fit.get("proposer_adapter_sha256"), str):
        raise TypeError("arbiter artifact does not identify its fitted proposer")
    fitted_identity = fit["proposer_adapter_sha256"]
    if proposer_identity.get("backend") == "llama-cpp-gguf-lora":
        if proposer_identity.get("arbiter_sha256") != arbiter_sha256:
            raise ValueError("GGUF runtime was prepared for a different arbiter artifact")
        if proposer_identity.get("arbiter_fit_proposer_sha256") != fitted_identity:
            raise ValueError("GGUF runtime disagrees with the arbiter's fitted proposer identity")
        if proposer_identity.get("model_matched_arbiter") is True and (
            proposer_identity.get("selected_peft_adapter_sha256") != fitted_identity
        ):
            raise ValueError("GGUF runtime claims a model match but adapter identities differ")
        if proposer_identity.get("model_matched_arbiter") is True and (
            not isinstance(fit.get("likelihood_protocol"), str)
            or proposer_identity.get("likelihood_protocol") != fit["likelihood_protocol"]
        ):
            raise ValueError("GGUF runtime scoring protocol differs from the arbiter fit")
        return
    if proposer_identity.get("adapter_sha256") != fitted_identity:
        raise ValueError("proposer adapter differs from the adapter used to fit the arbiter")


def validate_proposer_runtime_pin(weights_manifest: dict, runtime_sha256: str) -> None:
    """Require the proposer runtime contract itself to be covered by the bundle manifest."""
    runtime_path = "sql_proposer/runtime.json"
    expected = (weights_manifest.get("files") or {}).get(runtime_path)
    if expected is None:
        record = (weights_manifest.get("committed_artifacts") or {}).get(runtime_path)
        expected = record.get("sha256") if isinstance(record, dict) else None
    if not isinstance(expected, str):
        raise TypeError(f"weights manifest does not pin {runtime_path}")
    if expected != runtime_sha256:
        raise ValueError(f"weights manifest SHA-256 mismatch for {runtime_path}")


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
             device: str = "cpu", data_dir: str | Path | None = None) -> SQLProposer:
        adapter_dir = Path(adapter_dir)
        runtime_path = adapter_dir / "runtime.json"
        if runtime_path.is_file():
            return GGUFSQLProposer.load(
                runtime_path,
                bundle_dir=data_dir or adapter_dir.parent,
                expected_beams=beams,
                expected_max_new_tokens=max_new_tokens,
            )
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
                   device=device, adapter_sha256=adapter_sha256(adapter_dir))

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
            lines.append(normalize_decoded_sql(text))
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


class GGUFSQLProposer:
    """CPU llama.cpp proposer loaded from a hash-pinned bundle runtime manifest.

    It exposes the same typed-SQL proposal and likelihood methods as SQLProposer. Runtime
    manifests must disclose selector mismatch; mismatched candidates are refused in production.
    """

    def __init__(self, model, tokenizer, *, runtime: dict, model_fingerprint: str,
                 max_new_tokens: int, context: int, threads: int):
        self.model = model
        self.tokenizer = tokenizer
        self.runtime = runtime
        self.beams = len(runtime["prompt_variants"])
        self.max_new_tokens = max_new_tokens
        self.context = context
        self.threads = threads
        self.device = "cpu"
        self.adapter_sha256 = model_fingerprint
        self.last_generation_diagnostics: list[dict] = []
        self._decoded: OrderedDict[str, tuple[str, ...]] = OrderedDict()

    @classmethod
    def load(cls, runtime_path: str | Path, *, expected_beams: int,
             expected_max_new_tokens: int, bundle_dir: str | Path | None = None
             ) -> GGUFSQLProposer:
        from engine.artifact_provenance import (
            canonical_json_sha256,
            sha256_file,
            sha256_tree,
        )
        from engine.config import APP_ENV

        runtime_path = Path(runtime_path).resolve()
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        if runtime.get("version") != 1 or runtime.get("backend") != "llama-cpp-gguf-lora":
            raise ValueError(f"unsupported SQL proposer runtime manifest: {runtime_path}")
        if runtime.get("likelihood_protocol") != "neutral-sentinel-v1":
            raise ValueError("this arbiter has only been evaluated with neutral likelihoods")
        if not isinstance(runtime.get("model_matched_arbiter"), bool):
            raise TypeError("runtime manifest must declare model_matched_arbiter")
        if not runtime["model_matched_arbiter"] and APP_ENV not in {"development", "test"}:
            raise RuntimeError(
                "refusing an uncalibrated SQL proposer/arbiter pair outside development/test"
            )
        if int(runtime.get("max_new_tokens", -1)) != expected_max_new_tokens:
            raise ValueError("runtime token limit differs from the fitted pool contract")
        variants = runtime.get("prompt_variants")
        if not isinstance(variants, list) or not variants or any(
            not isinstance(value, str) or not value.strip() for value in variants
        ):
            raise ValueError("runtime manifest requires ordered non-empty prompt variants")
        if len(variants) != expected_beams:
            raise ValueError(
                f"arbiter expects {expected_beams} proposer outputs; runtime has {len(variants)}"
            )

        bundle_dir = Path(bundle_dir or runtime_path.parent.parent).resolve()

        def checked_artifact(key: str) -> Path:
            relative = runtime.get(key)
            digest = runtime.get(f"{key}_sha256")
            if not isinstance(relative, str) or not isinstance(digest, str):
                raise TypeError(f"runtime manifest lacks {key} path/hash")
            path = (bundle_dir / relative).resolve()
            if bundle_dir not in path.parents:
                raise ValueError(f"{key} must remain inside the model-data bundle")
            if not path.is_file():
                raise FileNotFoundError(path)
            if sha256_file(path) != digest:
                raise ValueError(f"{key} SHA-256 mismatch")
            return path

        model_path = checked_artifact("model_file")
        lora_path = checked_artifact("lora_file")
        base_id = runtime.get("tokenizer_model_id")
        revision = runtime.get("tokenizer_revision")
        if not isinstance(base_id, str) or not isinstance(revision, str):
            raise TypeError("runtime manifest must pin tokenizer model and revision")
        tokenizer_relative = runtime.get("tokenizer_dir")
        tokenizer_digest = runtime.get("tokenizer_dir_sha256")
        if not isinstance(tokenizer_relative, str) or not isinstance(tokenizer_digest, str):
            raise TypeError("runtime manifest must pin a tokenizer snapshot directory")
        tokenizer_path = (bundle_dir / tokenizer_relative).resolve()
        if bundle_dir not in tokenizer_path.parents:
            raise ValueError("tokenizer snapshot must remain inside the model-data bundle")
        if not tokenizer_path.is_dir():
            raise FileNotFoundError(tokenizer_path)
        if sha256_tree(tokenizer_path) != tokenizer_digest:
            raise ValueError("tokenizer snapshot SHA-256 mismatch")
        context = int(runtime.get("context_tokens", 0))
        threads = int(runtime.get("cpu_threads", 0))
        if not 512 <= context <= 8192 or not 1 <= threads <= 16:
            raise ValueError("runtime context or CPU-thread limit is outside the supported range")
        fingerprint = canonical_json_sha256({
            "protocol": "cpu-gguf-lora-model-v1",
            "base_sha256": runtime["model_file_sha256"],
            "lora_sha256": runtime["lora_file_sha256"],
            "lora_scale": float(runtime.get("lora_scale", 1.0)),
        })

        from llama_cpp import Llama
        from transformers import AutoTokenizer

        model = Llama(
            model_path=str(model_path),
            n_gpu_layers=0,
            n_ctx=context,
            n_threads=threads,
            n_threads_batch=threads,
            n_batch=256,
            n_ubatch=256,
            seed=0,
            lora_path=str(lora_path),
            lora_scale=float(runtime.get("lora_scale", 1.0)),
            verbose=False,
            logits_all=False,
        )
        tokenizer = AutoTokenizer.from_pretrained(
            str(tokenizer_path), local_files_only=True
        )
        return cls(
            model, tokenizer, runtime=runtime, model_fingerprint=fingerprint,
            max_new_tokens=expected_max_new_tokens, context=context, threads=threads,
        )

    def _prompt(self, tables: list[dict], question: str, variant: str) -> str:
        from engine.sql_prompt import schema_prompt

        messages = [
            {"role": "system", "content": self.runtime["system_prompt"]},
            {"role": "user", "content": schema_prompt(tables, question).rstrip()
             + "\n" + variant},
        ]
        template_options = {"tokenize": False, "add_generation_prompt": True}
        thinking = self.runtime.get("thinking")
        if thinking is not None:
            template_options["enable_thinking"] = bool(thinking)
        prompt = self.tokenizer.apply_chat_template(messages, **template_options)
        if not isinstance(prompt, str) or not prompt:
            raise RuntimeError("pinned tokenizer returned an empty rendered prompt")
        return prompt

    def propose(self, tables: list[dict], question: str, graph, floor: float):
        from engine.sql_ast import render_query, validate_query
        from engine.sql_candidate import ScoredQuery
        from engine.sql_import import Unsupported, import_sql

        proposals, seen, diagnostics = [], set(), []
        for position, variant in enumerate(self.runtime["prompt_variants"]):
            prompt = self._prompt(tables, question, variant)
            cached = self._decoded.get(prompt)
            if cached is not None:
                self._decoded.move_to_end(prompt)
                raw_text = cached[0]
            else:
                result = self.model.create_completion(
                    prompt=prompt,
                    max_tokens=self.max_new_tokens,
                    temperature=0.0,
                    top_p=1.0,
                    seed=0,
                    stop=[self.tokenizer.eos_token] if self.tokenizer.eos_token else [],
                )
                choice = result["choices"][0]
                raw_text = str(choice.get("text", ""))
                self._decoded[prompt] = (raw_text,)
                while len(self._decoded) > 256:
                    self._decoded.popitem(last=False)
            sql = normalize_decoded_sql(raw_text)
            diagnostic = {"variant_index": position, "raw_text": raw_text,
                          "normalized_sql": sql,
                          "status": "empty" if not sql else "pending_validation"}
            diagnostics.append(diagnostic)
            if not sql:
                continue
            try:
                query = import_sql(sql, graph)
                validate_query(query)
                canonical = render_query(query)
            except (Unsupported, TypeError, ValueError) as exc:
                diagnostic.update(status="rejected", failure_stage="import_or_validate",
                                  rejection=f"{type(exc).__name__}: {str(exc)[:500]}")
                continue
            if canonical in seen:
                diagnostic.update(status="duplicate", canonical_sql=canonical)
                continue
            seen.add(canonical)
            diagnostic.update(status="accepted", canonical_sql=canonical)
            proposals.append(ScoredQuery(
                query, canonical, proposal_score(floor, position),
                (f"proposer:variant{position}",),
            ))
        self.last_generation_diagnostics = diagnostics
        request_timing.count("proposals", len(proposals))
        return proposals

    def likelihoods(self, tables: list[dict], question: str,
                    sqls: list[str]) -> list[tuple[float, int]]:
        # The saved full-DEV candidate used neutral likelihoods. Keep this explicit until a
        # model-matched arbiter is trained and accepted.
        return [(0.0, 1) for _ in sqls]
