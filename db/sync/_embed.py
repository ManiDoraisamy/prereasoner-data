"""The retrieval embedder used to build/extend knowledgebase."words" (standalone copy of the
engine's embed16 module, so db/ needs no engine import).

bge-small-en-v1.5 sentence embedding = the [CLS] token of last_hidden_state,
L2-normalized (per the model card), so cosine similarity == dot product. 384-dim,
CPU-fine (33M params). knowledgebase."words".embedding is declared vector(384) to match.

normalize_surface() is the deterministic exact-match key stored in words.norm:
'the U.S.A.' -> 'usa', 'the United States' -> 'unitedstates', 'New York' -> 'newyork'.
"""
from __future__ import annotations
import re

import numpy as np

_MODEL_ID = "BAAI/bge-small-en-v1.5"


def normalize_surface(s):
    """lowercase, drop a leading 'the', squish to alphanumerics."""
    s = (s or "").strip().lower()
    if s.startswith("the "):
        s = s[4:]
    return re.sub(r"[^a-z0-9]+", "", s)


class Embedder:
    """Lazy singleton around bge-small. `.encode(texts) -> (n, 384) float32`, L2-normalized rows."""
    _inst = None

    def __init__(self):
        import torch
        from transformers import AutoTokenizer, AutoModel
        self._torch = torch
        self.tok = AutoTokenizer.from_pretrained(_MODEL_ID)
        self.mdl = AutoModel.from_pretrained(_MODEL_ID, low_cpu_mem_usage=True).eval()
        self.dim = self.mdl.config.hidden_size          # 384

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def encode(self, texts, batch=64):
        torch = self._torch
        if isinstance(texts, str):
            texts = [texts]
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        out = []
        with torch.no_grad():
            for i in range(0, len(texts), batch):
                chunk = [("" if t is None else str(t)) for t in texts[i:i + batch]]
                enc = self.tok(chunk, padding=True, truncation=True, max_length=32, return_tensors="pt")
                cls = self.mdl(**enc).last_hidden_state[:, 0]          # [CLS]
                cls = torch.nn.functional.normalize(cls, p=2, dim=1)
                out.append(cls.cpu().numpy().astype(np.float32))
        return np.vstack(out)


def pgvector_literal(vec):
    """format a vector as a '[v1,v2,...]' text literal for ::vector casts."""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
