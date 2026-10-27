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


def test_a_slow_flush_lands_before_the_authoritative_write():
    """Chrome gate, 2026-10-01: a first chat reply was saved as "Your rest". The first flush was slower
    than close()'s 2 s wait, so it landed after the caller's final write and the node kept the prefix.
    close() now returns only after the flush in flight has landed."""
    import threading
    writes, lock = [], threading.Lock()

    def emit(node, value):
        if value == "Your rest":
            time.sleep(2.5)                                # a first RTDB write slower than the old wait
        with lock:
            writes.append(value)

    buf = StreamBuffer(emit, "reply", interval=0.01)
    buf.update("Your rest")
    time.sleep(0.1)                                        # the flusher has taken it and is writing
    buf.close()
    emit("reply", "Your restaurant total comes to 9,600.")  # the orchestrator's authoritative write
    time.sleep(0.1)
    assert writes[-1] == "Your restaurant total comes to 9,600.", writes


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


class _Reference:
    """Stands in for firebase_admin.db.reference: records each multi-path update; ``delay`` slows each one."""

    def __init__(self, delay=0.0):
        self.updates = []
        self.delay = delay

    def __call__(self, base):
        recorder = self

        class _Ref:
            def update(self, values):
                time.sleep(recorder.delay)
                recorder.updates.append((base, values))
        return _Ref()


def test_a_question_s_trace_writes_leave_the_serving_thread_as_few_updates():
    """2026-10-26: 13 synchronous RTDB writes per question, rtdb p50 0.65 s and p90 5.0 s on the serving thread.
    The writer queues them and sends what is queued as one multi-path update per round trip."""
    from engine.trace import _Writer, write_batches

    assert write_batches([("status", "running", False), ("views/0", {"a": 1}, False),
                          ("views/1", {"b": 2}, False), ("status", "done", False)]) == [
        {"status": "done", "views/0": {"a": 1}, "views/1": {"b": 2}}]
    assert write_batches([("views/0", 1, False), ("views", {}, False), ("result", 2, False)]) == [
        {"views/0": 1}, {"views": {}, "result": 2}]                  # nested paths never share an update
    assert write_batches([("", {"created_at": 1, "expires_at": 2}, True), ("analysis", {"x": 1}, True)]) == [
        {"created_at": 1, "expires_at": 2, "analysis/x": 1}]

    reference = _Reference(delay=0.05)
    writer = _Writer("runs/u/j", reference)
    view = {"rows": [[1]]}
    started = time.perf_counter()
    writer("status", "running")
    writer("views/0", view)
    view["rows"].append([2])                                         # the caller goes on mutating its value
    for index in range(1, 12):
        writer(f"views/{index}", {"i": index})
    writer("status", "done")
    queued = time.perf_counter() - started
    assert queued < 0.05, f"queueing waited on the network: {queued:.3f}s"
    assert writer.drain(5.0)
    assert len(reference.updates) <= 2, reference.updates             # one in flight, the rest in one update
    delivered = {}
    for base, values in reference.updates:
        assert base == "runs/u/j"
        delivered.update(values)
    assert delivered["status"] == "done" and delivered["views/11"] == {"i": 11}
    assert delivered["views/0"] == {"rows": {"__pr_wire__": "array/v1", "json": "[[1]]"}}   # the value when emitted


def test_a_response_waits_only_for_its_own_trace_writes():
    import contextvars
    import threading

    from engine import trace

    reference = _Reference(delay=0.2)
    other = []

    def request(name, out):
        writer = trace._Writer(f"runs/u/{name}", reference)
        trace._WRITERS.set([writer])
        writer("status", "done")
        started = time.perf_counter()
        trace.drain_request(timeout=5.0)
        out.append(time.perf_counter() - started)
        assert trace._WRITERS.get() is None

    mine = []
    thread = threading.Thread(target=contextvars.Context().run, args=(request, "b", other))
    thread.start()
    contextvars.Context().run(request, "a", mine)
    thread.join()
    assert mine[0] >= 0.15 and other[0] >= 0.15                       # each waited for its own write
    assert sorted(base for base, _ in reference.updates) == ["runs/u/a", "runs/u/b"]
    trace.drain_request()                                             # nothing queued in this context: no wait


def test_a_writer_nobody_drains_holds_no_thread():
    """The chat orchestrator created writers and never drained them (2026-10-27 review): each turn would have
    left a thread waiting forever. A writer's thread ends once its queue is idle; a later write starts another."""
    from engine import trace

    reference = _Reference()
    saved = trace.WRITER_IDLE_SECONDS
    trace.WRITER_IDLE_SECONDS = 0.05
    try:
        writer = trace._Writer("runs/u/idle", reference)
        writer("status", "running")
        deadline = time.monotonic() + 2.0
        while writer._thread is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert writer._thread is None, "an idle writer kept its thread"
        writer("status", "done")
        assert writer.drain(2.0)
        assert [values for _, values in reference.updates] == [{"status": "running"}, {"status": "done"}]
    finally:
        trace.WRITER_IDLE_SECONDS = saved


def test_the_chat_orchestrator_answers_after_its_trace_writes():
    """The browser reads a chat turn's reply from RTDB and from the HTTP answer; the answer must not overtake
    the reply's final write."""
    import io

    from engine import trace
    from orchestrator.server import H

    reference = _Reference(delay=0.2)
    order = []

    class _Stub:
        wfile = io.BytesIO()

        def send_response(self, code):
            order.append(("response", sorted(path for _, values in reference.updates for path in values)))

        def send_header(self, *args):
            pass

        def end_headers(self):
            pass

        def _cors(self):
            pass

    writer = trace._Writer("runs/u/turn", reference)
    trace._WRITERS.set([writer])
    writer("reply", "Your total comes to 740.")
    writer("status", "done")
    H._send(_Stub(), 200, "{}")
    assert order == [("response", ["reply", "status"])], order         # both delivered before the answer


TESTS = [
    test_a_question_s_trace_writes_leave_the_serving_thread_as_few_updates,
    test_a_response_waits_only_for_its_own_trace_writes,
    test_a_writer_nobody_drains_holds_no_thread,
    test_the_chat_orchestrator_answers_after_its_trace_writes,
    test_many_updates_coalesce_into_few_writes,
    test_every_write_is_full_state_and_monotonic,
    test_close_final_wins_over_pending_update,
    test_close_without_final_writes_nothing_more,
    test_emit_failure_never_raises_out,
    test_a_slow_flush_lands_before_the_authoritative_write,
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
