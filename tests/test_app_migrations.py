"""Hermetic tests for admin-owned application schema migrations."""
from __future__ import annotations

import sys

import engine.conversations as conversations
from db.sync.app_migrations import CHAT_MIGRATIONS, migrate_chat


class _Cursor:
    def __init__(self):
        self.applied = []
        self.one = None
        self.rows = []
        self.statements = []

    def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append((text, params))
        if "to_regnamespace" in text:
            self.one = ("chat",)
        elif "to_regclass" in text:
            self.one = ("chat.user_profile", "chat.conversation", "chat.user_conversation")
        elif "SELECT pg_advisory_xact_lock" in text:
            self.one = (None,)
        elif 'SELECT version, name, checksum FROM "chat"."schema_migration"' in text:
            self.rows = list(self.applied)
        elif 'INSERT INTO "chat"."schema_migration"' in text:
            self.applied.append(tuple(params))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Connection:
    def __init__(self):
        self.cursor_value = _Cursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_chat_migration_is_admin_run_and_idempotent():
    assert [migration.version for migration in CHAT_MIGRATIONS] == [1]
    assert CHAT_MIGRATIONS[0].name == "conversation_state"
    connection = _Connection()
    assert migrate_chat(connection) == (1,)
    assert migrate_chat(connection) == ()
    assert connection.commits == 2 and connection.rollbacks == 0
    assert any("ALTER TABLE \"chat\".\"conversation\"" in statement
               for statement, _ in connection.cursor_value.statements)


def test_request_path_contains_no_shared_chat_ddl():
    assert not hasattr(conversations, "_CHAT_DDL")
    assert not hasattr(conversations, "_ensure")


TESTS = [
    test_chat_migration_is_admin_run_and_idempotent,
    test_request_path_contains_no_shared_chat_ddl,
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
    print(f"\napp migrations: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
