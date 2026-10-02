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
from dataclasses import dataclass
from typing import Callable


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


@dataclass
class RequestLease:
    """Idempotent release handle returned by a request gate."""

    release_callback: Callable[[], None]
    _released: bool = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self.release_callback()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


class RequestGate:
    """One reusable process-local rate and concurrency boundary."""

    def __init__(self, *, requests: int, window_seconds: float, in_flight: int, max_keys: int = 4096):
        self.limiter = SlidingWindowLimiter(requests, window_seconds, max_keys=max_keys)
        self.semaphore = threading.BoundedSemaphore(in_flight)

    def acquire(self, key: str) -> tuple[RequestLease | None, int, str | None]:
        allowed, retry_after = self.limiter.allow(key)
        if not allowed:
            return None, retry_after, "rate"
        if not self.semaphore.acquire(blocking=False):
            return None, 1, "concurrency"
        return RequestLease(self.semaphore.release), 0, None


class ResponseReplay:
    """One response per request id, so a caller that lost a response can ask for it again.

    The chat service waits a minute or more for a question, and a response lost between the two
    services ended a turn with "could you send the question again?" although the engine had
    answered it (Chrome gate, 2026-10-02). The caller repeats the request with the same id: a
    finished request's response is sent again, and a request still running is waited for, so the
    question runs once. Entries are keyed by the caller's verified principal and expire after
    `ttl_seconds`; a request that ends without a response releases its id to the next caller. The
    finished responses kept are bounded by count and by the size of their bodies.
    """

    def __init__(self, *, ttl_seconds: float = 300.0, max_entries: int = 64,
                 max_bytes: int = 64 * 1024 * 1024, wait_seconds: float = 270.0,
                 clock: Callable[[], float] = time.monotonic):
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self.wait_seconds = float(wait_seconds)
        self._clock = clock
        self._entries: dict = {}                  # key -> [done Event, response or None, finished at]
        self._lock = threading.Lock()

    def claim(self, key) -> tuple[bool, object]:
        """(True, None) when the caller owns `key` and must `finish` or `release` it; (False,
        response) when an earlier request with `key` produced `response`; (False, None) when that
        request is still running after `wait_seconds`."""
        deadline = self._clock() + self.wait_seconds
        while True:
            with self._lock:
                self._expire()
                entry = self._entries.get(key)
                if entry is None:
                    self._entries[key] = [threading.Event(), None, None]
                    return True, None
            # The entry itself is read once it is set, so a response evicted after it finished is
            # still the one returned.
            if not entry[0].wait(max(0.0, deadline - self._clock())):
                return False, None
            if entry[2] is not None:
                return False, entry[1]
            # Released without a response: the next pass claims the key.

    def finish(self, key, response) -> None:
        """Record the owner's response and wake every caller waiting for it."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0].is_set():
                return
            entry[1], entry[2] = response, self._clock()
            entry[0].set()
            finished = sorted((item[2], name) for name, item in self._entries.items() if item[0].is_set())
            kept = sum(self._size(self._entries[name][1]) for _, name in finished)
            while finished and (len(finished) > self.max_entries or kept > self.max_bytes):
                _, name = finished.pop(0)
                kept -= self._size(self._entries.pop(name)[1])

    def release(self, key) -> None:
        """Give up an owned key that produced no response; the next caller with it runs it."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0].is_set():
                return
            del self._entries[key]
            entry[0].set()

    @staticmethod
    def _size(response) -> int:
        return sum(len(part) for part in response if isinstance(part, (str, bytes))) if response else 0

    def _expire(self) -> None:
        cutoff = self._clock() - self.ttl_seconds
        for name in [name for name, item in self._entries.items()
                     if item[0].is_set() and item[2] is not None and item[2] <= cutoff]:
            del self._entries[name]


def allowed_origin(origin: str | None, configured: str) -> str | None:
    """Return the exact allowed origin; never turn an allowlist into ``*``."""
    if not origin:
        return None
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    return origin if origin in allowed else None
