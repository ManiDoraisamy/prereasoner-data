"""Hermetic tests for admin-owned application schema migrations."""
from __future__ import annotations

import pathlib
import re
import sys

import engine.conversations as conversations
from db.reference_grants import _LAZY_FILL_FUNCTIONS, apply_lazy_fill_grants
from db.sync.app_migrations import (
    CHAT_MIGRATIONS,
    KNOWLEDGEBASE_MIGRATIONS,
    migrate_chat,
    migrate_knowledgebase,
)


class _Cursor:
    def __init__(self):
        self.ledgers = {}                       # schema -> [(version, name, checksum)]
        self.one = None
        self.rows = []
        self.statements = []

    def execute(self, statement, params=None):
        text = str(statement)
        self.statements.append((text, params))
        if "to_regnamespace" in text:
            self.one = (params[0],)
        elif "to_regclass" in text:
            self.one = tuple(params)
        elif "SELECT pg_advisory_xact_lock" in text:
            self.one = (None,)
        else:
            ledger = re.search(r'"([a-z]+)"\."schema_migration"', text)
            if ledger and text.startswith("SELECT version"):
                self.rows = list(self.ledgers.get(ledger.group(1), []))
            elif ledger and text.startswith("INSERT INTO"):
                self.ledgers.setdefault(ledger.group(1), []).append(tuple(params))

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


def test_knowledgebase_migration_installs_definer_functions():
    assert [migration.version for migration in KNOWLEDGEBASE_MIGRATIONS] == [1]
    assert KNOWLEDGEBASE_MIGRATIONS[0].name == "lazy_fill_definer_functions"
    statements = KNOWLEDGEBASE_MIGRATIONS[0].statements
    definers = [s for s in statements if "SECURITY DEFINER" in s]
    assert len(definers) == 3
    # Every definer pins search_path so the dynamic SQL cannot be captured.
    assert all("SET search_path = pg_catalog, pg_temp" in s for s in definers)
    # Default PUBLIC EXECUTE must be revoked on every function.
    revokes = [s for s in statements if s.startswith("REVOKE ALL ON FUNCTION")]
    assert len(revokes) == 3
    # The entity upsert keeps the qid arbiter — the structural guard that limits it
    # to entity-shaped (qid PRIMARY KEY) lazy tables.
    assert any("ON CONFLICT (qid) DO NOTHING" in s for s in definers)
    connection = _Connection()
    assert migrate_knowledgebase(connection) == (1,)
    assert migrate_knowledgebase(connection) == ()
    # Separate ledgers: the chat and knowledgebase v1 entries must not collide.
    assert migrate_chat(connection) == (1,)


def test_serving_path_has_no_direct_knowledgebase_writes():
    """Regression for the live failure: 'CREATE TABLE ... knowledgebase' as the
    SELECT-only serving role -> permission denied for schema knowledgebase."""
    source = pathlib.Path("engine/knowledge_sync.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS knowledgebase" not in source
    assert "INSERT INTO knowledgebase" not in source
    for name in ("lazy_ensure_table", "lazy_upsert_entity", "lazy_register_word"):
        assert f"knowledgebase.{name}(" in source


class _GrantCursor:
    def __init__(self, functions_exist=True):
        self.functions_exist = functions_exist
        self.one = None
        self.grants = []

    def execute(self, statement, params=None):
        text = str(statement)
        if "to_regprocedure" in text:
            self.one = (params[0] if self.functions_exist else None,)
        elif "GRANT EXECUTE ON FUNCTION" in text:
            self.grants.append(text)
        elif "has_function_privilege" in text:
            self.one = (True,)
        elif "has_schema_privilege" in text:
            self.one = (False, False)           # no CREATE, no direct DML

    def fetchone(self):
        return self.one


def test_lazy_fill_grants_require_migration_and_audit_direct_writes():
    cur = _GrantCursor(functions_exist=False)
    try:
        apply_lazy_fill_grants(cur, "serving")
        raise AssertionError("missing functions must fail the bootstrap")
    except RuntimeError as exc:
        assert "app_migrations" in str(exc)
    cur = _GrantCursor()
    apply_lazy_fill_grants(cur, "serving")
    assert len(cur.grants) == len(_LAZY_FILL_FUNCTIONS) == 3


def test_request_path_contains_no_shared_chat_ddl():
    assert not hasattr(conversations, "_CHAT_DDL")
    assert not hasattr(conversations, "_ensure")


TESTS = [
    test_chat_migration_is_admin_run_and_idempotent,
    test_knowledgebase_migration_installs_definer_functions,
    test_serving_path_has_no_direct_knowledgebase_writes,
    test_lazy_fill_grants_require_migration_and_audit_direct_writes,
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
