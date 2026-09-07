"""test_pg_upload.py — the uploaded-sheet load must not issue one statement per row.

Registered in tests/run_all.py. Hermetic: a recording cursor stands in for psycopg2, so this runs
without a database and pins BOTH halves of the contract — the statement count is bounded by pages
rather than rows (the regression), and every value handed to the driver is byte-for-byte what the
per-row loop passed (coercion, exact decimals, column order, NULLs).
"""
from __future__ import annotations

import sys
from decimal import Decimal

from engine.pg import _UPLOAD_PAGE_SIZE, _load_user_schema
from engine.knowledge_tables import KnowledgeTableQuery


class _Connection:
    """The one attribute execute_values reads off the cursor's connection (for the wire encoding)."""
    encoding = "UTF8"


class RecordingCursor:
    """Records statements. `execute_values` calls .execute with the fully-built statement, so the
    count it produces here is the count a real connection would send."""

    def __init__(self):
        self.statements = []
        self.connection = _Connection()

    def execute(self, sql, args=None):
        self.statements.append((sql if isinstance(sql, str) else sql.decode("utf-8"), args))

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
    inserts = [s for s, _ in cur.statements if s.lstrip().upper().startswith("INSERT")]
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
    inserts = [s for s, _ in cur.statements if s.lstrip().upper().startswith("INSERT")]
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


TESTS = [
    test_statement_count_is_bounded_by_pages_not_rows,
    test_every_row_is_loaded_with_the_same_coercion,
    test_fractional_values_stay_exact_decimals,
    test_null_survives_batching,
    test_empty_sheet_issues_no_insert,
    test_table_is_still_replaced_on_reupload,
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
