"""Scheduled RTDB trace retention job; run with Firebase Admin ADC credentials."""
from __future__ import annotations

from engine.trace import cleanup_expired_traces


def main() -> int:
    deleted = cleanup_expired_traces()
    print(f"trace cleanup: deleted {deleted} expired job(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
