"""Hermetic tests for per-Google-Sheet sidebar restoration."""
from __future__ import annotations

import json
from unittest.mock import patch

from engine import sheet_sessions
from engine.conversations import source_snapshot_hash


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


def test_restore_returns_the_mapped_sidebar_snapshot():
    cid = "c_" + "1" * 32
    tables = [{"name": "Customers", "data": "order ID,customer\n101,Sherlock Holmes\n"}]
    source_hash = source_snapshot_hash(tables)
    saved = {"client": "google-sheets-addon", "version": 1, "turns": [{"question": "Who?"}]}

    class Cursor:
        def __init__(self):
            self.one = None
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if 'FROM "chat"."sheet_session" ss' in text:
                self.one = (cid, json.dumps(saved), "Who?", source_hash, 3)

        def fetchone(self):
            return self.one

    cursor = Cursor()
    connection = _Connection(cursor)
    with patch.object(sheet_sessions, "_pg", return_value=connection):
        restored = sheet_sessions.restore_sheet_session("user", "sheet_1234567890", tables)
    assert restored["conversation_id"] == cid
    assert restored["state"] == saved
    assert restored["source_changed"] is False and restored["legacy"] is False
    assert connection.commits == 1 and connection.rollbacks == 0 and connection.closed


def test_restore_backfills_the_latest_exact_source_conversation_once():
    cid = "c_" + "2" * 32
    tables = [{"name": "Customers", "data": "id,name\n1,Nancy Drew\n"}]
    source_hash = source_snapshot_hash(tables)

    class Cursor:
        def __init__(self):
            self.one = None
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if 'FROM "chat"."sheet_session" ss' in text:
                self.one = None
            elif 'ORDER BY c.last_active_at DESC' in text:
                self.one = (cid, "Who has the most orders?", source_hash, 4)
            elif 'SELECT count(*) FROM "chat"."sheet_session"' in text:
                self.one = (0,)

        def fetchone(self):
            return self.one

    cursor = Cursor()
    connection = _Connection(cursor)
    with patch.object(sheet_sessions, "_pg", return_value=connection):
        restored = sheet_sessions.restore_sheet_session("user", "sheet_abcdefghij", tables)
    assert restored["conversation_id"] == cid and restored["legacy"] is True
    insert = next(item for item in cursor.statements if item[0].startswith('INSERT INTO "chat"."sheet_session"'))
    assert insert[1][2] == cid


def test_save_requires_ownership_and_persists_a_bounded_snapshot():
    cid = "c_" + "3" * 32
    state = {"client": "google-sheets-addon", "version": 1, "turns": []}

    class Cursor:
        def __init__(self):
            self.one = None
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if 'SELECT 1 FROM "chat"."user_conversation"' in text:
                self.one = (1,)
            elif 'SELECT state_bytes FROM "chat"."sheet_session"' in text:
                self.one = None
            elif 'SELECT count(*) FROM "chat"."sheet_session"' in text:
                self.one = (0,)

        def fetchone(self):
            return self.one

    cursor = Cursor()
    connection = _Connection(cursor)
    with patch.object(sheet_sessions, "_pg", return_value=connection):
        result = sheet_sessions.save_sheet_session("user", "sheet_klmnopqrst", cid, state)
    assert result["saved"] == cid
    upsert = next(item for item in cursor.statements if 'sidebar_state = EXCLUDED.sidebar_state' in item[0])
    assert json.loads(upsert[1][3]) == state
    assert upsert[1][4] == len(upsert[1][3].encode("utf-8"))


def test_clear_persists_a_blank_marker_instead_of_deleting_the_mapping():
    class Cursor:
        def __init__(self):
            self.one = None
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if 'SELECT 1 FROM "chat"."sheet_session"' in text:
                self.one = (1,)

        def fetchone(self):
            return self.one

    cursor = Cursor()
    connection = _Connection(cursor)
    with patch.object(sheet_sessions, "_pg", return_value=connection):
        result = sheet_sessions.clear_sheet_session("user", "sheet_uvwx123456")
    assert result == {"cleared": "sheet_uvwx123456"}
    assert any('conversation_id = NULL' in statement for statement, _ in cursor.statements)


TESTS = (
    test_restore_returns_the_mapped_sidebar_snapshot,
    test_restore_backfills_the_latest_exact_source_conversation_once,
    test_save_requires_ownership_and_persists_a_bounded_snapshot,
    test_clear_persists_a_blank_marker_instead_of_deleting_the_mapping,
)


if __name__ == "__main__":
    failures = []
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        raise SystemExit(1)
    print(f"sheet sessions: {len(TESTS)} passed")
