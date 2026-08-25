"""Small, dependency-free request guards shared by the HTTP entry points.

These guards are deliberately process-local.  Cloud Run can run several instances, so
they are a last-mile protection against one instance being exhausted, not an accounting
or billing control.  Global quotas belong at the edge or in an authenticated gateway.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque


class JSONBodyError(ValueError):
    """A client-correctable request-body failure with its HTTP status."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def parse_content_length(value: str | None, max_bytes: int) -> int:
    """Validate a request Content-Length before any blocking body read."""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    if value in (None, ""):
        return 0
    try:
        length = int(value)
    except (TypeError, ValueError) as exc:
        raise JSONBodyError("invalid Content-Length") from exc
    if length < 0:
        raise JSONBodyError("invalid Content-Length")
    if length > max_bytes:
        raise JSONBodyError("payload too large", 413)
    return length


def read_json_object(stream, content_length: str | None, max_bytes: int) -> dict:
    """Read one bounded JSON object, rejecting malformed JSON and top-level arrays."""
    length = parse_content_length(content_length, max_bytes)
    raw = stream.read(length) if length else b"{}"
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JSONBodyError("invalid JSON payload") from exc
    if not isinstance(value, dict):
        raise JSONBodyError("request must be a JSON object")
    return value


class SlidingWindowLimiter:
    """Bounded in-memory request limiter keyed by a verified principal or client address."""

    def __init__(self, limit: int, window_seconds: float, max_keys: int = 4096):
        self.limit = int(limit)
        self.window_seconds = float(window_seconds)
        self.max_keys = int(max_keys)
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        now = time.monotonic() if now is None else float(now)
        key = key or "anonymous"
        with self._lock:
            if key not in self._events and len(self._events) >= self.max_keys:
                cutoff = now - self.window_seconds
                for candidate in list(self._events):
                    events = self._events[candidate]
                    while events and events[0] <= cutoff:
                        events.popleft()
                    if not events:
                        del self._events[candidate]
                if len(self._events) >= self.max_keys:
                    del self._events[next(iter(self._events))]
            events = self._events[key]
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                retry = max(1, int(events[0] + self.window_seconds - now + 0.999))
                return False, retry
            events.append(now)
            return True, 0


def allowed_origin(origin: str | None, configured: str) -> str | None:
    """Return the exact allowed origin; never turn an allowlist into ``*``."""
    if not origin:
        return None
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    return origin if origin in allowed else None
