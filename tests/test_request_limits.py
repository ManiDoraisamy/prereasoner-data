"""Hermetic security and resource-bound tests."""
from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

from engine import config
from engine.request_budget import BudgetPolicy, PostgresRequestBudget
from engine.request_limits import (
    JSONBodyError,
    RequestGate,
    SlidingWindowLimiter,
    allowed_origin,
    parse_content_length,
    read_json_object,
)
from engine.request_validation import (
    MAX_TABLE_IDENTIFIER_BYTES,
    MAX_UPLOAD_ROWS,
    RequestValidationError,
    canonical_table_name,
    upload_row_limit_error,
    validate_reason_request,
)
from orchestrator.validation import validate_chat_request


def test_sliding_window_limiter_is_bounded_and_expires():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10)
    assert limiter.allow("u", now=100) == (True, 0)
    assert limiter.allow("u", now=101) == (True, 0)
    allowed, retry = limiter.allow("u", now=102)
    assert not allowed and retry > 0
    assert limiter.allow("u", now=111) == (True, 0)
    bounded = SlidingWindowLimiter(limit=1, window_seconds=10, max_keys=1)
    assert bounded.allow("first", now=100)[0]
    assert bounded.allow("second", now=100)[0]


def test_server_500_logs_where_it_failed_without_user_data():
    """Regression for a 2026-09-07 production diagnosis dead-end: `world request failed: TypeError`
    named no location, so the failing line could not be found without reproducing against
    production. The handler now records code positions (file:line:function) and MUST NOT record the
    exception message, which can quote a cell value or the user's question."""
    import pathlib
    source = pathlib.Path("engine/server.py").read_text(encoding="utf-8")
    handler = source[source.index("world request failed"):]
    handler = handler[:handler.index("self._send(500")]
    assert "traceback.extract_tb" in source, "the 500 path must capture the traceback frames"
    assert "f.lineno" in source and "f.name" in source, "log file:line:function, not just the class"
    assert "{e}" not in handler and "str(e)" not in handler, (
        "the exception MESSAGE may quote user data — log positions only")


def test_chat_authenticates_without_a_database_and_signs_with_the_firebase_uid():
    """Release review of c8533ca (2026-09-24): the storage principal moved into Postgres
    (chat.auth_principal), and the chat server resolved it on every turn. The chat image ships no
    database driver and the chat service has no database, so every chat turn would have answered
    "sign in required". The chat now only verifies the token, the engine alone resolves the storage
    principal, and dataset attestations are keyed by the verified Firebase UID both sides know."""
    import json
    import pathlib
    import re
    import sys
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from engine import auth
    from orchestrator import server

    class Firebase:
        @staticmethod
        def verify_id_token(token):
            assert token == "id-token"
            return {"uid": "fb-uid-1", "firebase": {"identities": {"google.com": ["google-sub-1"]}}}

    def storage_principal(*_args):
        raise AssertionError("the chat service must not resolve the storage principal")

    seen = {}

    async def run_chat(message, tables, history, **kwargs):
        seen.update(kwargs)
        return {"reply": "ok", "traces": [], "history": [], "conversation_id": None}

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    body = json.dumps({"message": "total amount",
                       "tables": [{"name": "orders", "data": "id,amount\n1,2\n"}]}).encode()
    try:
        with patch.object(auth, "_FB_AUTH", Firebase), \
                patch.object(auth, "auth_test_sub", lambda: None), \
                patch.object(auth, "_storage_principal", storage_principal), \
                patch.dict(sys.modules, {"engine.pg": None}), \
                patch.object(config, "external_llm_enabled", lambda: True), \
                patch.object(server, "run_chat", run_chat):
            request = urllib.request.Request(
                f"http://127.0.0.1:{httpd.server_address[1]}/chat", data=body, method="POST",
                headers={"Authorization": "Bearer id-token", "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                assert response.status == 200
            assert seen["principal"] == "fb-uid-1", seen
            # The engine maps the same identity to its storage principal.
            with patch.object(auth, "_storage_principal", lambda uid, google: f"owner:{google}"):
                assert auth._verify_principal("id-token") == ("owner:google-sub-1", "fb-uid-1")
    finally:
        httpd.shutdown()
    source = pathlib.Path("engine/server.py").read_text(encoding="utf-8")
    assert re.search(r"dataset_attestation\.verify\(\s*uid\b", source), \
        "the engine must verify a dataset attestation against the key the chat signs with"


def test_cors_requires_exact_configured_origin():
    assert allowed_origin("https://app.example", "https://app.example") == "https://app.example"
    assert allowed_origin("https://evil.example", "https://app.example") is None
    assert allowed_origin("https://app.example.evil", "https://app.example") is None


def test_auth_test_sub_is_ignored_outside_explicit_nonproduction():
    with patch.dict("os.environ", {"AUTH_TEST_SUB": "local", "APP_ENV": "production"}, clear=False):
        with patch.object(config, "APP_ENV", "production"):
            assert config.auth_test_sub() is None
    with patch.dict("os.environ", {"AUTH_TEST_SUB": "local", "APP_ENV": "test"}, clear=False):
        with patch.object(config, "APP_ENV", "test"):
            assert config.auth_test_sub() == "local"


def test_conversation_lifecycle_limits_are_bounded_and_configurable():
    with patch.dict("os.environ", {
        "CONVERSATION_RETENTION_DAYS": "99999",
        "MAX_CONVERSATIONS_PER_USER": "0",
        "MAX_CONVERSATION_STORAGE_BYTES": "1",
    }, clear=False):
        assert config.conversation_retention_days() == 3650
        assert config.max_conversations_per_user() == 1
        assert config.max_conversation_storage_bytes() == 1024 * 1024


def test_admin_access_fails_closed_without_an_explicit_allowlist():
    from engine.admin import _admins

    with patch.dict("os.environ", {}, clear=True):
        assert _admins() == set()
    with patch.dict("os.environ", {"ADMIN_EMAILS": "a@example.com, B@example.com"}, clear=True):
        assert _admins() == {"a@example.com", "b@example.com"}


def test_postgres_connect_retries_transport_errors_but_not_authentication():
    import psycopg2

    from engine import pg

    connection = object()
    transient = psycopg2.OperationalError("connection timed out")
    with patch.object(pg, "kb_pg_password", return_value="secret"), \
            patch.object(pg.time, "sleep") as sleep, \
            patch.object(pg.psycopg2, "connect", side_effect=[transient, connection]) as connect:
        assert pg._pg() is connection
        assert connect.call_count == 2
        sleep.assert_called_once_with(0.25)

    denied = psycopg2.OperationalError("password authentication failed for user serving")
    with patch.object(pg, "kb_pg_password", return_value="wrong"), \
            patch.object(pg.time, "sleep") as sleep, \
            patch.object(pg.psycopg2, "connect", side_effect=denied) as connect:
        try:
            pg._pg()
            raise AssertionError
        except psycopg2.OperationalError:
            pass
        assert connect.call_count == 1
        sleep.assert_not_called()


def test_cached_connection_dropped_between_requests_is_replaced_before_use():
    """2026-09-25 15:44 UTC (the Sheets add-on; five times since 08-30): the engine's one
    cross-request connection was dropped between requests, `_rconn()` handed it back, and the
    world bridge's first statement failed with `server closed the connection unexpectedly`
    (HTTP 500). psycopg2 marks a connection closed only after a statement fails on it, so the dead
    connection looked healthy until a user's request paid for it. The shortest observed gap
    between the last good use and the failing request was 8 s (09-10)."""
    import contextlib
    import io
    from types import SimpleNamespace

    import psycopg2

    from engine import entities, request_timing

    class Connection:
        def __init__(self):
            self.alive, self.closed, self.autocommit, self.statements = True, 0, False, []

        def cursor(self):
            connection = self

            class Cursor:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def execute(self, sql, params=None):
                    connection.statements.append(sql)
                    if not connection.alive:
                        connection.closed = 2
                        raise psycopg2.OperationalError("server closed the connection unexpectedly")

            return Cursor()

        def close(self):
            self.closed = self.closed or 1

    def resolver():
        query = entities.EntityQuery.__new__(entities.EntityQuery)   # the connection cache only
        query._rcn = None
        return query

    clock = SimpleNamespace(now=1000.0)
    fake_time = SimpleNamespace(monotonic=lambda: clock.now)

    # The production failure: the session dies between requests, then the next request arrives.
    dropped, fresh = Connection(), Connection()
    query = resolver()
    with patch.object(entities, "_pg", side_effect=[dropped, fresh]), \
            patch.object(entities, "time", fake_time, create=True):
        assert query._rconn() is dropped and dropped.autocommit
        dropped.alive = False
        clock.now += 8                                          # the shortest measured gap
        timing = request_timing.begin("stale")
        try:
            handed = query._rconn()
            line = io.StringIO()
            with contextlib.redirect_stdout(line):
                request_timing.emit("reason")
        finally:
            request_timing.end(timing)
    assert handed is not dropped, "a connection dropped between requests was handed to a request"
    assert handed is fresh and fresh.autocommit and dropped.closed
    assert "pg_stale_reconnect_ms=" in line.getvalue(), "the replacement must be visible in the timing line"

    # Contrast: a live connection reached after a pause costs one probe and is kept; statements
    # within a request, milliseconds apart, are never probed, so a busy request pays nothing.
    live = Connection()
    query = resolver()
    with patch.object(entities, "_pg", side_effect=[live]) as connect, \
            patch.object(entities, "time", fake_time, create=True):
        assert query._rconn() is live
        clock.now += 0.004
        assert query._rconn() is live and live.statements == []
        clock.now += 8
        assert query._rconn() is live and live.statements == ["SELECT 1"]
        assert connect.call_count == 1


def test_chat_validation_normalizes_and_bounds_inputs():
    out = validate_chat_request({
        "message": "  total amount  ",
        "tables": [{"name": " orders ", "data": "id,amount\n1,2\n"}],
        "history": [{"role": "user", "content": "hello"}],
        "turnId": " t1 ",
        "use": "both",
        "analysis": {"action": "modify", "analysis_id": "a_" + "1" * 32,
                     "slug": "total_amount"},
    })
    assert out[:3] == ("total amount", [{"name": "orders", "data": "id,amount\n1,2\n"}],
                       [{"role": "user", "content": "hello"}])
    assert out[4] is None and out[5] == "verify"
    assert out[6] == {"action": "modify", "analysis_id": "a_" + "1" * 32,
                      "slug": "total_amount", "revision": None}
    for bad in ({"message": "x" * 20_001}, {"message": "x", "tables": [{}] * 9}):
        try:
            validate_chat_request(bad)
            raise AssertionError
        except ValueError:
            pass


def test_a_long_conversation_keeps_its_most_recent_history_window():
    """Chrome pass (2026-09-24): a conversation past twelve turns failed every new message with
    "history is too long", because the browser sends the whole transcript and the limit rejected
    it. The chat request now keeps the most recent window, starting at a user message."""
    from engine.request_validation import MAX_HISTORY_CHARS, MAX_HISTORY_ITEMS

    turns = [{"role": role, "content": f"{role} {i}"}
             for i in range(17) for role in ("user", "assistant")]
    history = validate_chat_request({"message": "total", "history": turns})[2]
    assert history == turns[-MAX_HISTORY_ITEMS:] and history[0]["role"] == "user"
    # A character budget cut keeps whole messages and still starts at a user message.
    wide = [{"role": role, "content": role[0] * 9_000} for _ in range(6)
            for role in ("user", "assistant")]
    history = validate_chat_request({"message": "total", "history": wide})[2]
    assert sum(len(item["content"]) for item in history) <= MAX_HISTORY_CHARS
    assert history == wide[-len(history):] and history[0]["role"] == "user" and len(history) == 8
    # Malformed input inside the window is still refused.
    for bad in ([{"role": "system", "content": "x"}], "not a list",
                [{"role": "user", "content": "x" * 20_001}]):
        try:
            validate_chat_request({"message": "total", "history": bad})
            raise AssertionError(bad)
        except ValueError:
            pass


def test_table_names_are_canonical_bounded_and_unique_at_every_boundary():
    long_name = "Quarterly Revenue " * 20
    canonical = canonical_table_name(long_name)
    assert len(canonical.encode("ascii")) <= MAX_TABLE_IDENTIFIER_BYTES
    assert canonical == canonical_table_name(long_name)
    assert canonical_table_name("Sales.csv") == "sales"
    assert canonical_table_name("***", 3) == "t3"

    duplicate = {
        "question": "total amount",
        "tables": [
            {"name": "Sales.csv", "data": "amount\n1"},
            {"name": "sales", "data": "amount\n2"},
        ],
    }
    for validate, request in (
        (validate_reason_request, duplicate),
        (validate_chat_request, {"message": "total amount", "tables": duplicate["tables"]}),
    ):
        try:
            validate(request)
            raise AssertionError("canonical table-name collision was accepted")
        except RequestValidationError as exc:
            assert exc.status_code == 400 and "same identifier" in str(exc)


def test_reason_validation_rejects_unbounded_or_invalid_fields():
    valid = validate_reason_request({
        "question": " total amount ",
        "tables": {"name": "Revenue Report.xlsx", "data": "amount\n1"},
        "jobId": "job_1",
    })
    assert valid["question"] == "total amount"
    assert valid["tables"] == [{"name": "revenue_report", "data": "amount\n1"}]
    assert valid["jobId"] == "job_1"
    sourced = validate_reason_request({
        "question": "total amount",
        "tables": {"name": "Orders", "data": "amount\n1", "source": {"kind": "google-sheets-addon"}},
    })
    assert sourced["tables"][0]["source"] == {"kind": "google-sheets-addon"}
    assert validate_reason_request({
        "question": "total amount", "tables": [], "use": "py",
    })["use"] == "python"
    named = validate_reason_request({
        "question": "total amount", "tables": {"name": "orders", "data": "amount\n1"},
        "analysis": {"action": "create", "slug": "Total Sales"},
    })
    assert named["analysis"] == {
        "action": "create", "analysis_id": None, "slug": "total_sales", "revision": None,
    }
    for body in (
        {"question": 1, "tables": []},
        {"question": "x", "tables": [], "jobId": "bad/path"},
        {"question": "x", "tables": [], "as_of": "yesterday"},
        {"question": "x", "tables": [], "conversation_id": "c_not-an-id"},
        {"question": "x", "tables": [{"name": 0, "data": "a\n1"}]},
        {"question": "x", "tables": [{"name": "data", "data": 0}]},
        {"question": "x", "tables": [{"name": "data", "data": "a\n1", "source": {"kind": "unknown"}}]},
        {"question": "x", "tables": [], "analysis": {"action": "modify", "slug": "sales"}},
        {"question": "x", "tables": [], "analysis": {"action": "create", "slug": "sales",
                                                           "analysis_id": "a_" + "1" * 32}},
        {"question": "x", "tables": [], "use": "javascript"},
        {"question": "x", "tables": [], "use": 1},
    ):
        try:
            validate_reason_request(body)
            raise AssertionError("invalid reasoning request was accepted")
        except RequestValidationError:
            pass


def test_uploaded_row_limit_rejects_without_truncating_at_the_public_boundary():
    rows = [[index] for index in range(MAX_UPLOAD_ROWS)]
    table = {"name": "orders", "rows": rows}
    assert upload_row_limit_error([table]) is None
    assert len(table["rows"]) == MAX_UPLOAD_ROWS

    oversized = {"name": "orders", "rows": rows + [[MAX_UPLOAD_ROWS]]}
    error = upload_row_limit_error([oversized])
    assert error == "orders has too many data rows; maximum is 10000"
    assert len(oversized["rows"]) == MAX_UPLOAD_ROWS + 1


def test_decomposition_request_is_closed_bounded_and_fully_connected():
    proposal = {
        "subquestions": [
            {"id": "products", "question": "top 3 products by quantity"},
            {"id": "customers", "question": "top 2 customers by spend"},
        ],
        "merges": [
            {"id": "pairs", "op": "cross", "inputs": ["customers", "products"]},
        ],
        "output": "pairs",
        "grain": "one customer-product pair",
    }
    request = {
        "question": "promotion gaps", "tables": [],
        "analysis": {"action": "create", "slug": "promotion_gaps"},
        "decomposition": proposal,
    }
    normalized = validate_reason_request(request)
    # The validator returns JSON-safe normalized values.  Internal dependency
    # sets may use tuples, but the request boundary must remain list-shaped so
    # the normalized request can be serialized and sent back through the API.
    assert normalized["decomposition"]["merges"][0]["inputs"] == [
        "customers", "products",
    ]

    for bad in (
        {**proposal, "sql": "SELECT * FROM secrets"},
        {**proposal, "subquestions": proposal["subquestions"] + [
            {"id": "unused", "question": "unrelated totals"},
        ]},
        {**proposal, "merges": [
            {"id": "pairs", "op": "join", "inputs": ["customers", "products"]},
        ]},
    ):
        try:
            validate_reason_request({**request, "decomposition": bad})
            raise AssertionError("unsafe decomposition was accepted")
        except RequestValidationError:
            pass

    for body in (
        {"message": "x", "tables": [{"name": 0, "data": "a\n1"}]},
        {"message": "x", "tables": [{"name": "data", "data": 0}]},
    ):
        try:
            validate_chat_request(body)
            raise AssertionError("invalid chat table was accepted")
        except RequestValidationError:
            pass


def test_json_body_guard_rejects_bad_lengths_payloads_and_shapes():
    assert parse_content_length(None, 10) == 0
    assert parse_content_length("4", 10) == 4
    for value, status in (("-1", 400), ("abc", 400), ("11", 413)):
        try:
            parse_content_length(value, 10)
            raise AssertionError
        except JSONBodyError as exc:
            assert exc.status_code == status
    assert read_json_object(BytesIO(b'{"ok":true}'), "11", 20) == {"ok": True}
    for raw in (b"[1]", b"{bad"):
        try:
            read_json_object(BytesIO(raw), str(len(raw)), 20)
            raise AssertionError
        except JSONBodyError as exc:
            assert exc.status_code == 400


def test_shared_request_gate_releases_capacity_and_limits_rate():
    gate = RequestGate(requests=2, window_seconds=60, in_flight=1)
    lease, _, reason = gate.acquire("user")
    assert lease is not None and reason is None
    blocked, retry, reason = gate.acquire("other")
    assert blocked is None and retry == 1 and reason == "concurrency"
    lease.release(); lease.release()
    second, _, _ = gate.acquire("user")
    assert second is not None
    second.release()
    denied, retry, reason = gate.acquire("user")
    assert denied is None and retry > 0 and reason == "rate"


def test_distributed_paid_budget_is_atomic_and_releases_lease():
    class Connection:
        def __init__(self, usage=(), active=(0, 0)):
            self.usage, self.active = usage, active
            self.statements, self.commits, self.rollbacks, self.closed = [], 0, 0, False
            self.last = ""

        def cursor(self): return self
        def execute(self, statement, params=None):
            self.last = str(statement); self.statements.append((self.last, params))
        def fetchall(self): return list(self.usage)
        def fetchone(self): return self.active
        def commit(self): self.commits += 1
        def rollback(self): self.rollbacks += 1
        def close(self): self.closed = True

    acquired, released = Connection(), Connection()
    connections = iter((acquired, released))
    budget = PostgresRequestBudget(
        lambda: next(connections),
        {"generate": BudgetPolicy(2, 10, 1, 3)},
    )
    lease, retry, reason = budget.acquire("private-user-id", "generate")
    assert lease is not None and retry == 0 and reason is None and acquired.commits == 1
    parameters = repr([params for _, params in acquired.statements])
    assert "private-user-id" not in parameters and "__global__" in parameters
    assert any("period,bucket_start" in statement for statement, _ in acquired.statements)
    assert any(params and params[0] == "day" for _, params in acquired.statements)
    lease.release(); lease.release()
    assert released.commits == 1
    assert any("DELETE FROM chat.request_lease" in statement for statement, _ in released.statements)


TESTS = [
    test_sliding_window_limiter_is_bounded_and_expires,
    test_server_500_logs_where_it_failed_without_user_data,
    test_chat_authenticates_without_a_database_and_signs_with_the_firebase_uid,
    test_cors_requires_exact_configured_origin,
    test_auth_test_sub_is_ignored_outside_explicit_nonproduction,
    test_conversation_lifecycle_limits_are_bounded_and_configurable,
    test_admin_access_fails_closed_without_an_explicit_allowlist,
    test_postgres_connect_retries_transport_errors_but_not_authentication,
    test_cached_connection_dropped_between_requests_is_replaced_before_use,
    test_chat_validation_normalizes_and_bounds_inputs,
    test_a_long_conversation_keeps_its_most_recent_history_window,
    test_table_names_are_canonical_bounded_and_unique_at_every_boundary,
    test_reason_validation_rejects_unbounded_or_invalid_fields,
    test_uploaded_row_limit_rejects_without_truncating_at_the_public_boundary,
    test_decomposition_request_is_closed_bounded_and_fully_connected,
    test_json_body_guard_rejects_bad_lengths_payloads_and_shapes,
    test_shared_request_gate_releases_capacity_and_limits_rate,
    test_distributed_paid_budget_is_atomic_and_releases_lease,
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
    print(f"\nrequest limits: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
