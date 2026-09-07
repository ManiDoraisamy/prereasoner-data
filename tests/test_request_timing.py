"""test_request_timing.py — the phase-timing line must attribute correctly and change nothing.

Registered in tests/run_all.py. Covers what compileall and the composition suite cannot: that nested
spans are not double counted, that the line is emitted for a FAILED request as well as a successful
one, that instrumented calls return identical results, and that no user data reaches the log line.
"""
from __future__ import annotations

import io
import sys
import time
from contextlib import redirect_stdout

from engine import request_timing


def _capture(fn):
    """Run fn with stdout captured; return (result, printed_text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn()
    return result, buf.getvalue()


def _fields(line):
    """Parse one `[timing] tag k=v k=v` line into a dict."""
    parts = line.strip().split()
    return dict(p.split("=", 1) for p in parts[2:] if "=" in p)


def test_nested_spans_are_not_double_counted():
    """An outer span's _self_ms must exclude its children, so the _self_ms values partition the time."""
    def run():
        token = request_timing.begin("rid1")
        try:
            with request_timing.span("outer"):
                time.sleep(0.05)
                with request_timing.span("inner"):
                    time.sleep(0.10)
            request_timing.emit("t")
        finally:
            request_timing.end(token)

    _, out = _capture(run)
    f = _fields(out)
    outer_total, outer_self = float(f["outer_ms"]), float(f["outer_self_ms"])
    inner_total = float(f["inner_ms"])
    assert inner_total >= 90, f"inner span too short: {inner_total}"
    assert outer_total >= 140, f"outer total should include the child: {outer_total}"
    # The whole point: outer_self excludes inner, so self values do not overlap.
    assert 30 <= outer_self <= 90, f"outer_self_ms should be ~50ms, got {outer_self}"
    assert abs((outer_self + inner_total) - outer_total) < 15, "self + child should reconstruct total"
    # A leaf span publishes ONE number (no redundant _self_ms).
    assert "inner_self_ms" not in f, "a leaf span must not print a _self_ms twin"


def test_repeated_span_reports_count_and_sum():
    """A repeated span self-counts — no explicit counter needed, and none may be double-added."""
    def run():
        token = request_timing.begin("rid2")
        try:
            for _ in range(3):
                with request_timing.span("sql"):
                    time.sleep(0.01)
            request_timing.emit("t")
        finally:
            request_timing.end(token)

    _, out = _capture(run)
    f = _fields(out)
    assert f["sql_n"] == "3", f"three spans should report three statements: {f}"
    assert int(f["sql_ms"]) >= 25, f"three 10ms spans should sum: {f}"


def test_line_is_emitted_for_a_failed_request():
    """A request that raises is exactly the one whose phase split matters."""
    def run():
        token = request_timing.begin("rid3")
        try:
            try:
                with request_timing.span("serve"):
                    raise ValueError("boom")
            except ValueError:
                pass
            request_timing.emit("reason", status=500)
        finally:
            request_timing.end(token)

    _, out = _capture(run)
    f = _fields(out)
    assert f["status"] == "500", f"failed request must report its status: {f}"
    assert "serve_ms" in f, "the span that raised must still be attributed"


def test_instrumentation_is_inert_outside_a_request():
    """No collector open => spans/counters are no-ops and emit prints nothing."""
    def run():
        with request_timing.span("orphan"):
            pass
        request_timing.count("orphan_n")
        request_timing.mark("orphan_mark", 1.0)
        request_timing.emit("t")
        return "value"

    result, out = _capture(run)
    assert result == "value", "wrapped code must still run and return"
    assert out == "", f"nothing should be logged outside a request: {out!r}"


def test_span_returns_the_wrapped_value_and_reraises():
    """Timing must not change results — the reason it is safe to leave on in production."""
    token = request_timing.begin("rid4")
    try:
        with request_timing.span("s"):
            value = sum(range(10))
        assert value == 45
        raised = False
        try:
            with request_timing.span("s2"):
                raise KeyError("original")
        except KeyError as e:
            raised = "original" in str(e)
        assert raised, "the original exception must propagate unchanged"
    finally:
        request_timing.end(token)


def test_no_user_data_in_the_line():
    """The line carries names, counts and durations only — it goes to ordinary container logs."""
    secret_question = "what is the total revenue for Acme Corp in France"
    def run():
        token = request_timing.begin("rid5")
        try:
            request_timing.count("upload_rows", 42)
            with request_timing.span("serve"):
                pass
            request_timing.emit("reason", status=200)
        finally:
            request_timing.end(token)

    _, out = _capture(run)
    assert "Acme" not in out and secret_question not in out
    assert "upload_rows=42" in out, f"counts are fine, content is not: {out}"


def test_unattributed_time_is_published():
    """Span self-times cover only instrumented work; the remainder must be visible, or the biggest
    span reads as 'the cost' while uninstrumented time hides."""
    def run():
        token = request_timing.begin("rid6")
        try:
            with request_timing.span("measured"):
                time.sleep(0.05)
            time.sleep(0.10)                              # deliberately outside every span
            request_timing.emit("t")
        finally:
            request_timing.end(token)

    _, out = _capture(run)
    f = _fields(out)
    unattributed = float(f["unattributed_ms"])
    assert 70 <= unattributed <= 140, f"the ~100ms outside any span must show up: {unattributed}"
    total, measured = float(f["total_ms"]), float(f["measured_ms"])
    assert abs((measured + unattributed) - total) < 15, "self + unattributed should reconstruct total"


def test_span_count_and_explicit_count_share_one_key():
    """executemany adds statements beyond its single call; the count must ACCUMULATE, not print twice."""
    def run():
        token = request_timing.begin("rid7")
        try:
            with request_timing.span("sql"):
                pass
            request_timing.count("sql_n", 4)              # e.g. executemany sent 5 statements in 1 call
            request_timing.emit("t")
        finally:
            request_timing.end(token)

    _, out = _capture(run)
    assert out.count("sql_n=") == 1, f"sql_n must appear exactly once: {out}"
    assert _fields(out)["sql_n"] == "5", f"1 span call + 4 extra statements = 5: {out}"


def test_single_call_phase_prints_no_count():
    def run():
        token = request_timing.begin("rid8")
        try:
            with request_timing.span("serve"):
                pass
            request_timing.emit("t")
        finally:
            request_timing.end(token)

    _, out = _capture(run)
    assert "serve_n=" not in out, f"a once-only phase should not carry a count: {out}"


def test_request_id_can_be_adopted_after_parsing():
    def run():
        token = request_timing.begin("generated")
        try:
            request_timing.set_request_id("turn123_0")
            assert request_timing.request_id() == "turn123_0"
            request_timing.emit("reason")
        finally:
            request_timing.end(token)

    _, out = _capture(run)
    assert "rid=turn123_0" in out, f"the adopted id must be the one logged: {out}"


TESTS = [
    test_nested_spans_are_not_double_counted,
    test_repeated_span_reports_count_and_sum,
    test_line_is_emitted_for_a_failed_request,
    test_instrumentation_is_inert_outside_a_request,
    test_span_returns_the_wrapped_value_and_reraises,
    test_no_user_data_in_the_line,
    test_unattributed_time_is_published,
    test_span_count_and_explicit_count_share_one_key,
    test_single_call_phase_prints_no_count,
    test_request_id_can_be_adopted_after_parsing,
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
