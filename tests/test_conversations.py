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
            elif 'FROM "chat"."analysis_revision"' in text:
                self.one = (0,)

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
    assert update[1][1] == len(b'{"ok": true}')
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


def test_source_snapshot_hash_is_order_independent_but_value_sensitive():
    orders = {"name": "orders.csv", "data": "id,amount\n1,10\n"}
    tiers = {"name": "tier.csv", "data": "tier,discount\nGold,0.1\n"}
    assert conversations.source_snapshot_hash([orders, tiers]) == \
        conversations.source_snapshot_hash([tiers, orders])
    changed = {**tiers, "data": "tier,discount\nGold,0.2\n"}
    assert conversations.source_snapshot_hash([orders, tiers]) != \
        conversations.source_snapshot_hash([orders, changed])


def test_source_replacement_advances_dataset_version_and_marks_analyses_stale():
    class Cursor:
        def __init__(self):
            self.one = None
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if 'SELECT 1 FROM "chat"."user_conversation"' in text:
                self.one = (1,)
            elif "SELECT source_bytes, source_hash" in text:
                self.one = (10, "0" * 64)
            elif "SUM(c.source_bytes" in text or 'SUM(ar.response_bytes)' in text:
                self.one = (0,)

        def fetchone(self):
            return self.one

    cursor = Cursor()
    connection = _Connection(cursor)
    sheets = [{"name": "orders", "data": "id,amount\n1,12\n"}]
    cid = "c_" + "4" * 32
    with patch.object(conversations, "_pg", return_value=connection), \
            patch.object(conversations.config, "max_conversation_storage_bytes", return_value=1_000_000):
        assert conversations.resolve_conversation("user", cid, "total", sheets) == cid
    assert any(statement.startswith('UPDATE "chat"."analysis" SET stale')
               for statement, _ in cursor.statements)
    update = next((statement, params) for statement, params in cursor.statements
                  if statement.startswith('UPDATE "chat"."conversation" SET tables'))
    assert "dataset_version = dataset_version + %s" in update[0]
    assert update[1][3] == 1


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


def test_append_dataset_ops_is_bounded_and_serialized():
    class Cursor:
        def __init__(self, existing):
            self.existing = existing
            self.updated = None
            self.statements = []

        def execute(self, statement, params=None):
            self.statements.append((str(statement), params))
            if "SELECT dataset_ops" in str(statement):
                self.updated = (self.existing,)
            elif "UPDATE \"chat\".\"conversation\"" in str(statement):
                self.updated = (params[0],)

        def fetchone(self):
            return self.updated

    cid = "c_" + "1" * 32
    cursor = Cursor([])
    connection = _Connection(cursor)
    with patch.object(conversations, "_pg", return_value=connection):
        assert conversations.append_dataset_ops(cid, [{"op": "clear_measure_metadata",
                                                        "table": "responses", "column": "budget"}])
    assert "FOR UPDATE" in cursor.statements[0][0]
    assert any(statement.startswith('UPDATE "chat"."analysis" SET stale')
               for statement, _ in cursor.statements)
    dataset_update = next(statement for statement, _ in cursor.statements
                          if statement.startswith('UPDATE "chat"."conversation"'))
    assert "dataset_version = dataset_version + 1" in dataset_update
    assert connection.commits == 1

    cursor = Cursor([{"op": "clear_measure_metadata", "table": "responses", "column": "budget"}]
                    * conversations.MAX_DATASET_OPS)
    connection = _Connection(cursor)
    with patch.object(conversations, "_pg", return_value=connection):
        try:
            conversations.append_dataset_ops(cid, [{"op": "clear_measure_metadata",
                                                    "table": "responses", "column": "budget"}])
            raise AssertionError("dataset operation log exceeded its bound")
        except conversations.DatasetOpsLimitError:
            pass
    assert connection.commits == 0 and connection.rollbacks == 1

    # Full-log validation runs after the row lock and before UPDATE; a rejection rolls the same
    # transaction back, so no unvalidated prefix can become durable.
    cursor = Cursor([{"op": "set_measure_metadata", "table": "responses", "column": "budget"}])
    connection = _Connection(cursor)
    seen = []

    def reject(combined):
        seen.append(combined)
        raise ValueError("bad log")

    with patch.object(conversations, "_pg", return_value=connection):
        try:
            conversations.append_dataset_ops(
                cid,
                [{"op": "clear_measure_metadata", "table": "responses", "column": "budget"}],
                validate=reject,
            )
            raise AssertionError("validation failure must abort the append")
        except ValueError as exc:
            assert str(exc) == "bad log"
    assert len(seen[0]) == 2
    assert not any("SET dataset_ops" in statement for statement, _ in cursor.statements)
    assert connection.commits == 0 and connection.rollbacks == 1

    # The byte cap is independent of the operation-count cap; a small number of oversized
    # records must not turn JSONB into an unbounded conversation payload.
    cursor = Cursor([])
    connection = _Connection(cursor)
    oversized = [{"op": "clear_measure_metadata", "table": "responses", "column": "budget",
                  "basis": {"source": "conversation", "text": "x" * conversations.MAX_DATASET_OP_BYTES}}]
    with patch.object(conversations, "_pg", return_value=connection):
        try:
            conversations.append_dataset_ops(cid, oversized)
            raise AssertionError("dataset operation bytes exceeded their bound")
        except conversations.DatasetOpsLimitError:
            pass
    assert connection.commits == 0 and connection.rollbacks == 1


def test_analysis_completion_marks_changed_sources_stale_and_advances_monotonically():
    class Cursor:
        def __init__(self):
            self.one = None
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if "SELECT a.slug" in text:
                self.one = ("total_sales", "old", 1, False)
            elif "SELECT ar.status" in text:
                self.one = ("pending", 1, 2)
            elif "SUM(c.source_bytes" in text or 'SUM(ar.response_bytes)' in text:
                self.one = (0,)

        def fetchone(self):
            return self.one

    cursor = Cursor()
    connection = _Connection(cursor)
    descriptor = {"analysis_id": "a_" + "1" * 32, "slug": "total_sales",
                  "revision": 2, "action": "modify", "stale": False}
    response = {"result": {"columns": ["total"], "rows": [["12.30"]]},
                "analysis": descriptor,
                "dataset_semantics": [{"table": "orders", "column": "amount", "currency": "EUR"}],
                "reference": {"source": "ecb"}, "present": True}
    with patch.object(conversations, "_pg", return_value=connection), \
            patch.object(conversations.config, "max_conversation_storage_bytes", return_value=1_000_000):
        snapshot = conversations.complete_analysis(
            "user", "c_" + "2" * 32, descriptor, "total sales", response,
        )
    assert snapshot["analysis"]["stale"] is True
    assert snapshot["dataset_semantics"][0]["currency"] == "EUR"
    assert snapshot["reference"] == {"source": "ecb"} and snapshot["present"] is True
    latest = next((statement, params) for statement, params in cursor.statements
                  if statement.startswith('UPDATE "chat"."analysis" SET latest_question'))
    assert "latest_revision < %s" in latest[0]
    assert latest[1][-1] == 2
    assert connection.commits == 1 and connection.rollbacks == 0 and connection.closed


def test_analysis_reservation_uses_the_effective_request_input_hash():
    class Cursor:
        def __init__(self):
            self.one = None
            self.rows = []
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if 'SELECT c.source_hash, c.dataset_version' in text:
                self.one = ("7" * 64, 3)
            elif 'SELECT COUNT(*) FROM "chat"."analysis"' in text:
                self.one = (0,)
            elif 'SELECT slug FROM "chat"."analysis"' in text:
                self.rows = []

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    cursor = Cursor()
    connection = _Connection(cursor)
    input_hash = "9" * 64
    with patch.object(conversations, "_pg", return_value=connection), \
            patch.object(conversations, "_new_analysis_id", return_value="a_" + "3" * 32):
        descriptor = conversations.begin_analysis(
            "user", "c_" + "2" * 32, {"action": "create", "slug": "total_sales"},
            "total sales", request_input_hash=input_hash, request_source_hash="7" * 64,
        )
    insert = next((statement, params) for statement, params in cursor.statements
                  if statement.startswith('INSERT INTO "chat"."analysis_revision"'))
    assert insert[1][-2:] == (input_hash, 3)
    assert any(statement.startswith('DELETE FROM "chat"."analysis" a')
               for statement, _ in cursor.statements)
    assert descriptor["revision"] == 1 and connection.commits == 1


def test_analysis_reservation_rejects_a_superseded_source_snapshot():
    class Cursor:
        def __init__(self):
            self.one = None

        def execute(self, statement, _params=None):
            if 'SELECT c.source_hash, c.dataset_version' in str(statement):
                self.one = ("1" * 64, 4)

        def fetchone(self):
            return self.one

    connection = _Connection(Cursor())
    with patch.object(conversations, "_pg", return_value=connection):
        try:
            conversations.begin_analysis(
                "user", "c_" + "2" * 32, {"action": "create", "slug": "total_sales"},
                "total sales", request_input_hash="8" * 64, request_source_hash="2" * 64,
            )
            raise AssertionError("an obsolete request reserved a current workbook revision")
        except conversations.AnalysisError as exc:
            assert "source tables changed" in str(exc)
    assert connection.commits == 0 and connection.rollbacks == 1 and connection.closed


def test_modify_reclaims_abandoned_pending_rows_without_reusing_failed_numbers():
    class Cursor:
        def __init__(self):
            self.one = None
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if 'SELECT c.source_hash, c.dataset_version' in text:
                self.one = ("7" * 64, 3)
            elif "SELECT a.slug" in text:
                self.one = ("total_sales", "old total", 1, False)
            elif "SELECT COUNT(*)" in text and 'FROM "chat"."analysis_revision"' in text:
                self.one = (1, 3)  # revision 3 is a retained legacy failed row

        def fetchone(self):
            return self.one

    cursor = Cursor()
    connection = _Connection(cursor)
    with patch.object(conversations, "_pg", return_value=connection):
        descriptor = conversations.begin_analysis(
            "user", "c_" + "2" * 32,
            {"action": "modify", "slug": "total_sales", "analysis_id": "a_" + "1" * 32},
            "total sales in USD", request_input_hash="8" * 64, request_source_hash="7" * 64,
        )
    assert descriptor["revision"] == 4
    assert any('SET status = %s, completed_at = now()' in statement
               for statement, _ in cursor.statements)


def test_failed_modify_keeps_a_zero_payload_revision_tombstone():
    class Cursor:
        def __init__(self):
            self.one = None
            self.statements = []

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if "SELECT a.slug" in text:
                self.one = ("total_sales", "old", 1, False)
            elif "SELECT latest_revision" in text:
                self.one = (1,)

        def fetchone(self):
            return self.one

    cursor = Cursor()
    connection = _Connection(cursor)
    descriptor = {"analysis_id": "a_" + "1" * 32, "slug": "total_sales",
                  "revision": 2, "action": "modify"}
    with patch.object(conversations, "_pg", return_value=connection):
        conversations.fail_analysis("user", "c_" + "2" * 32, descriptor)
    update = next((statement, params) for statement, params in cursor.statements
                  if statement.startswith('UPDATE "chat"."analysis_revision"'))
    assert update[1] == ("failed", descriptor["analysis_id"], 2, "pending")
    assert not any(statement.startswith('DELETE FROM "chat"."analysis_revision"')
                   for statement, _ in cursor.statements)


def test_historical_revision_uses_its_own_input_hash_and_rejects_zero():
    class Cursor:
        def __init__(self):
            self.one = None

        def execute(self, statement, _params=None):
            text = str(statement)
            if "SELECT a.slug" in text:
                self.one = ("total_sales", "latest", 3, False)
            elif "SELECT ar.question" in text:
                self.one = ("old total", "create", {"result": {"rows": [[12]]}},
                            "a" * 64, 1, 2)

        def fetchone(self):
            return self.one

    connection = _Connection(Cursor())
    with patch.object(conversations, "_pg", return_value=connection):
        loaded = conversations.get_analysis_revision(
            "user", "c_" + "2" * 32, "a_" + "1" * 32, revision=1,
        )
    assert loaded["analysis"]["revision"] == 1
    assert loaded["analysis"]["stale"] is True, "staleness belongs to the selected revision"
    assert connection.closed

    try:
        conversations.get_analysis_revision(
            "user", "c_" + "2" * 32, "a_" + "1" * 32, revision=0,
        )
        raise AssertionError("revision zero silently selected the latest workbook")
    except conversations.AnalysisError:
        pass


TESTS = [
    test_conversation_page_uses_a_stable_timestamp_and_id_cursor,
    test_save_state_locks_and_replaces_only_the_previous_state_bytes,
    test_save_state_rejects_an_oversized_snapshot_and_rolls_back,
    test_source_snapshot_hash_is_order_independent_but_value_sensitive,
    test_source_replacement_advances_dataset_version_and_marks_analyses_stale,
    test_delete_all_removes_only_owned_valid_conversations_and_user_traces,
    test_append_dataset_ops_is_bounded_and_serialized,
    test_analysis_completion_marks_changed_sources_stale_and_advances_monotonically,
    test_analysis_reservation_uses_the_effective_request_input_hash,
    test_analysis_reservation_rejects_a_superseded_source_snapshot,
    test_modify_reclaims_abandoned_pending_rows_without_reusing_failed_numbers,
    test_failed_modify_keeps_a_zero_payload_revision_tombstone,
    test_historical_revision_uses_its_own_input_hash_and_rejects_zero,
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
