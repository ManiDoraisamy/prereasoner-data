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


TESTS = [
    test_identical_lookup_executes_once_per_request,
    test_different_params_never_collide,
    test_new_request_re_executes,
    test_no_open_request_is_an_uncached_passthrough,
    test_list_params_hash_by_value,
    test_production_entry_opens_the_request,
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
