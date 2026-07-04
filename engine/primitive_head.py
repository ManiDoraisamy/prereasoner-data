"""PrimitiveReader: the LEARNED primitive-dim readout. Loads the unified encoder + the trained head
(primitives.npz) and reads which analytical primitives a question contains (exclusion / ratio-over-time /
top-N / share / group-by), so the composition engine decomposes by READING the head, not regex. This is the
anchored named-dim readout for the composition vocabulary, realized as a linear head on the same metric
space the rest of the system uses.
"""
from __future__ import annotations

import numpy as np

from engine.config import DATA_DIR

HEAD = DATA_DIR / "primitives.npz"


def ngram_views(text, nmax=3):
    """The whole text + every 1..nmax-word contiguous SPAN. The head transfers to short spans ('excluding returns'
    fires EXCL) but not isolated words, so reading a primitive off the MAX over spans is undiluted on dense
    questions while a multi-word cue ('year over year') stays intact."""
    import re as _re
    words = _re.findall(r"[a-z]+", text.lower())
    out = [text.lower()]
    for n in range(1, nmax + 1):
        out += [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
    return out


class PrimitiveReader:
    """Reads which analytical primitives a question contains, per SPAN: the head is applied to the whole question
    AND to each 1-3 word span, and a primitive fires on the MAX over those views — so a cue span ('excluding
    returns') is read undiluted on a dense question."""

    def __init__(self, encoder=None, head=HEAD):
        d = np.load(head, allow_pickle=True)
        self.W = d["W"].astype(np.float64); self.prims = [str(p) for p in d["prims"]]
        self.thr = (d["thr_tok"] if "thr_tok" in d else d["thr"]).astype(float)
        if encoder is None:
            from engine.encoder_overlay import EncoderQuery
            encoder = EncoderQuery()                       # the unified encoder (same metric space)
        self.enc = encoder

    def scores(self, question):
        views = ngram_views(question)                      # whole question + 1-3 word spans
        V = self.enc._encode(views).astype(np.float64)
        V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
        s = (np.hstack([V, np.ones((len(views), 1))]) @ self.W).max(axis=0)   # max over spans
        return {p: float(s[i]) for i, p in enumerate(self.prims)}

    def present(self, question):
        sc = self.scores(question)
        return {p for i, p in enumerate(self.prims) if sc[p] >= self.thr[i]}
