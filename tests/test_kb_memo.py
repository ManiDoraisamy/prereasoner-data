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


def test_an_entity_resolves_to_the_exact_nearest_of_its_type():
    """The 2026-10-02 engine build resolved "Mayo Clinic" on a fresh seed through the HNSW index with the
    hospital filter applied after the approximate scan: no hospital came back, the US hospitals totalled 32
    instead of 46, and every earlier build had found it. The fuzzy entity lookup sorts the exact distance
    over the rows of its type, which the planner cannot serve from the approximate index."""
    import numpy as np

    import engine.knowledge_query as knowledge_query

    class _Embedder:
        @staticmethod
        def encode(texts):
            return np.zeros((len(texts), 4), dtype=np.float32)

    calls = []
    q = knowledge_query.KnowledgeQuery.__new__(knowledge_query.KnowledgeQuery)
    q._kb_rows = lambda sql, params: calls.append((sql, params)) or (
        [("hospital",)] if "types" in sql else [] if "norm=%s" in sql else [("Q30280159", 0.91)])
    original = knowledge_query.Embedder
    knowledge_query.Embedder = type("E", (), {"get": staticmethod(lambda: _Embedder())})
    try:
        assert q._resolve_world_qid("Mayo Clinic", "hospital", "Q16917") == "Q30280159"
    finally:
        knowledge_query.Embedder = original
    sql = calls[-1][0]
    assert "ORDER BY (embedding <=> %s::vector) + 0" in sql and "embedding IS NOT NULL" in sql, sql
    assert calls[-1][1][1] == "hospital"


def test_a_shared_name_resolves_the_same_way_every_time():
    """87 hospital names named two QIDs in production (2026-10-02), and the exact lookup took whichever row
    came first: no ORDER BY. Both lookups now break a tie by the name's primary entity, then the lowest
    numeric QID (length first, so Q9 precedes Q10)."""
    import numpy as np

    import engine.knowledge_query as knowledge_query

    class _Embedder:
        @staticmethod
        def encode(texts):
            return np.zeros((len(texts), 4), dtype=np.float32)

    calls = []
    q = knowledge_query.KnowledgeQuery.__new__(knowledge_query.KnowledgeQuery)
    q._kb_rows = lambda sql, params: calls.append(sql) or ([("hospital",)] if "types" in sql else [])
    original = knowledge_query.Embedder
    knowledge_query.Embedder = type("E", (), {"get": staticmethod(lambda: _Embedder())})
    try:
        assert q._resolve_world_qid("St. Mary's Hospital", "hospital", "Q16917") is None
    finally:
        knowledge_query.Embedder = original
    exact, nearest = calls[1], calls[2]
    assert "norm=%s" in exact and "ORDER BY is_primary IS TRUE DESC, length(qid), qid LIMIT 1" in exact, exact
    assert "ORDER BY (embedding <=> %s::vector) + 0, is_primary IS TRUE DESC, length(qid), qid" in nearest, nearest


def test_rows_whose_entity_matched_nothing_are_disclosed():
    """A non-geo world total skipped every uploaded row whose entity resolved to nothing, so an unmatched US
    hospital lowered "total transfers to US hospitals" without a word (2026-10-02). The rows are reported
    with their names, counted only among the rows the uploaded-value filters keep, as the plan compares
    them (case-insensitively)."""
    from engine.knowledge_query import UNMATCHED_NAMES_SHOWN, unmatched_rows

    table = {"name": "transfers", "columns": ["hospital", "region", "transfers"], "rows": []}
    rows = [["Mayo Clinic", "North", 14], ["Xqzv Kpltr", "North", 3], ["Cleveland Clinic", "South", 12],
            ["Pqrw Hosp", "south", 5], ["Xqzv Kpltr", "North", 2]]
    matched = [True, False, True, False, False]
    report = unmatched_rows(table, "hospital", rows, matched, [], "hospital")
    assert report == {"table": "transfers", "column": "hospital", "entity": "hospital", "rows": 3, "of": 5,
                      "names": ["Xqzv Kpltr", "Pqrw Hosp"], "more": 0}, report
    # Contrastive: a filter on the upload keeps only its rows, compared as LOWER(cell) = LOWER(value).
    south = unmatched_rows(table, "hospital", rows, matched, [("transfers", "region", "South")], "hospital")
    assert (south["rows"], south["of"], south["names"]) == (1, 2, ["Pqrw Hosp"]), south
    # A filter on another sheet is not this table's.
    other = unmatched_rows(table, "hospital", rows, matched, [("regions", "region", "South")], "hospital")
    assert other["of"] == 5, other
    # Negative: every row matched, nothing to say.
    assert unmatched_rows(table, "hospital", rows, [True] * 5, [], "hospital") is None
    # The names are bounded; the rest are counted.
    many = [[f"Unknown {index}", "North", 1] for index in range(UNMATCHED_NAMES_SHOWN + 2)]
    bounded = unmatched_rows(table, "hospital", many, [False] * len(many), [], "hospital")
    assert len(bounded["names"]) == UNMATCHED_NAMES_SHOWN and bounded["more"] == 2, bounded


def test_a_request_derives_its_tables_once():
    """A 6-tab, 34,500-row workbook ran foreign-key discovery, the planner's schema and the schema graph up to
    four times a request (2026-10-04). Within `relations.request_memo` an equal derivation is reused and each
    caller gets its own records; a key that cannot be hashed computes afresh; a new request, or none, recomputes."""
    from engine.relations import memoized, request_memo
    from engine.sql_schema import SchemaGraph
    from engine.tables import TableQuery
    calls = []

    def compute():
        calls.append(1)
        return ["result"]

    memoized("kind", lambda: ("k",), compute)
    memoized("kind", lambda: ("k",), compute)
    assert len(calls) == 2, "outside a request nothing is kept"
    with request_memo():
        first = memoized("kind", lambda: ("k",), compute)
        assert memoized("kind", lambda: ("k",), compute) is first and len(calls) == 3
        memoized("other", lambda: ("k",), compute)
        assert len(calls) == 4, "another derivation never collides"
        memoized("kind", lambda: ([1],), compute)
        memoized("kind", lambda: ([1],), compute)
        assert len(calls) == 6, "an unhashable key computes afresh"
    with request_memo():
        memoized("kind", lambda: ("k",), compute)
        assert len(calls) == 7, "a new request recomputes"
    planner = TableQuery.__new__(TableQuery)
    planner.model = object()
    built = []

    def schema(tables, fks):
        built.append(1)
        return ([{"table": t["name"], "name": c, "idx": i, "affinity": "TEXT", "values": [row[i] for row in t["rows"]]}
                 for t in tables for i, c in enumerate(t["columns"])], {}, {t["name"]: t for t in tables})

    planner._schema = schema
    orders = {"name": "orders", "columns": ["city"], "rows": [["Paris"], ["Lyon"]]}
    with request_memo():
        sch, _, _ = planner.schema([orders], [])
        again, _, _ = planner.schema([dict(orders)], [])       # an equal table, another object
        assert len(built) == 1 and sch == again and sch[0] is not again[0], "one build, the caller's own records"
        assert SchemaGraph.from_planner(sch, []) is SchemaGraph.from_planner(again, []), "one graph a request"


TESTS = [
    test_identical_lookup_executes_once_per_request,
    test_different_params_never_collide,
    test_new_request_re_executes,
    test_no_open_request_is_an_uncached_passthrough,
    test_list_params_hash_by_value,
    test_production_entry_opens_the_request,
    test_value_membership_routing_is_one_lookup_per_table,
    test_an_entity_resolves_to_the_exact_nearest_of_its_type,
    test_a_shared_name_resolves_the_same_way_every_time,
    test_rows_whose_entity_matched_nothing_are_disclosed,
    test_a_request_derives_its_tables_once,
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
