"""The LoRA-Qwen encoder of the trained metric space, as a STANDALONE import-light module.

Used by BOTH training and serving (engine.router). It deliberately imports only torch + transformers + peft
(no training-pipeline deps), so the router — and thus the serving image that runs the trained model for
column routing — bundles cleanly without dragging the offline training stack.
"""
from __future__ import annotations
from pathlib import Path

import torch
import torch.nn as nn

from engine.config import BASE_MODEL_ID as MODEL_ID


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
        self.tok = AutoTokenizer.from_pretrained(MODEL_ID)
        if self.tok.pad_token is None:
            self.tok.pad_token = self.tok.eos_token
        m = AutoModel.from_pretrained(MODEL_ID, low_cpu_mem_usage=True).float()
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
        texts = list(texts)
        ctx = torch.enable_grad() if grad else torch.no_grad()
        outs = []
        for i in range(0, len(texts), bs):
            enc = self.tok(texts[i:i + bs], return_tensors="pt", padding=True, truncation=True,
                           max_length=max_len).to(self.dev)
            with ctx, torch.autocast(self.dev.type, dtype=torch.bfloat16, enabled=(self.dev.type == "cuda")):
                h = self.qwen(**enc).last_hidden_state
            m = enc["attention_mask"].unsqueeze(-1).float()
            outs.append((h.float() * m).sum(1) / m.sum(1).clamp(min=1.0))
        return torch.cat(outs, 0)
