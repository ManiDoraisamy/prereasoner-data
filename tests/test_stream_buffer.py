"""test_stream_buffer.py — the coalescing RTDB stream writer must bound writes and end authoritatively.

Registered in tests/run_all.py. Hermetic: a recording emit stands in for RTDB. Pins the design
constraints that made per-token writes unshippable — measured 33-54ms per synchronous RTDB write,
so a 100-token reply written per-token would spend 3-5s writing alone.
"""
from __future__ import annotations

import sys
import time

from engine.trace import StreamBuffer


class _Recorder:
    def __init__(self, fail=False):
        self.writes = []
        self.fail = fail

    def __call__(self, node, value):
        if self.fail:
            raise RuntimeError("rtdb down")
        self.writes.append((node, value))


def test_many_updates_coalesce_into_few_writes():
    """Writes are bounded by ELAPSED TIME / interval, not by update count. The bound is computed
    from the measured wall clock, so a slow shared CI runner (where 50 x 5ms sleeps can stretch to
    seconds) moves the budget with it instead of failing on machine speed."""
    rec = _Recorder()
    interval = 0.15
    buf = StreamBuffer(rec, "reply", interval=interval)
    started = time.perf_counter()
    text = ""
    for word in ["Your", " total", " comes", " to", " 740", ".", " Let", " me", " know", "!"] * 5:
        text += word
        buf.update(text)
        time.sleep(0.005)
    elapsed = time.perf_counter() - started
    buf.close(text)
    budget = int(elapsed / interval) + 2                   # possible interim flushes + the final write
    assert len(rec.writes) <= budget, (
        f"50 updates over {elapsed:.2f}s must coalesce to <= {budget} writes, got {len(rec.writes)}")
    assert len(rec.writes) >= 2, "at least one interim flush plus the final write should happen"


def test_every_write_is_full_state_and_monotonic():
    """Full-state writes are the reconnect story: no sequence numbers, the node IS the state."""
    rec = _Recorder()
    buf = StreamBuffer(rec, "reply", interval=0.02)
    text = ""
    for ch in "streaming prose one char at a time":
        text += ch
        buf.update(text)
        time.sleep(0.004)
    buf.close(text)
    values = [v for _, v in rec.writes]
    full = "streaming prose one char at a time"
    assert values[-1] == full, "the final write must be the authoritative full text"
    for earlier, later in zip(values, values[1:]):
        assert full.startswith(earlier) and full.startswith(later), "every write is a full prefix"
        assert len(later) >= len(earlier), f"writes must be monotonic: {earlier!r} -> {later!r}"


def test_close_final_wins_over_pending_update():
    rec = _Recorder()
    buf = StreamBuffer(rec, "reply", interval=5)           # interval so long no interim flush can land
    buf.update("partial that never flushed")
    buf.close("the authoritative final")
    assert rec.writes[-1] == ("reply", "the authoritative final")
    assert all(v != "partial that never flushed" for _, v in rec.writes), (
        "a pending interim value must be superseded by close(final), not written after it")


def test_close_without_final_writes_nothing_more():
    rec = _Recorder()
    buf = StreamBuffer(rec, "reply", interval=5)
    buf.update("pending")
    buf.close()                                            # e.g. the turn raised — the caller's error path owns the node
    assert rec.writes == [], f"close() without final must not flush the pending value: {rec.writes}"


def test_emit_failure_never_raises_out():
    buf = StreamBuffer(_Recorder(fail=True), "reply", interval=0.01)
    buf.update("x")
    time.sleep(0.05)
    buf.close("y")                                         # both paths swallow the failure


def test_updates_do_not_block_the_caller():
    slow_calls = []
    def slow_emit(node, value):
        slow_calls.append(value)
        time.sleep(0.05)                                   # a slow network write
    buf = StreamBuffer(slow_emit, "reply", interval=0.01)
    t0 = time.perf_counter()
    for i in range(100):
        buf.update(f"text {i}")
    elapsed = time.perf_counter() - t0
    buf.close("done")
    # 100 in-memory updates vs 100 x 50ms network writes (5s). The generous bound stays orders of
    # magnitude below the blocking cost while tolerating a shared CI runner's scheduling stalls.
    assert elapsed < 1.0, f"100 updates must not wait on the network: {elapsed:.3f}s"


TESTS = [
    test_many_updates_coalesce_into_few_writes,
    test_every_write_is_full_state_and_monotonic,
    test_close_final_wins_over_pending_update,
    test_close_without_final_writes_nothing_more,
    test_emit_failure_never_raises_out,
    test_updates_do_not_block_the_caller,
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
