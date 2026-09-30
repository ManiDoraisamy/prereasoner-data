"""test_kb_memo.py — the request-scoped shared-knowledge memo must dedupe without changing answers.

Registered in tests/run_all.py. Hermetic: a counting stub stands in for the resolution connection, so
this runs without Postgres and pins the contract measured to matter — identical (sql, params) lookups
within one request execute ONCE, a new request re-executes, different params never collide, and with
no open request the helper is an uncached passthrough.
"""
from __future__ import annotations

import sys

from engine.entities import EntityQuery


class _CountingCursor:
    def __init__(self, log):
        self.log = log
        self._rows = []

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        # Deterministic fake rows derived from the params so distinct lookups return distinct data.
        self._rows = [("row-for", repr(params))]

    def fetchall(self):
        return list(self._rows)


class _CountingConn:
    closed = False

    def __init__(self):
        self.log = []

    def cursor(self):
        return _CountingCursor(self.log)


def _query():
    q = EntityQuery.__new__(EntityQuery)      # bypass __init__: no model load, no Postgres
    q._rcn = _CountingConn()
    q._nlp = None
    return q


def test_identical_lookup_executes_once_per_request():
    q = _query()
    q.begin_request()
    a = q._kb_rows("SELECT norm FROM knowledgebase.\"words\" WHERE norm = ANY(%s)", (["paris", "lyon"],))
    b = q._kb_rows("SELECT norm FROM knowledgebase.\"words\" WHERE norm = ANY(%s)", (["paris", "lyon"],))
    assert a == b, "a memo hit must return the same rows"
    assert len(q._rcn.log) == 1, f"identical lookup must execute once, executed {len(q._rcn.log)}"


def test_different_params_never_collide():
    q = _query()
    q.begin_request()
    a = q._kb_rows("SELECT 1 WHERE x = ANY(%s)", (["paris"],))
    b = q._kb_rows("SELECT 1 WHERE x = ANY(%s)", (["lyon"],))
    assert a != b, "different params must not share a memo slot"
    assert len(q._rcn.log) == 2


def test_new_request_re_executes():
    """The memo is REQUEST-scoped: shared knowledge changes via offline sync, so nothing may be
    reused across serve() calls."""
    q = _query()
    q.begin_request()
    q._kb_rows("SELECT 1", ())
    q.begin_request()
    q._kb_rows("SELECT 1", ())
    assert len(q._rcn.log) == 2, "a new request must re-execute, never reuse the previous memo"


def test_no_open_request_is_an_uncached_passthrough():
    q = _query()
    q._kb_rows("SELECT 1", ())
    q._kb_rows("SELECT 1", ())
    assert len(q._rcn.log) == 2, "outside a request every call must reach the database"


def test_list_params_hash_by_value():
    """The norm arrays are Python lists (unhashable); the key must be their VALUE, not identity."""
    q = _query()
    q.begin_request()
    q._kb_rows("SELECT 1 WHERE x = ANY(%s)", (list(["a", "b"]),))
    q._kb_rows("SELECT 1 WHERE x = ANY(%s)", (list(["a", "b"]),))   # a DIFFERENT list object, same value
    assert len(q._rcn.log) == 1, "equal-valued list params must hit the memo"


def test_production_entry_opens_the_request():
    """engine.knowledge.KnowledgeReasoner.serve must clear the memo per request — source-pinned so
    the call cannot be silently dropped in a refactor."""
    import pathlib
    src = pathlib.Path("engine/knowledge.py").read_text(encoding="utf-8")
    serve = src[src.index("def serve("):src.index("def _verify_calculations")]
    assert "begin_request()" in serve, "KnowledgeReasoner.serve must open the request memo"


def test_value_membership_routing_is_one_lookup_per_table():
    """Routing read the words index once per column: seven round trips for the customer-orders sheet on
    every world request (2026-09-30). One lookup must route every column exactly as before, and a value
    that names two types (Georgia) breaks the tie by type name, never by hash order."""
    import engine.entities as entities

    rows = {"london": {"city"}, "paris": {"city"}, "brussels": {"city"}, "france": {"country"},
            "belgium": {"country"}, "georgia": {"country", "state"}, "chad": {"country"}}
    calls = []
    q = EntityQuery.__new__(EntityQuery)
    q.words = {"city": {}, "country": {}, "u_s_state": {}}
    q._kb_rows = lambda sql, params: calls.append(params) or [
        (norm, kind) for norm in params[0] for kind in sorted(rows.get(norm, ()))]
    table = {"name": "t", "columns": ["city", "country", "note", "tie"],
             "rows": [["London", "France", "x", "Georgia"], ["Paris", "Belgium", "y", "Georgia"],
                      ["Brussels", "France", "z", "Georgia"]]}
    routes = q._value_membership_routes(table)
    assert len(calls) == 1, f"one lookup for every column, got {len(calls)}"
    assert routes[("t", "city")] == entities.TYPE_TO_FRIENDLY["city"], routes
    assert routes[("t", "country")] == entities.TYPE_TO_FRIENDLY["country"], routes
    assert ("t", "note") not in routes
    assert routes[("t", "tie")] == entities.TYPE_TO_FRIENDLY["country"], routes   # 'country' < 'state'


TESTS = [
    test_identical_lookup_executes_once_per_request,
    test_different_params_never_collide,
    test_new_request_re_executes,
    test_no_open_request_is_an_uncached_passthrough,
    test_list_params_hash_by_value,
    test_production_entry_opens_the_request,
    test_value_membership_routing_is_one_lookup_per_table,
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
