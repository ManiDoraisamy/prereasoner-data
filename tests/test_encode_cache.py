"""test_encode_cache.py — the encoder text cache must dedupe without changing any vector.

Registered in tests/run_all.py. Hermetic: a counting fake stands in for the forward pass
(`_encode_batch`), so this runs without weights and pins the contract measured to matter — 49-70
texts per request with only 19 unique meant column names paid the forward pass 5-8x per turn and
again every follow-up.
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


TESTS = [
    test_repeated_texts_hit_the_forward_pass_once,
    test_output_order_and_shape_are_preserved,
    test_cache_rows_are_not_aliased_to_the_returned_array,
    test_cap_is_enforced_lru,
    test_unloaded_encoder_still_raises,
]


def main():
    failures = 0
    for t in TESTS:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
