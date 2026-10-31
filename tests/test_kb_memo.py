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


class _Connection:
    """A preparation's PostgreSQL connection (engine/prepared.py): records each statement, answers with
    ``answer(sql)``."""

    def __init__(self, calls, answer):
        self._calls, self._answer = calls, answer

    def cursor(self):
        connection = self

        class Cursor:
            rows = []

            def execute(self, sql, params=None):
                connection._calls.append((sql, params))
                self.rows = connection._answer(sql)

            def fetchall(self):
                return self.rows

        return Cursor()


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
    q._rconn = lambda: _Connection(calls, lambda sql: [] if "DISTINCT ON (norm)" in sql
                                   else [("Johns Hopkns Hospital", "Q1472068", 0.93, "johnshopkinshospital")])
    original = knowledge_query.Embedder
    knowledge_query.Embedder = type("E", (), {"get": staticmethod(lambda: _Embedder())})
    try:
        assert q._resolve_world_qids(["Johns Hopkns Hospital"], "hospital") == {
            "Johns Hopkns Hospital": "Q1472068"}
    finally:
        knowledge_query.Embedder = original
    sql = calls[-1][0]
    assert "ORDER BY (embedding <=> fv.vec) + 0" in sql and "embedding IS NOT NULL" in sql, sql
    assert calls[-1][1] == ("hospital",)


def test_a_nearest_name_matches_only_when_its_spelling_agrees():
    """The embedding alone matched names the knowledgebase does not hold to another entity: in a read-only
    sample of banks, universities and hospitals (2026-10-09), 557 of 1,200 such names matched a different
    entity at cosine 0.85, and 163 of 300 hospital names did in production. A nearest name now also has to be
    spelled alike: a typo still matches (all 735 sampled typo matches stayed), a different entity with a
    similar embedding does not, and a shortened name is disclosed as unmatched instead of counted as whatever
    is nearest ("Mayo Clinic" took "Mayo Clinic Health System")."""
    from engine.embeddings import normalize_surface
    from engine.knowledge_query import nearest_name_matches

    def matches(name, nearest, cosine):
        return nearest_name_matches(normalize_surface(name), normalize_surface(nearest), cosine)

    assert matches("Johns Hopkns Hospital", "Johns Hopkins Hospital", 0.93)          # a typo
    assert not matches("Bank of Kyoto", "Bank of Nagoya", 0.92)                     # another bank
    assert not matches("Mayo Clinic", "Mayo Clinic Health System", 0.911)            # a shortened name
    assert not matches("Johns Hopkns Hospital", "Johns Hopkins Hospital", 0.80)      # the embedding disagrees


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
    q._rconn = lambda: _Connection(calls, lambda _sql: [])
    original = knowledge_query.Embedder
    knowledge_query.Embedder = type("E", (), {"get": staticmethod(lambda: _Embedder())})
    try:
        assert q._resolve_world_qids(["St. Mary's Hospital"], "hospital") == {"St. Mary's Hospital": None}
    finally:
        knowledge_query.Embedder = original
    exact, nearest = calls[0][0], calls[1][0]
    assert "ORDER BY norm, is_primary IS TRUE DESC, length(qid), qid" in exact, exact
    assert "ORDER BY (embedding <=> fv.vec) + 0, is_primary IS TRUE DESC, length(qid), qid" in nearest, nearest


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
    caller gets its own records; a key that cannot be hashed computes afresh; outside a request nothing is kept."""
    from engine.relations import memoized, request_memo
    from engine.sql_schema import SchemaGraph
    from engine.tables import TableQuery
    calls = []

    def compute():
        calls.append(1)
        return ["result"]

    memoized("request kind", lambda: ("k",), compute, 1)
    memoized("request kind", lambda: ("k",), compute, 1)
    assert len(calls) == 2, "outside a request nothing is kept"
    with request_memo():
        first = memoized("request kind", lambda: ("k",), compute, 1)
        assert memoized("request kind", lambda: ("k",), compute, 1) is first and len(calls) == 3
        memoized("request other", lambda: ("k",), compute, 1)
        assert len(calls) == 4, "another derivation never collides"
        memoized("request kind", lambda: ([1],), compute, 1)
        memoized("request kind", lambda: ([1],), compute, 1)
        assert len(calls) == 6, "an unhashable key computes afresh"
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


def test_a_later_question_on_the_same_cells_derives_nothing_again():
    """Every question on a 500,000-cell workbook derived its keys, schema and graph again: 5.7 to 8.7 s on Cloud Run
    (2026-10-31). The process keeps them under the content itself: another request reuses them, a changed cell does
    not, and the bound and the time limit let them go."""
    from engine import relations
    from engine.relations import memoized, request_memo
    calls = []

    def compute():
        calls.append(1)
        return ["result"]

    def asked(key, cells=1):
        with request_memo():
            return memoized("kept kind", lambda: (key,), compute, cells)

    first = asked("cells")
    assert asked("cells") is first and len(calls) == 1, "a later request reuses an equal derivation"
    asked("a changed cell")
    assert len(calls) == 2, "other content derives its own"

    calls.clear()
    limit, seconds = relations.KEPT_CELLS, relations.KEPT_SECONDS
    try:
        relations.KEPT_CELLS = 10
        asked("too big", 11)
        asked("too big", 11)
        assert len(calls) == 2, "what is larger than the bound is not kept"
        calls.clear()
        relations._KEPT.clear()
        asked("oldest", 6)
        asked("newer", 4)
        asked("oldest", 6)
        assert len(calls) == 2, "a reuse is a use"
        asked("newest", 4)
        assert len(calls) == 3
        asked("oldest", 6)
        assert len(calls) == 3, "the least recently used goes first"
        asked("newer", 4)
        assert len(calls) == 4, "and it went"
        calls.clear()
        relations.KEPT_CELLS = limit
        relations._KEPT.clear()
        asked("idle")
        relations.KEPT_SECONDS = -1
        asked("idle")
        assert len(calls) == 2, "what has not been used for the time limit is gone"
    finally:
        relations.KEPT_CELLS, relations.KEPT_SECONDS = limit, seconds
        relations._KEPT.clear()


def test_a_graph_kept_for_a_request_serves_the_next():
    """Two requests with equal cells read one graph and one readers' schema, each with its own records."""
    from engine.readers.text import Schema
    from engine.relations import memoized, request_memo
    from engine.sql_schema import SchemaGraph

    def schema():
        return [{"table": "orders", "name": "city", "idx": 0, "affinity": "TEXT", "values": ["Paris", "Lyon"]}]

    def graph_of(column_values):
        return SchemaGraph.from_planner([dict(column, values=list(column_values)) for column in schema()], [])

    with request_memo():
        first = graph_of(["Paris", "Lyon"])
        read = memoized("readers_schema", lambda: first, lambda: Schema(first), 2)
    with request_memo():
        again = graph_of(["Paris", "Lyon"])
        reread = memoized("readers_schema", lambda: again, lambda: Schema(again), 2)
    with request_memo():
        other = graph_of(["Paris", "Lille"])
    assert again is first and reread is read, "the same cells"
    assert other is not first and other.column_map["orders", "city"].values == ("Paris", "Lille"), "a changed cell"


TESTS = [
    test_identical_lookup_executes_once_per_request,
    test_different_params_never_collide,
    test_new_request_re_executes,
    test_no_open_request_is_an_uncached_passthrough,
    test_list_params_hash_by_value,
    test_production_entry_opens_the_request,
    test_value_membership_routing_is_one_lookup_per_table,
    test_an_entity_resolves_to_the_exact_nearest_of_its_type,
    test_a_nearest_name_matches_only_when_its_spelling_agrees,
    test_a_shared_name_resolves_the_same_way_every_time,
    test_rows_whose_entity_matched_nothing_are_disclosed,
    test_a_request_derives_its_tables_once,
    test_a_later_question_on_the_same_cells_derives_nothing_again,
    test_a_graph_kept_for_a_request_serves_the_next,
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
