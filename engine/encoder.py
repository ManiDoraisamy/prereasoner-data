"""The LoRA-Qwen encoder of the trained metric space, as a STANDALONE import-light module.

Used by BOTH training and serving (engine.router). It deliberately imports only torch + transformers + peft
(no training-pipeline deps), so the router — and thus the serving image that runs the trained model for
column routing — bundles cleanly without dragging the offline training stack.
"""
from __future__ import annotations
import threading
from pathlib import Path

import torch
import torch.nn as nn

from engine.config import BASE_MODEL_ID as MODEL_ID, BASE_MODEL_REVISION as MODEL_REVISION

# One Qwen tokenizer serves every encoder in the process: TableQuery reads cells at 48 tokens and the
# schema interpreter reads table summaries at 128. A fast tokenizer keeps its truncation length as state
# and rewrites it when a call asks for another, so with questions running in parallel one request could
# tokenize at the other's length, and cache those vectors, or fail "Already borrowed". Tokenizing takes
# about a millisecond; the forward pass runs outside the lock.
_TOKENIZER_LOCK = threading.Lock()

# Texts are read PACKED: one forward pass takes up to this many tokens of several texts, laid end to end, each
# attending causally to itself alone and numbered from position 0, so no padding is computed and each text's
# vector is the one it has read alone. In padded batches every text was read at the length of the longest one
# beside it: the 109 spans of a long question (engine/primitive_head.py) took 12.2 s at 3 threads, 1.2 s packed,
# with 1 - cosine <= 1.2e-7 between the two (2026-10-28). A pass also streams the weights once (about 100 ms on
# Cloud Run's CPU), so fewer, fuller passes cost less.
PACKED_TOKENS = 1024


def token_ids(tok, texts, max_length):
    """Each text's token ids, truncated to ``max_length``, one tokenizer call at a time."""
    with _TOKENIZER_LOCK:
        return tok(list(texts), truncation=True, max_length=max_length)["input_ids"]


def pooled_vectors(model, tok, texts, max_length, device, budget=PACKED_TOKENS):
    """The mean of the last hidden states of each text's own tokens, as if each were read alone: a pass packs texts
    up to ``budget`` tokens into one sequence under a block-diagonal causal mask, with positions restarting at each
    text. A text without tokens reads as zeros, as an all-padding row did. Returns a (len(texts), hidden) float32
    tensor on ``device``. The caller chooses gradient and autocast."""
    import torch

    ids = token_ids(tok, texts, max_length)
    pooled = [None] * len(ids)
    start = 0
    while start < len(ids):
        end, total = start, 0
        while end < len(ids) and (end == start or total + len(ids[end]) <= budget):
            total += len(ids[end])
            end += 1
        members = [index for index in range(start, end) if ids[index]]
        if members:
            flat = torch.tensor([[token for index in members for token in ids[index]]], dtype=torch.long, device=device)
            positions = torch.tensor([[place for index in members for place in range(len(ids[index]))]],
                                     dtype=torch.long, device=device)
            segment = torch.tensor([slot for slot, index in enumerate(members) for _ in ids[index]], device=device)
            mask = (segment[:, None] == segment[None, :]) & torch.ones(
                len(segment), len(segment), dtype=torch.bool, device=device).tril()
            hidden = model(input_ids=flat, attention_mask=mask[None, None], position_ids=positions).last_hidden_state[0]
            sums = torch.zeros(len(members), hidden.shape[-1], device=device).index_add_(0, segment, hidden.float())
            counts = torch.bincount(segment, minlength=len(members)).unsqueeze(-1).float()
            for slot, index in enumerate(members):
                pooled[index] = sums[slot] / counts[slot]
        start = end
    width = next((vector.shape[-1] for vector in pooled if vector is not None), None)
    if width is None:
        width = model.config.hidden_size
    zeros = torch.zeros(width, device=device)
    return torch.stack([zeros if vector is None else vector for vector in pooled]) if pooled else \
        torch.zeros(0, width, device=device)


class LiveQwen(nn.Module):
    """LoRA-wrapped Qwen used AS an encoder (tokenize -> last_hidden_state -> mean-pool -> per-unit vector, grad
    through the adapters). warm_lora warm-starts the adapter from a saved LoRA (the trained metric space).

    serving=True is the INFERENCE path (engine.router / engine.dimension): the adapter is loaded non-trainable and
    the module is put in .eval() so LoRA DROPOUT is OFF -> deterministic, reproducible scores that match the
    calibrated thresholds. Training (serving=False) keeps dropout + gradient checkpointing + input-require-grads."""
    def __init__(self, dev, lora_r=16, warm_lora=None, serving=True, shared_qwen=None, shared_tok=None):
        super().__init__()
        if shared_qwen is not None:                                # REUSE an already-loaded Qwen (one model in
            self.tok = shared_tok; self.qwen = shared_qwen; self.dev = dev   # memory — the world encoder shares it)
            self.hdim = self.qwen.config.hidden_size if hasattr(self.qwen, "config") else 896
            return
        from transformers import AutoModel, AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
        if self.tok.pad_token is None:
            self.tok.pad_token = self.tok.eos_token
        m = AutoModel.from_pretrained(
            MODEL_ID, revision=MODEL_REVISION, low_cpu_mem_usage=True
        ).float()
        if warm_lora and Path(warm_lora).exists():
            from peft import PeftModel
            m = PeftModel.from_pretrained(m, str(warm_lora), is_trainable=not serving)   # serving => frozen adapter
            print(f"warm-started Qwen LoRA from {warm_lora}", flush=True)
        else:
            from peft import LoraConfig, get_peft_model
            m = get_peft_model(m, LoraConfig(r=lora_r, lora_alpha=2 * lora_r, lora_dropout=0.05, bias="none",
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]))
        self.qwen = m.to(dev); self.dev = dev
        if serving:
            self.qwen.eval()                                       # DROPOUT OFF -> deterministic serving scores
        else:
            self.qwen.gradient_checkpointing_enable()              # training-only setup
            if hasattr(self.qwen, "enable_input_require_grads"):
                self.qwen.enable_input_require_grads()
        self.hdim = self.qwen.config.hidden_size if hasattr(self.qwen, "config") else 896

    def encode(self, texts, max_len=24, grad=True, bs=96):
        """Each text's pooled vector (``pooled_vectors``), ``bs`` texts' worth of tokens a pass."""
        ctx = torch.enable_grad() if grad else torch.no_grad()
        with ctx, torch.autocast(self.dev.type, dtype=torch.bfloat16, enabled=(self.dev.type == "cuda")):
            return pooled_vectors(self.qwen, self.tok, list(texts), max_len, self.dev, budget=bs * max_len)
