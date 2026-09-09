"""test_pg_upload.py — the uploaded-sheet load must not issue one statement per row.

Registered in tests/run_all.py. Hermetic: a recording cursor stands in for psycopg2, so this runs
without a database and pins BOTH halves of the contract — the statement count is bounded by pages
rather than rows (the regression), and every value handed to the driver is byte-for-byte what the
per-row loop passed (coercion, exact decimals, column order, NULLs).
"""
from __future__ import annotations

import sys
from decimal import Decimal

import psycopg2.extensions

from engine.knowledge_tables import KnowledgeTableQuery
from engine.pg import _PgCon, _UPLOAD_PAGE_SIZE, _load_user_schema


class _Connection:
    """The one attribute execute_values reads off the cursor's connection (for the wire encoding)."""
    encoding = "UTF8"


class RecordingCursor:
    """Records statements. `execute_values` calls .execute with the fully-built statement, so the
    count it produces here is the count a real connection would send."""

    def __init__(self):
        self.statements = []
        self.connection = _Connection()
        self.content_hash = None
        self.relation_exists = False
        self.one = None
        self.manifest_tables = []

    def execute(self, sql, args=None):
        text = sql if isinstance(sql, str) else sql.decode("utf-8")
        self.statements.append((text, args))
        if 'SELECT content_hash FROM "chat"."working_table"' in text:
            self.one = (self.content_hash,) if self.content_hash else None
        elif "SELECT to_regclass" in text:
            self.one = ('"tenant"."sales"',) if self.relation_exists else (None,)
        elif 'INSERT INTO "chat"."working_table"' in text:
            self.content_hash = args[2]
            self.relation_exists = True

    def fetchone(self):
        return self.one

    def fetchall(self):
        return [(name,) for name in self.manifest_tables]

    def mogrify(self, template, row):
        # psycopg2's execute_values builds each row through mogrify; mirror its bytes contract and
        # keep the coerced values so the test can assert on exactly what would be sent.
        rendered = b"(" + b",".join(_render(v) for v in row) + b")"
        self.recorded_rows = getattr(self, "recorded_rows", [])
        self.recorded_rows.append(list(row))
        return rendered


def _render(v):
    if v is None:
        return b"NULL"
    if isinstance(v, (int, Decimal, float)):
        return str(v).encode("utf-8")
    return ("'" + str(v).replace("'", "''") + "'").encode("utf-8")


def _fixture(n_rows):
    """One sheet: a text key, an integer, and a fractional decimal that must stay exact."""
    sch = [
        {"table": "sales", "name": "city", "affinity": "TEXT"},
        {"table": "sales", "name": "units", "affinity": "INTEGER"},
        {"table": "sales", "name": "revenue", "affinity": "REAL"},
    ]
    rows = [[f"city{i}", i, f"{i}.25"] for i in range(n_rows)]
    if rows:
        rows[0][0] = None                               # a NULL must survive the batching unchanged
    tablemap = {"sales": {"name": "sales", "columns": ["city", "units", "revenue"], "rows": rows}}
    return sch, tablemap


def test_statement_count_is_bounded_by_pages_not_rows():
    """THE REGRESSION: 1000 rows used to mean 1000 INSERT statements (one network round trip each)."""
    sch, tablemap = _fixture(1000)
    cur = RecordingCursor()
    _load_user_schema(cur, "tenant", sch, tablemap)
    inserts = [s for s, _ in cur.statements if s.lstrip().upper().startswith('INSERT INTO "TENANT"."SALES"')]
    expected_pages = -(-1000 // _UPLOAD_PAGE_SIZE)      # ceil
    assert len(inserts) == expected_pages, (
        f"expected {expected_pages} paged INSERTs for 1000 rows, got {len(inserts)}")
    assert len(inserts) < 1000, "one statement per row is the bug this test exists to catch"


def test_every_row_is_loaded_with_the_same_coercion():
    """Batching must not change a single value the driver receives."""
    sch, tablemap = _fixture(7)
    cur = RecordingCursor()
    _load_user_schema(cur, "tenant", sch, tablemap)
    sent = cur.recorded_rows
    assert len(sent) == 7, f"every row must be sent exactly once, got {len(sent)}"

    cols = [c for c in sch]
    for i, row in enumerate(tablemap["sales"]["rows"]):
        rd = dict(zip(tablemap["sales"]["columns"], row))
        expected = [KnowledgeTableQuery._coerce(rd.get(c["name"]), c["affinity"]) for c in cols]
        assert sent[i] == expected, f"row {i}: batched {sent[i]!r} != per-row {expected!r}"


def test_fractional_values_stay_exact_decimals():
    """NUMERIC exactness is the reason uploads are not floats; batching must not reintroduce binary."""
    sch, tablemap = _fixture(3)
    cur = RecordingCursor()
    _load_user_schema(cur, "tenant", sch, tablemap)
    revenues = [row[2] for row in cur.recorded_rows]
    assert all(not isinstance(v, float) for v in revenues), f"a float crept in: {revenues}"
    assert revenues[1] == KnowledgeTableQuery._coerce("1.25", "REAL")
    assert str(revenues[1]) == "1.25", f"decimal representation drifted: {revenues[1]!r}"


def test_null_survives_batching():
    sch, tablemap = _fixture(4)
    cur = RecordingCursor()
    _load_user_schema(cur, "tenant", sch, tablemap)
    assert cur.recorded_rows[0][0] is None, "a NULL cell must not become the string 'None'"


def test_empty_sheet_issues_no_insert():
    sch, tablemap = _fixture(0)
    cur = RecordingCursor()
    _load_user_schema(cur, "tenant", sch, tablemap)
    inserts = [s for s, _ in cur.statements if s.lstrip().upper().startswith('INSERT INTO "TENANT"."SALES"')]
    assert inserts == [], f"an empty sheet must not INSERT: {inserts}"
    created = [s for s, _ in cur.statements if "CREATE TABLE" in s.upper()]
    assert created, "the table must still be created for an empty sheet"


def test_table_is_still_replaced_on_reupload():
    """DROP+CREATE ordering is part of the contract — a re-upload replaces, never appends."""
    sch, tablemap = _fixture(3)
    cur = RecordingCursor()
    _load_user_schema(cur, "tenant", sch, tablemap)
    kinds = [s.split(None, 2)[0].upper() + " " + s.split(None, 2)[1].upper()
             for s, _ in cur.statements if s.split()]
    drop_at = next(i for i, k in enumerate(kinds) if k.startswith("DROP"))
    create_at = next(i for i, k in enumerate(kinds) if k.startswith("CREATE TABLE"))
    insert_at = next(i for i, k in enumerate(kinds) if k.startswith("INSERT"))
    assert drop_at < create_at < insert_at, f"DROP -> CREATE -> INSERT order broke: {kinds}"


def test_unchanged_source_table_is_reused_without_table_writes():
    sch, tablemap = _fixture(3)
    cur = RecordingCursor()
    _load_user_schema(cur, "tenant", sch, tablemap)
    before = len(cur.statements)
    _load_user_schema(cur, "tenant", sch, tablemap)
    followup = [statement for statement, _ in cur.statements[before:]]
    assert not any(statement.startswith(('DROP TABLE', 'CREATE TABLE', 'INSERT INTO "tenant"."sales"'))
                   for statement in followup), followup


def test_changed_working_table_does_not_invalidate_unrelated_analyses():
    sch, tablemap = _fixture(2)
    cur = RecordingCursor()
    cur.content_hash = "0" * 64
    cur.relation_exists = True
    _load_user_schema(cur, "tenant", sch, tablemap)
    statements = [statement for statement, _ in cur.statements]
    replacement = next(i for i, statement in enumerate(statements)
                       if statement.startswith('DROP TABLE'))
    assert replacement >= 0
    assert not any(statement.startswith('UPDATE "chat"."analysis" SET stale')
                   for statement in statements)


def test_removed_working_table_is_dropped_without_cross_analysis_invalidation():
    sch, tablemap = _fixture(2)
    cur = RecordingCursor()
    cur.manifest_tables = ["sales", "obsolete"]
    _load_user_schema(cur, "tenant", sch, tablemap)
    statements = [(statement, params) for statement, params in cur.statements]
    assert any(statement == 'DROP TABLE IF EXISTS "tenant"."obsolete" CASCADE'
               for statement, _ in statements)
    assert not any(statement.startswith('UPDATE "chat"."analysis" SET stale')
                   for statement, _ in statements)
    deletion = next((statement, params) for statement, params in statements
                    if statement.startswith('DELETE FROM "chat"."working_table"'))
    assert deletion[1] == ("tenant", ["obsolete"])


def test_world_connection_commits_success_and_rolls_back_failed_sql():
    class Connection:
        def __init__(self, status):
            self.status = status
            self.commits = self.rollbacks = self.closes = 0

        def get_transaction_status(self):
            return self.status

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closes += 1

    good = Connection(psycopg2.extensions.TRANSACTION_STATUS_INTRANS)
    _PgCon(good).close()
    assert (good.commits, good.rollbacks, good.closes) == (1, 0, 1)
    failed = Connection(psycopg2.extensions.TRANSACTION_STATUS_INERROR)
    _PgCon(failed).close()
    assert (failed.commits, failed.rollbacks, failed.closes) == (0, 1, 1)


TESTS = [
    test_statement_count_is_bounded_by_pages_not_rows,
    test_every_row_is_loaded_with_the_same_coercion,
    test_fractional_values_stay_exact_decimals,
    test_null_survives_batching,
    test_empty_sheet_issues_no_insert,
    test_table_is_still_replaced_on_reupload,
    test_unchanged_source_table_is_reused_without_table_writes,
    test_changed_working_table_does_not_invalidate_unrelated_analyses,
    test_removed_working_table_is_dropped_without_cross_analysis_invalidation,
    test_world_connection_commits_success_and_rolls_back_failed_sql,
]


def main():
    failures = 0
    for t in TESTS:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
