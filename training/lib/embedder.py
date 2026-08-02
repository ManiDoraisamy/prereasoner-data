"""
The retrieval embedder for entity resolution (separate from the frozen Qwen interpretable encoder).

Why a different model: the frozen Qwen-0.5B unit encoder mean-pools a generative LM's hidden
states — an anisotropic space that is NOT a metric space for entity-linking (measured: "USA" is nearer to
"Australia" than to "United States"). bge-small-en-v1.5 is contrastively trained for retrieval, so cosine
distance is meaningful: "US"/"USA"/"UK"/"Deutschland"/"Holland"/"Bombay" all land on the right canonical entity,
and prepositions ("in") / non-entities ("Indiana") stay below threshold. This embedder backs:
  - the `knowledgebase.words` vector index (one row per canonical entity)
  - CSV cell-value resolution and parsed prompt-word resolution in `engine.entities`

bge-small sentence embedding = the [CLS] token of last_hidden_state, L2-normalized (per the model card), so cosine
similarity == dot product. 384-dim. CPU-fine (33M params).
"""
from __future__ import annotations
import re
import numpy as np

_MODEL_ID = "BAAI/bge-small-en-v1.5"


def normalize_surface(s):
    """Match key for EXACT alias lookup: lowercase, drop a leading 'the', squish to alphanumerics.
    'the U.S.A.'->'usa', 'the United States'->'unitedstates', 'U. S.'->'us', 'New York'->'newyork'. Only the
    SURFACE form is normalized for matching — the stored canonical PK is untouched (this is NOT lowercasing a
    join key into a non-PK; it's how a query form finds its surface row, which then yields the real PK)."""
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
    """psycopg2 has no pgvector adapter here -> format as a '[v1,v2,...]' text literal for ::vector casts."""
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
