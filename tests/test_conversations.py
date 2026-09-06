"""Hermetic tests for conversation quotas, pagination, and deletion."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from engine import conversations


class _Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_conversation_page_uses_a_stable_timestamp_and_id_cursor():
    stamp = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

    class Cursor:
        rows = [("c_" + "c" * 32, "third", stamp),
                ("c_" + "b" * 32, "second", stamp),
                ("c_" + "a" * 32, "first", stamp)]

        def __init__(self):
            self.statement = ""
            self.params = ()

        def execute(self, statement, params=None):
            self.statement = str(statement)
            self.params = tuple(params or ())

        def fetchall(self):
            return self.rows

    cursor = Cursor()
    connection = _Connection(cursor)
    with patch.object(conversations, "_pg", return_value=connection):
        page = conversations.conversation_page("user", limit=2)
    assert [item["question"] for item in page["conversations"]] == ["third", "second"]
    assert page["next_cursor"] == f"{stamp.isoformat()}|c_{'b' * 32}"
    assert "ORDER BY c.created_at DESC, c.conversation_id DESC" in cursor.statement
    assert cursor.params == ("user", 3)
    assert connection.commits == 1 and connection.closed

    cursor = Cursor()
    cursor.rows = []
    connection = _Connection(cursor)
    with patch.object(conversations, "_pg", return_value=connection):
        conversations.conversation_page("user", limit=2, before=page["next_cursor"])
    assert "(c.created_at, c.conversation_id) < (%s, %s)" in cursor.statement
    assert cursor.params == ("user", stamp, "c_" + "b" * 32, 3)


def test_save_state_locks_and_replaces_only_the_previous_state_bytes():
    class Cursor:
        def __init__(self):
            self.statements = []
            self.one = None

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if "SELECT c.source_bytes, c.state_bytes" in text:
                self.one = (100, 20)
            elif "SELECT COALESCE(SUM" in text:
                self.one = (120,)

        def fetchone(self):
            return self.one

    cursor = Cursor()
    connection = _Connection(cursor)
    cid = "c_" + "d" * 32
    with patch.object(conversations, "_pg", return_value=connection), \
            patch.object(conversations.config, "max_conversation_storage_bytes", return_value=200):
        assert conversations.save_state("user", cid, {"ok": True}) == {"saved": cid}
    assert "pg_advisory_xact_lock" in cursor.statements[0][0]
    update = next(item for item in cursor.statements if item[0].startswith('UPDATE "chat"."conversation"'))
    assert update[1][1] == len('{"ok": true}'.encode("utf-8"))
    assert connection.commits == 1 and connection.rollbacks == 0 and connection.closed


def test_save_state_rejects_an_oversized_snapshot_and_rolls_back():
    class Cursor:
        one = (0, 0)

        def execute(self, _statement, _params=None):
            pass

        def fetchone(self):
            return self.one

    connection = _Connection(Cursor())
    with patch.object(conversations, "_pg", return_value=connection), \
            patch.object(conversations, "MAX_STATE_BYTES", 8):
        try:
            conversations.save_state("user", "c_" + "e" * 32, {"value": "too large"})
            raise AssertionError("oversized state was accepted")
        except conversations.QuotaExceeded:
            pass
    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed


def test_delete_all_removes_only_owned_valid_conversations_and_user_traces():
    valid = "c_" + "f" * 32

    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, statement, params=None):
            self.statements.append((str(statement), params))

        def fetchall(self):
            return [(valid,), ("not_a_schema",)]

    cursor = Cursor()
    connection = _Connection(cursor)
    with patch.object(conversations, "_pg", return_value=connection), \
            patch("engine.trace.delete_traces", return_value=7) as delete_traces:
        result = conversations.delete_all_conversations("user", rtdb_uid="firebase-user")
    assert result == {"deleted": 1, "deleted_traces": 7}
    delete_traces.assert_called_once_with("firebase-user")
    drops = [statement for statement, _ in cursor.statements if statement.startswith("DROP SCHEMA")]
    assert drops == [f'DROP SCHEMA IF EXISTS "{valid}" CASCADE']
    assert connection.commits == 1 and connection.closed


TESTS = [
    test_conversation_page_uses_a_stable_timestamp_and_id_cursor,
    test_save_state_locks_and_replaces_only_the_previous_state_bytes,
    test_save_state_rejects_an_oversized_snapshot_and_rolls_back,
    test_delete_all_removes_only_owned_valid_conversations_and_user_traces,
]


def main():
    failures = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nconversations: {len(TESTS) - len(failures)} passed, {len(failures)} failed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
