"""CPU inference adapter for the pinned XiYanSQL Q4_K_M production proposer."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import weakref
from collections import OrderedDict
from pathlib import Path

from engine import request_timing
from engine.artifact_provenance import sha256_file
from engine.sql_ast import render_query, validate_query
from engine.sql_candidate import ScoredQuery
from engine.sql_import import Unsupported, import_sql, normalize_decoded_sql
from engine.sql_prompt import xiyansql_prompt
from engine.sql_rank import proposal_score

_CONTRACT = Path(__file__).resolve().parent / "data" / "xiyan_sql_proposer.json"
_DEFAULT_MODEL = Path(__file__).resolve().parent / "data" / "xiyan_sql_proposer.gguf"
QUEUE_TIMEOUT_SECONDS = 15.0
DECODE_TIMEOUT_SECONDS = 60.0


class SQLProposerUnavailable(RuntimeError):
    """Inference admission or execution exhausted its bounded CPU budget."""


class SQLDecodeBudgetExceeded(SQLProposerUnavailable):
    """One decode ran past DECODE_TIMEOUT_SECONDS. Greedy decoding of the same prompt takes as long
    again, so a retry cannot help: the selection serves its search pool without proposals and
    records the abstention (engine/tables.py:TableQuery.select_query)."""


def load_contract(path: str | Path = _CONTRACT) -> dict:
    contract = json.loads(Path(path).read_text(encoding="utf-8"))
    if contract.get("version") != 1 or contract.get("backend") != "llama-cpp-gguf":
        raise RuntimeError("unsupported XiYanSQL proposer contract")
    if contract.get("likelihood_policy") != "neutral-sentinel-v1":
        raise RuntimeError("the production XiYanSQL contract requires the measured neutral policy")
    if contract.get("prompt") != "xiyansql-m-schema-v1":
        raise RuntimeError("unsupported XiYanSQL prompt contract")
    if contract.get("selector", {}).get("likelihood_policy") != "neutral-sentinel-v1":
        raise RuntimeError("XiYanSQL selector contract has an unsupported likelihood policy")
    selector = contract["selector"]
    if type(selector.get("model_matched_arbiter")) is not bool:
        raise RuntimeError("the selector must explicitly disclose whether its proposer is matched")
    for field in ("sha256", "fit_source_proposer_sha256"):
        if not isinstance(selector.get(field), str) or not re.fullmatch(
            r"[0-9a-f]{64}", selector[field]
        ):
            raise RuntimeError(f"XiYanSQL selector contract has an invalid {field}")
    artifact = contract.get("gguf")
    if (not isinstance(artifact, dict)
            or not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256", "")))
            or not isinstance(artifact.get("size_bytes"), int)
            or artifact["size_bytes"] <= 0
            or artifact.get("quantization") != "Q4_K_M"
            or artifact.get("license") != "Apache-2.0"):
        raise RuntimeError("XiYanSQL GGUF contract is incomplete or unsupported")
    if (selector["model_matched_arbiter"]
            and selector["fit_source_proposer_sha256"] != artifact["sha256"]):
        raise RuntimeError("selector claims a model match but its fit proposer differs from the GGUF")
    generation = contract.get("generation")
    if (not isinstance(generation, dict)
            or generation.get("temperature") != 0.0
            or generation.get("top_p") != 1.0
            or generation.get("seed") != 0
            or not 1 <= int(generation.get("max_new_tokens", 0)) <= 1024
            or generation.get("stop") != "tokenizer_eos"):
        raise RuntimeError("XiYanSQL generation contract is not the measured deterministic policy")
    runtime = contract.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("gpu_layers") != 0:
        raise RuntimeError("the production XiYanSQL proposer must run on CPU")
    if (not 512 <= int(runtime.get("context", 0)) <= 8192
            or not 1 <= int(runtime.get("threads", 0)) <= 16
            or not 1 <= int(runtime.get("batch_size", 0)) <= 2048):
        raise RuntimeError("XiYanSQL CPU runtime limits are outside the supported bounds")
    tokenizer = contract.get("tokenizer")
    if (not isinstance(tokenizer, dict)
            or not isinstance(tokenizer.get("repository"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", str(tokenizer.get("revision", "")))):
        raise RuntimeError("XiYanSQL tokenizer must be pinned to an immutable revision")
    return contract


def effective_cpu_threads(contract: dict, environ=None) -> int:
    """Return the exact llama.cpp CPU thread count, rejecting unsafe overrides.

    The run contract's default is part of model provenance; deployments may
    override it for a measured host, but the effective value must be recorded
    by evaluation and remain within the runtime's supported range.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get("SQL_PROPOSER_THREADS")
    threads = int(raw) if raw is not None else int(contract["runtime"]["threads"])
    if not 1 <= threads <= 16:
        raise ValueError("SQL_PROPOSER_THREADS must be between 1 and 16")
    return threads


class XiYanSQLProposer:
    """Implements the SQLProposer interface while preserving the experiment's exact model path.

    Decoding runs under one lock because a llama.cpp context is mutable and must not be used by
    concurrent requests at the same time. Correctness labels are never passed to this class.
    """

    _DECODE_CACHE_CAP = 256

    def __init__(self, model, tokenizer, contract: dict, model_sha256: str):
        self.model = model
        self.tokenizer = tokenizer
        self.contract = contract
        self.model_sha256 = model_sha256
        # Kept for the generic planner provenance interface; this is a GGUF model, not a LoRA.
        self.adapter_sha256 = model_sha256
        self.device = "cpu"
        self.beams = 1
        self.max_new_tokens = int(contract["generation"]["max_new_tokens"])
        self._decoded: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self._model_lock = threading.Lock()
        self._deadline = None
        self._closed = False

    @staticmethod
    def read_contract(path: str | Path = _CONTRACT) -> dict:
        return load_contract(path)

    @classmethod
    def load(cls, model_path: str | Path | None = None, *, contract_path=None):
        contract = load_contract(contract_path or _CONTRACT)
        path = Path(model_path or os.environ.get("SQL_PROPOSER_MODEL_PATH") or _DEFAULT_MODEL)
        expected = contract["gguf"]["sha256"]
        if not path.is_file():
            raise FileNotFoundError(f"pinned XiYanSQL model is missing: {path}")
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"XiYanSQL model hash mismatch: expected {expected}, got {actual}")

        from llama_cpp import Llama
        from transformers import AutoTokenizer

        runtime = contract["runtime"]
        generation = contract["generation"]
        threads = effective_cpu_threads(contract)
        tokenizer = AutoTokenizer.from_pretrained(
            contract["tokenizer"]["repository"],
            revision=contract["tokenizer"]["revision"],
            local_files_only=True,
        )
        model = Llama(
            model_path=str(path.resolve()),
            n_ctx=int(runtime["context"]),
            n_threads=threads,
            n_threads_batch=threads,
            n_batch=int(runtime["batch_size"]),
            n_ubatch=min(int(runtime["batch_size"]), 512),
            seed=int(generation["seed"]),
            n_gpu_layers=0,
            logits_all=False,
            verbose=False,
        )
        instance = cls(model, tokenizer, contract, actual)
        # The pinned CPU backend supports cooperative cancellation inside native
        # decode, including prompt evaluation. Retain the callback until close.
        from llama_cpp import llama_cpp
        reference = weakref.ref(instance)
        def should_abort(_):
            owner = reference()
            return owner is None or owner._expired()
        instance._abort_callback = llama_cpp.ggml_abort_callback(should_abort)
        llama_cpp.llama_set_abort_callback(model._ctx.ctx, instance._abort_callback, None)
        # A strong atexit-bound method would retain every temporary proposer in
        # model-backed tests. Finalize on collection OR before interpreter exit.
        instance._finalizer = weakref.finalize(instance, model.close)
        return instance

    def _expired(self):
        return self._deadline is not None and time.monotonic() >= self._deadline

    def close(self):
        """Release native resources before interpreter teardown; safe to call twice."""
        with self._model_lock:
            if not self._closed:
                finalizer = getattr(self, "_finalizer", None)
                if finalizer is not None:
                    finalizer()
                else:
                    self.model.close()
                self._closed = True
                self._decoded.clear()

    def propose(self, tables, question: str, graph, floor: float) -> list[ScoredQuery]:
        prompt = xiyansql_prompt(self.tokenizer, graph, question)
        # Lookup and insertion share the model lock. A waiter must re-check the
        # cache after the previous request finishes, and eviction must not race
        # another request's move_to_end.
        with request_timing.span("propose_queue"):
            acquired = self._model_lock.acquire(timeout=QUEUE_TIMEOUT_SECONDS)
        if not acquired:
            request_timing.count("proposer_queue_rejections")
            raise SQLProposerUnavailable("SQL model is busy; retry shortly")
        try:
            if self._closed:
                raise SQLProposerUnavailable("SQL model is closed")
            cached = self._decoded.get(prompt)
            if cached is not None:
                self._decoded.move_to_end(prompt)
                request_timing.count("proposer_cache_hits", 1)
                lines = cached
            else:
                config = self.contract["generation"]
                # Reserve the entire output budget. Two additional positions
                # conservatively cover backend-added BOS/EOS tokens; do not let
                # llama.cpp silently shorten the configured completion budget.
                prompt_tokens = len(self.model.tokenize(
                    prompt.encode("utf-8"), add_bos=False, special=True))
                if prompt_tokens + 2 + self.max_new_tokens > int(self.contract["runtime"]["context"]):
                    request_timing.count("proposer_context_rejections", 1)
                    return []
                with request_timing.span("propose"):
                    request_timing.count("proposer_decodes", 1)
                    self._deadline = time.monotonic() + DECODE_TIMEOUT_SECONDS
                    try:
                        result = self.model.create_completion(
                            prompt=prompt,
                            max_tokens=int(config["max_new_tokens"]),
                            temperature=float(config["temperature"]),
                            top_p=float(config["top_p"]),
                            seed=int(config["seed"]),
                            stop=[self.tokenizer.eos_token] if self.tokenizer.eos_token else [],
                        )
                        if self._expired():
                            raise SQLDecodeBudgetExceeded("SQL decoding exceeded its CPU budget")
                    except RuntimeError as exc:
                        if self._expired():
                            self.model.reset()
                            request_timing.count("proposer_deadline_rejections")
                            raise SQLDecodeBudgetExceeded(
                                "SQL decoding exceeded its CPU budget") from exc
                        raise
                    finally:
                        self._deadline = None
                choice = result["choices"][0]
                if choice.get("finish_reason") != "stop":
                    request_timing.count("proposer_incomplete_rejections", 1)
                    return []
                text = str(choice.get("text", ""))
                lines = (normalize_decoded_sql(text),)
                self._decoded[prompt] = lines
                while len(self._decoded) > self._DECODE_CACHE_CAP:
                    self._decoded.popitem(last=False)
        finally:
            self._model_lock.release()

        proposals = []
        seen = set()
        for position, line in enumerate(lines):
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
                query,
                rendered,
                proposal_score(floor, position),
                (f"proposer:variant{position}",),
            ))
        request_timing.count("proposals", len(proposals))
        return proposals

    def likelihoods(self, tables, question: str, sqls: list[str]) -> list[tuple[float, int]]:
        # The measured >80% policy used these exact neutral sentinels with the shared arbiter.
        return [(0.0, 1) for _ in sqls]
