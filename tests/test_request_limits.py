"""Hermetic security and resource-bound tests."""
from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

from engine import config
from engine.request_limits import (
    JSONBodyError, SlidingWindowLimiter, allowed_origin, parse_content_length, read_json_object,
)
from orchestrator.validation import validate_chat_request


def test_sliding_window_limiter_is_bounded_and_expires():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10)
    assert limiter.allow("u", now=100) == (True, 0)
    assert limiter.allow("u", now=101) == (True, 0)
    allowed, retry = limiter.allow("u", now=102)
    assert not allowed and retry > 0
    assert limiter.allow("u", now=111) == (True, 0)
    bounded = SlidingWindowLimiter(limit=1, window_seconds=10, max_keys=1)
    assert bounded.allow("first", now=100)[0]
    assert bounded.allow("second", now=100)[0]


def test_cors_requires_exact_configured_origin():
    assert allowed_origin("https://app.example", "https://app.example") == "https://app.example"
    assert allowed_origin("https://evil.example", "https://app.example") is None
    assert allowed_origin("https://app.example.evil", "https://app.example") is None


def test_auth_test_sub_is_ignored_outside_explicit_nonproduction():
    with patch.dict("os.environ", {"AUTH_TEST_SUB": "local", "APP_ENV": "production"}, clear=False):
        with patch.object(config, "APP_ENV", "production"):
            assert config.auth_test_sub() is None
    with patch.dict("os.environ", {"AUTH_TEST_SUB": "local", "APP_ENV": "test"}, clear=False):
        with patch.object(config, "APP_ENV", "test"):
            assert config.auth_test_sub() == "local"


def test_chat_validation_normalizes_and_bounds_inputs():
    out = validate_chat_request({
        "message": "  total amount  ",
        "tables": [{"name": " orders ", "data": "id,amount\n1,2\n"}],
        "history": [{"role": "user", "content": "hello"}],
        "turnId": " t1 ",
    })
    assert out[:3] == ("total amount", [{"name": "orders", "data": "id,amount\n1,2\n"}],
                       [{"role": "user", "content": "hello"}])
    for bad in ({"message": "x" * 20_001}, {"message": "x", "tables": [{}] * 9}):
        try:
            validate_chat_request(bad)
            raise AssertionError
        except ValueError:
            pass


def test_json_body_guard_rejects_bad_lengths_payloads_and_shapes():
    assert parse_content_length(None, 10) == 0
    assert parse_content_length("4", 10) == 4
    for value, status in (("-1", 400), ("abc", 400), ("11", 413)):
        try:
            parse_content_length(value, 10)
            raise AssertionError
        except JSONBodyError as exc:
            assert exc.status_code == status
    assert read_json_object(BytesIO(b'{"ok":true}'), "11", 20) == {"ok": True}
    for raw in (b"[1]", b"{bad"):
        try:
            read_json_object(BytesIO(raw), str(len(raw)), 20)
            raise AssertionError
        except JSONBodyError as exc:
            assert exc.status_code == 400


TESTS = [
    test_sliding_window_limiter_is_bounded_and_expires,
    test_cors_requires_exact_configured_origin,
    test_auth_test_sub_is_ignored_outside_explicit_nonproduction,
    test_chat_validation_normalizes_and_bounds_inputs,
    test_json_body_guard_rejects_bad_lengths_payloads_and_shapes,
]


def main():
    failed = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nrequest limits: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
