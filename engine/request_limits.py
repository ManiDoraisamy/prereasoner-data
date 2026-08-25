"""Small, dependency-free request guards shared by the HTTP entry points.

These guards are deliberately process-local.  Cloud Run can run several instances, so
they are a last-mile protection against one instance being exhausted, not an accounting
or billing control.  Global quotas belong at the edge or in an authenticated gateway.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


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
