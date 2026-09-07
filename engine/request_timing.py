"""request_timing.py — ONE structured timing line per served request.

Answers "where did the wall clock go" without a profiler attached, through the production entry
point. A request opens a collector (`begin`), code marks phases with `span(...)`/`count(...)`, and
the server prints ONE `[timing]` line in a finally (so a failed request is measured too).

NESTED SPANS ARE NOT DOUBLE COUNTED. Each span records both:
  <name>_ms       total wall time inside the span, nested work included
  <name>_self_ms  that time MINUS the time attributed to spans nested within it
Only the `_self_ms` values partition the request; summing `_ms` across nested spans over-counts.
Emitted only when they differ, so a leaf span prints one number.

PRIVACY: names, counts and durations only. Never a prompt, a cell value, a question, a token, or a
row — this line goes to ordinary container logs, which are not a place for user data.

Thread-safe via contextvars: each serving thread gets its own collector, so the line stays correct
if the engine's request lock is ever narrowed.
"""
from __future__ import annotations

import contextlib
import contextvars
import time

_CTX: contextvars.ContextVar = contextvars.ContextVar("request_timing", default=None)


class _Timing:
    def __init__(self, request_id):
        self.request_id = request_id
        self.started = time.perf_counter()
        self.spans: dict[str, list] = {}      # name -> [count, total_s, self_s]
        self.counters: dict[str, int] = {}
        self._child_stack: list[float] = []   # time consumed by spans nested in the open span

    def record(self, name, elapsed, child_elapsed):
        slot = self.spans.setdefault(name, [0, 0.0, 0.0])
        slot[0] += 1
        slot[1] += elapsed
        slot[2] += elapsed - child_elapsed
        # ONE source for "<name>_n" so a span count and an explicit `count("<name>_n", ...)`
        # accumulate into the same key instead of printing it twice.
        self.counters[f"{name}_n"] = self.counters.get(f"{name}_n", 0) + 1


def begin(request_id):
    """Open a collector for this request. Returns a token to pass back to `end`."""
    return _CTX.set(_Timing(request_id))


def end(token):
    """Close the collector. Always pair with `begin` in a finally."""
    try:
        _CTX.reset(token)
    except ValueError:                       # noqa: BLE001 — reset from a different context; nothing to unwind
        pass


@contextlib.contextmanager
def span(name):
    """Time a phase. A no-op (zero overhead beyond the ContextVar read) outside a request."""
    timing = _CTX.get()
    if timing is None:
        yield
        return
    timing._child_stack.append(0.0)
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        child_elapsed = timing._child_stack.pop()
        timing.record(name, elapsed, child_elapsed)
        if timing._child_stack:              # bill the FULL span to the enclosing span's children
            timing._child_stack[-1] += elapsed


def mark(name, elapsed):
    """Record a phase whose duration was measured by the caller — for work spanning a construct a
    `span` cannot wrap, such as an `async with` entry. Counts as a leaf: no child time is deducted."""
    timing = _CTX.get()
    if timing is not None:
        timing.record(name, float(elapsed), 0.0)


def count(name, n=1):
    """Add to a request counter (rows loaded, statements issued). No-op outside a request."""
    timing = _CTX.get()
    if timing is not None:
        timing.counters[name] = timing.counters.get(name, 0) + n


def request_id():
    """The current request id, or None outside a request."""
    timing = _CTX.get()
    return timing.request_id if timing is not None else None


def set_request_id(rid):
    """Adopt a correlation id that only became known after the body was parsed (the jobId), so the
    engine line joins to the chat turn even when the caller sent no `X-Request-Id` header."""
    timing = _CTX.get()
    if timing is not None and rid:
        timing.request_id = str(rid)


def emit(tag, **extra):
    """Print the request's ONE timing line. Best-effort: measurement never breaks a response."""
    timing = _CTX.get()
    if timing is None:
        return
    try:
        total_ms = (time.perf_counter() - timing.started) * 1000.0
        # Span self-times are disjoint by construction, so what they do NOT cover is work in no span
        # at all. Publishing the remainder keeps the line an honest partition: without it, reading
        # the largest span as "the cost" silently ignores everything uninstrumented.
        attributed_ms = sum(slot[2] for slot in timing.spans.values()) * 1000.0
        parts = [f"rid={timing.request_id}", f"total_ms={total_ms:.0f}",
                 f"unattributed_ms={max(0.0, total_ms - attributed_ms):.0f}"]
        for key, value in extra.items():
            if value is not None:
                parts.append(f"{key}={value}")
        for name in sorted(timing.spans):
            _, total_s, self_s = timing.spans[name]
            parts.append(f"{name}_ms={total_s * 1000:.0f}")
            if abs(total_s - self_s) > 1e-4:           # nested: publish the non-overlapping share too
                parts.append(f"{name}_self_ms={self_s * 1000:.0f}")
        for name in sorted(timing.counters):
            if timing.counters[name] != 1:             # a once-only phase needs no count
                parts.append(f"{name}={timing.counters[name]}")
        print(f"[timing] {tag} " + " ".join(parts), flush=True)
    except Exception as e:                             # noqa: BLE001 — a timing bug must not fail the request
        print(f"[timing] emit_failed error={type(e).__name__}", flush=True)
