"""test_encode_cache.py — the encoder text cache must dedupe without changing any vector.

Registered in tests/run_all.py. Hermetic: a counting fake stands in for the forward pass
(`_encode_batch`), so this runs without weights and pins the contract measured to matter — 49-70
texts per request with only 19 unique meant column names paid the forward pass 5-8x per turn and
again every follow-up. A tiny random Qwen2 checks the packed forward pass itself (engine/encoder.py).
"""
from __future__ import annotations

import sys

import numpy as np

from engine.tables import TableQuery


class _FakeEncoder(TableQuery):
    """TableQuery with the forward pass replaced by a deterministic counting fake."""

    def __init__(self):                                    # no super().__init__: no weights, no sqlite
        self.qwen = object()                               # non-None: passes the loaded-encoder guard
        self.hdim = 4
        self.batches = []                                  # every uncached batch that reached the "model"

    def _encode_batch(self, texts):
        self.batches.append(list(texts))
        out = np.zeros((len(texts), self.hdim), np.float32)
        for i, t in enumerate(texts):
            out[i] = (hash(t) % 997) / 997.0               # deterministic per text, distinct across texts
        return out


def test_repeated_texts_hit_the_forward_pass_once():
    q = _FakeEncoder()
    a = q._encode(["revenue", "city", "revenue", "total revenue in France"])
    assert sum(len(b) for b in q.batches) == 3, "within one call, a repeated text encodes once"
    b = q._encode(["revenue", "city", "units"])
    assert sum(len(b) for b in q.batches) == 4, "a second call re-encodes only the new text"
    # Vector equality across the cache boundary: cached rows are the SAME values.
    assert np.array_equal(a[0], a[2]), "same text, same call -> identical vector"
    assert np.array_equal(a[0], b[0]), "same text, later call -> identical vector"


def test_output_order_and_shape_are_preserved():
    q = _FakeEncoder()
    texts = ["b", "a", "b", "c", "a"]
    out = q._encode(texts)
    assert out.shape == (5, q.hdim)
    direct = q._encode_batch(texts)                        # the uncached reference, same fake model
    assert np.array_equal(out, direct), "caching must not reorder or substitute any row"


def test_cache_rows_are_not_aliased_to_the_returned_array():
    q = _FakeEncoder()
    first = q._encode(["city"])
    first[0, :] = 999.0                                    # caller mutates its result in place
    again = q._encode(["city"])
    assert again[0, 0] != 999.0, "a caller mutating its output must not poison the cache"


def test_cap_is_enforced_lru():
    q = _FakeEncoder()
    q._ENCODE_CACHE_CAP = 3
    q._encode(["a", "b", "c"])
    q._encode(["a"])                                       # refresh 'a' -> 'b' is now oldest
    q._encode(["d"])                                       # evicts 'b'
    assert len(q._encode_cache) == 3
    assert "b" not in q._encode_cache and "a" in q._encode_cache, "eviction must be LRU, not FIFO"


def test_unloaded_encoder_still_raises():
    q = _FakeEncoder()
    q.qwen = None
    try:
        q._encode(["x"])
        raise AssertionError("an unloaded encoder must raise, cache or no cache")
    except RuntimeError:
        pass


class _WordTokenizer:
    """Each word one id, the way a real tokenizer returns unpadded ids for a list of texts."""

    def __call__(self, texts, truncation=True, max_length=None, **_kwargs):
        ids = [[sum(map(ord, word)) % 97 + 1 for word in text.split()][:max_length] for text in texts]
        return {"input_ids": ids}


def test_packed_texts_read_as_if_each_were_alone():
    """engine/encoder.py:pooled_vectors packs a pass's texts into one sequence. Each text's vector must equal the
    one it has in a padded batch, where it is read alone: the same tokens, attention to its own earlier tokens only,
    positions from 0. Several passes (a small token budget), an empty text and a truncated one included."""
    import torch
    from transformers import Qwen2Config, Qwen2Model

    from engine.encoder import pooled_vectors

    torch.manual_seed(0)
    model = Qwen2Model(Qwen2Config(vocab_size=100, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                                   num_attention_heads=4, num_key_value_heads=2, max_position_embeddings=64)).eval()
    tok = _WordTokenizer()
    texts = ["total amount in france", "", "us dollars", "a b c d e f g h i j k l", "orders", "total amount"]
    with torch.no_grad():
        packed = pooled_vectors(model, tok, texts, 8, torch.device("cpu"), budget=6)
        for text, vector in zip(texts, packed):
            ids = tok([text], max_length=8)["input_ids"][0]
            if not ids:
                assert torch.count_nonzero(vector) == 0, "a text without tokens reads as zeros"
                continue
            alone = model(input_ids=torch.tensor([ids])).last_hidden_state[0].mean(0)
            assert torch.allclose(vector, alone, atol=1e-5), (text, (vector - alone).abs().max())
        padded = tok(["us dollars", "a b c d e f g h i j k l"], max_length=8)["input_ids"]
        width = max(map(len, padded))
        rows = torch.tensor([ids + [0] * (width - len(ids)) for ids in padded])
        mask = torch.tensor([[1] * len(ids) + [0] * (width - len(ids)) for ids in padded])
        hidden = model(input_ids=rows, attention_mask=mask).last_hidden_state
        batch = (hidden * mask.unsqueeze(-1)).sum(1) / mask.sum(1, keepdim=True)
        assert torch.allclose(packed[2], batch[0], atol=1e-5) and torch.allclose(packed[3], batch[1], atol=1e-5)


TESTS = [
    test_repeated_texts_hit_the_forward_pass_once,
    test_output_order_and_shape_are_preserved,
    test_cache_rows_are_not_aliased_to_the_returned_array,
    test_cap_is_enforced_lru,
    test_unloaded_encoder_still_raises,
    test_packed_texts_read_as_if_each_were_alone,
]
# CI's hermetic job installs no model stack; the engine image does.
NEEDS_TORCH = {test_packed_texts_read_as_if_each_were_alone}


def main():
    import importlib.util

    failures = skipped = 0
    for t in TESTS:
        if t in NEEDS_TORCH and importlib.util.find_spec("torch") is None:
            skipped += 1
            print(f"  SKIP {t.__name__}: needs torch (the engine image installs it)")
            continue
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"{len(TESTS) - failures - skipped}/{len(TESTS)} passed, {skipped} skipped")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
