"""Scheduled retention owner for PostgreSQL conversations and RTDB traces."""
from __future__ import annotations

from engine.conversations import cleanup_expired_conversations
from engine.sheet_sessions import cleanup_expired_sheet_sessions
from engine.trace import cleanup_expired_traces


def main():
    sheet_sessions = cleanup_expired_sheet_sessions()
    conversations = cleanup_expired_conversations()
    traces = cleanup_expired_traces()
    print(
        f"retention cleanup: sheet_sessions={sheet_sessions} conversations={conversations} traces={traces}",
        flush=True,
    )


if __name__ == "__main__":
    main()
