"""Hermetic tests for admin-owned application schema migrations."""
from __future__ import annotations

import pathlib
import re
import sys

import engine.conversations as conversations
from db.reference_grants import (
    _LEGACY_LAZY_FILL_FUNCTIONS,
    apply_shared_read_boundary,
)
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
    assert [migration.version for migration in CHAT_MIGRATIONS] == [1, 2]
    assert CHAT_MIGRATIONS[0].name == "conversation_state"
    connection = _Connection()
    assert migrate_chat(connection) == (1, 2)
    assert migrate_chat(connection) == ()
    assert connection.commits == 2 and connection.rollbacks == 0
    assert any("ALTER TABLE \"chat\".\"conversation\"" in statement
               for statement, _ in connection.cursor_value.statements)


def test_knowledgebase_migration_installs_definer_functions():
    assert [migration.version for migration in KNOWLEDGEBASE_MIGRATIONS] == [1, 2]
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
    assert migrate_knowledgebase(connection) == (1, 2)   # definer functions, then the schedule table
    assert migrate_knowledgebase(connection) == ()
    # Separate ledgers: the chat and knowledgebase entries must not collide on version numbers.
    assert migrate_chat(connection) == (1, 2)


def test_serving_path_has_no_direct_knowledgebase_writes():
    """Shared facts are synchronized offline; requests neither fetch nor write them."""
    assert not pathlib.Path("engine/knowledge_sync.py").exists()
    for relative in ("engine/entities.py", "engine/knowledge_query.py"):
        source = pathlib.Path(relative).read_text(encoding="utf-8")
        assert "engine.knowledge_sync" not in source
        assert "urllib.request" not in source


class _GrantCursor:
    def __init__(self, functions_exist=True, schedule_exists=True):
        self.functions_exist = functions_exist
        self.schedule_exists = schedule_exists
        self.one = None
        self.grants = []

    def execute(self, statement, params=None):
        text = str(statement)
        if "to_regprocedure" in text:
            self.one = (params[0] if self.functions_exist else None,)
        elif "REVOKE EXECUTE ON FUNCTION" in text:
            self.grants.append(text)
        elif "has_function_privilege" in text:
            self.one = (False,)
        elif "has_schema_privilege" in text:
            self.one = (False, False)           # no CREATE, no direct DML
        elif "to_regclass" in text:
            self.one = ("knowledgebase.schedule" if self.schedule_exists else None,)
        elif "GRANT SELECT ON TABLE" in text:
            self.grants.append(text)
        elif "has_table_privilege" in text and "schedule" in text:
            self.one = (True, False)            # SELECT yes, writes no

    def fetchone(self):
        return self.one


def test_shared_read_boundary_revokes_legacy_write_functions():
    cur = _GrantCursor(functions_exist=False)
    apply_shared_read_boundary(cur, "serving")
    cur = _GrantCursor()
    apply_shared_read_boundary(cur, "serving")
    revokes = [statement for statement in cur.grants if "REVOKE EXECUTE" in statement]
    assert len(revokes) == len(_LEGACY_LAZY_FILL_FUNCTIONS) == 3
    # The serving freshness guard READS the catalog, so the bootstrap must grant it — SELECT only.
    assert any("GRANT SELECT ON TABLE" in g and "schedule" in g for g in cur.grants)
    missing = _GrantCursor(schedule_exists=False)
    try:
        apply_shared_read_boundary(missing, "serving")
        raise AssertionError("a missing schedule table must fail the bootstrap")
    except RuntimeError as exc:
        assert "app_migrations" in str(exc)


def test_request_path_contains_no_shared_chat_ddl():
    assert not hasattr(conversations, "_CHAT_DDL")
    assert not hasattr(conversations, "_ensure")


def test_schedule_catalog_is_honest_about_what_it_claims():
    """The catalog must describe the tables the world path ACTUALLY joins, and must not silently
    imply an automatic cadence for manually rebuilt projections."""
    from db.sync.schedule import CATALOG
    names = {m.table_name for m in CATALOG}
    # The tables engine/data/word_*.json routes a filter to must all be declared, or the guard
    # reports "no maintenance record" on ordinary geo questions.
    for routed in ("city", "country", "u_s_state", "exchange_rate"):
        assert routed in names, f"{routed} is joined by the world path but undeclared"
    # The "... in the World" relations are VIEWS over the base tables (db/init.sql); scheduling
    # them would double-count the same data under two names.
    assert not any(n.endswith("in the World") for n in names), "views must not be scheduled"
    by_name = {m.table_name: m for m in CATALOG}
    assert by_name["exchange_rate"].cadence_hours == 24, "the ECB job runs daily"
    assert by_name["exchange_rate"].source_schema == "ecb", "must point at the release table"
    for projection in ("city", "country"):
        assert by_name[projection].cadence_hours is None
        assert "build_qid_world.py" in by_name[projection].note
    assert all(m.cadence_hours is None or m.cadence_hours > 0 for m in CATALOG)


def test_schedule_migration_and_base_schema_agree():
    """A fresh database (init.sql) and a migrated one must end up with the SAME table, or the two
    deployment paths quietly diverge."""
    from db.sync.app_migrations import KNOWLEDGEBASE_MIGRATIONS
    import pathlib
    v2 = [m for m in KNOWLEDGEBASE_MIGRATIONS if m.version == 2]
    assert v2 and v2[0].name == "maintenance_schedule", "the schedule migration must be v2"
    ddl = v2[0].statements[0]
    init = pathlib.Path("db/init.sql").read_text(encoding="utf-8")
    assert 'CREATE TABLE IF NOT EXISTS knowledgebase."schedule"' in init, "init.sql must declare it"
    for column in ("table_name", "source", "cadence_hours", "last_refreshed_at",
                   "last_release_id", "row_count"):
        assert column in ddl and column in init, f"{column} must exist on both paths"
    assert "schedule_cadence_positive" in ddl and "schedule_cadence_positive" in init


def test_serving_guard_consults_the_catalog_instead_of_skipping():
    """Regression for the observed gap: a world table with no per-row updated_at produced NO
    freshness signal at all — the guard simply did not run."""
    import pathlib
    kt = pathlib.Path("engine/knowledge_tables.py").read_text(encoding="utf-8")
    assert "_table_freshness(con, ft, as_of)" in kt, "the no-updated_at branch must consult the catalog"
    guard = kt[kt.index("FRESHNESS GUARD"):]
    guard = guard[:guard.index("if world_rate")]
    assert "else:" in guard, "the missing-updated_at case must be handled, not fall through"
    pg = pathlib.Path("engine/pg.py").read_text(encoding="utf-8")
    # The guard runs inside serve()'s try, where an exception costs the user their answer, so the
    # catalog read must tolerate a database that predates the migration WITHOUT raising.
    assert "to_regclass('knowledgebase.schedule')" in pg, "must probe before selecting"
    # It must also reuse the connection the query already holds. Opening a second connection here
    # turned a transient network blip into a lost answer ("total amount in France" -> None) when
    # this was first written; the signature carries `con` so that cannot regress.
    assert "def _table_freshness(self, con, table, as_of)" in pg, "must read via the live connection"
    assert "_pg()" not in pg[pg.index("def _table_freshness"):pg.index("def _connect")], \
        "the freshness read must not open its own connection"


TESTS = [
    test_chat_migration_is_admin_run_and_idempotent,
    test_knowledgebase_migration_installs_definer_functions,
    test_serving_path_has_no_direct_knowledgebase_writes,
    test_shared_read_boundary_revokes_legacy_write_functions,
    test_schedule_catalog_is_honest_about_what_it_claims,
    test_schedule_migration_and_base_schema_agree,
    test_serving_guard_consults_the_catalog_instead_of_skipping,
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
