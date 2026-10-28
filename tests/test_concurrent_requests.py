"""test_concurrent_requests.py — questions run in parallel on the one shared engine.

The engine served one question at a time behind a process-wide lock and answered "busy" after 15 s;
the lock is gone (DECISIONS.md, "Questions run in parallel and the engine scales out"). Each test runs
two requests at once and checks what the lock used to guarantee: neither reads or writes the other's
state. Registered in tests/run_all.py. Hermetic: stubs stand in for the model, the database, the
tokenizer and firebase_admin. The live counterpart (two conversations at once on one planner) is
tests/test_geo.py section (F).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


def _run_together(*targets):
    """Run each target on its own thread, started together; re-raise the first failure."""
    errors = []

    def guarded(target):
        try:
            target()
        except BaseException as exc:                       # noqa: BLE001 — reported on the caller's thread
            errors.append(exc)

    threads = [threading.Thread(target=guarded, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads), "a request never finished"
    if errors:
        raise errors[0]


def test_two_questions_run_at_once_each_on_its_own_stream():
    """A second question waited behind the first on the lock and got 503 "busy" after 15 s. Two
    questions are now inside the model at once, and what deep code streams (the bridge build's
    cell→qid lookup) reaches its own request's trace node, never the other's."""
    import urllib.request
    from http.server import ThreadingHTTPServer

    from engine import server
    from engine.request_replay import DurableResponseReplay
    from engine.trace import ctx_emit
    from tests.test_request_limits import _RequestJobs

    meet = threading.Barrier(2, timeout=5)
    streams: dict[str, list] = {}

    class Model:
        def serve(self, tables, question, conversation, as_of, **_kwargs):
            meet.wait()                                    # returns only once both questions are inside
            ctx_emit("probe", question)
            return {"question": question, "result": {"columns": ["q"], "rows": [[question]]}, "error": None}

    def emitter(_uid, job):
        events = streams.setdefault(job, [])
        return lambda node, value, merge=False: events.append((node, value))

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address[:2]
    answers = {}

    def ask(principal, job, question):
        def run():
            body = json.dumps({"tables": [{"name": "orders", "data": "id,amount\n1,2\n"}],
                               "question": question, "jobId": job}).encode()
            request = urllib.request.Request(
                f"http://{host}:{port}/api/reason", data=body, method="POST",
                headers={"Authorization": "Bearer " + principal, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=20) as response:
                answers[job] = (response.status, json.loads(response.read())["result"]["rows"])
        return run

    try:
        with (
            patch.object(server, "_verify_principal", lambda token: (token, token)),
            patch.object(server, "resolve_conversation", lambda *_args: "c_" + "0" * 32),
            patch.object(server.master, "relevant_tables", lambda *_args: {"tables": [], "warnings": []}),
            patch.object(server, "emitter", emitter),
            patch.object(server, "MODEL", Model()),
            patch.object(server, "WORLD_REPLAY", DurableResponseReplay()),
            patch("engine.pg._pg", _RequestJobs().connect),
        ):
            _run_together(ask("sub-a", "job_a", "total amount in France"),
                          ask("sub-b", "job_b", "total amount in Spain"))
    finally:
        httpd.shutdown()
    assert answers == {"job_a": (200, [["total amount in France"]]),
                       "job_b": (200, [["total amount in Spain"]])}, answers
    assert ("probe", "total amount in France") in streams["job_a"], streams["job_a"]
    assert ("probe", "total amount in Spain") in streams["job_b"], streams["job_b"]
    assert ("probe", "total amount in Spain") not in streams["job_a"], "one request streamed into another's node"
    assert ("probe", "total amount in France") not in streams["job_b"], "one request streamed into another's node"
    print("  PASS  two questions run at once, each streaming to its own node")


def test_each_request_sees_only_its_own_planner_state():
    """The request's connections, its question text and its typing buffer lived on the one shared planner:
    with two questions at once, one computed over and closed the connection of the other's conversation. Each
    request now writes and reads its own, and holds its own database (engine/prepared.py): one question's
    tables and bridges never reach another's."""
    from engine.knowledge_query import KnowledgeQuery
    from engine.prepared import current_database, scratch_database
    from engine.request_state import request_scope

    qw = KnowledgeQuery.__new__(KnowledgeQuery)            # the shared planner; no weights needed
    names = ("_rcn", "_rcn_used", "_kb_memo", "_q_orig", "_q_meaning", "_typing_run")
    meet = threading.Barrier(2, timeout=5)
    seen = {}

    def request(tag):
        def run():
            with request_scope(), scratch_database() as database:
                for name in names:
                    setattr(qw, name, f"{tag}:{name}")
                database.load({"t": {"columns": ["tag"], "rows": [[tag]]}},
                              [{"table": "t", "name": "tag", "affinity": "TEXT"}])
                meet.wait()                                # the other request has written its own values
                rows = current_database().connection.execute("SELECT tag FROM t").fetchall()
                seen[tag] = ({name: getattr(qw, name) for name in names}, rows)
        return run

    _run_together(request("a"), request("b"))
    for tag in ("a", "b"):
        assert seen[tag] == ({name: f"{tag}:{name}" for name in names}, [(tag,)]), seen[tag]
    with request_scope():
        assert qw._rcn is None and qw._typing_run is None, "a new request starts empty"
    assert current_database() is None, "a request's database ends with it"
    print("  PASS  each request reads only the planner state and the database it wrote")


def test_a_request_local_value_is_never_read_through_a_reused_id():
    """Values are keyed by the instance's id: an object created where a freed one stood must not
    read the freed one's value."""
    import weakref

    from engine import request_state
    from engine.request_state import RequestLocal, request_scope

    class Holder:
        value = RequestLocal()

    with request_scope():
        fresh, freed = Holder(), Holder()
        stale = weakref.ref(freed)
        del freed
        # The freed object's value, stored under the id the fresh object now has.
        request_state._STATE.get()[(id(fresh), "value")] = (stale, "the freed object's value")
        assert fresh.value is None, "a new object read the value of the object freed before it"
    print("  PASS  a reused id never reads a freed object's value")


def test_a_request_closes_the_resolution_connection_it_opened():
    """The resolution connection was one per process and lived for hours. Each request now opens its
    own; the serve entry closes it when the request ends, also when the planner raises."""
    from engine import entities
    from engine.knowledge import KnowledgeReasoner
    from engine.request_state import request_scope

    class Connection:
        closed, autocommit = 0, False

        def close(self):
            self.closed = 1

    query = entities.EntityQuery.__new__(entities.EntityQuery)
    with request_scope(), patch.object(entities, "_pg", Connection):
        connection = query._rconn()
        assert query._rconn() is connection and connection.autocommit, "one connection serves the request"
        query.end_request()
        assert connection.closed and query._rcn is None

    import contextlib

    from engine import knowledge

    ended = []
    reasoner = KnowledgeReasoner.__new__(KnowledgeReasoner)
    reasoner.qw = SimpleNamespace(end_request=lambda: ended.append(True))

    def fails(*_args, **_kwargs):
        raise RuntimeError("the planner failed")

    reasoner._serve = fails
    try:
        with patch.object(knowledge, "request_database", contextlib.nullcontext):
            reasoner.serve([], "total amount", "c_" + "0" * 32)
    except RuntimeError:
        pass
    assert ended == [True], "a request that failed left its connection open"
    print("  PASS  a request closes the resolution connection it opened")


def test_two_questions_over_one_table_prepare_it_once():
    """Concurrent questions share the process's preparations (engine/prepared.py). Two questions over a table
    being prepared, as when a client asks before its sync's background preparation ends, wait for the one
    preparation in progress: neither types and matches the cells again."""
    from engine import prepared

    computed = []
    meet = threading.Barrier(2, timeout=5)

    class Resolver:
        def prepare_table(self, table):
            computed.append(table["name"])
            time.sleep(0.2)                                # the other question arrives meanwhile
            return {"routes": [[table["name"], "city", "city"]]}

    preparations = prepared.Preparations()
    table = {"name": "customers", "columns": ["city"], "rows": [["Paris"], ["Lyon"]]}
    got = {}

    def ask(tag):
        def run():
            meet.wait()
            got[tag] = preparations.get(Resolver(), table)
        return run

    with patch.object(prepared.Knowledge, "versions", lambda self: ("world", "entity")):
        _run_together(ask("a"), ask("b"))
        again = preparations.get(Resolver(), {"name": "customers", "columns": ["city"],
                                              "rows": [["Paris"], ["Lyon"]]})
    assert computed == ["customers"], f"prepared {len(computed)} times"
    assert got["a"] is got["b"] is again, "both questions and a later one read the one preparation"
    assert got["a"].routes == {("customers", "city"): "city"}
    print("  PASS  two questions over one table prepare it once")




class _CountingTokenizer:
    """Records how many calls are inside it at once. A real call keeps its truncation length as
    state: two at once at different lengths tokenize at each other's length."""

    def __init__(self):
        self.inside, self.most, self.lock = 0, 0, threading.Lock()

    def __call__(self, texts, **_kwargs):
        with self.lock:
            self.inside += 1
            self.most = max(self.most, self.inside)
        time.sleep(0.005)
        with self.lock:
            self.inside -= 1
        return {"input_ids": [[0] for _ in texts]}            # one token a text, unpadded


def test_the_shared_tokenizer_is_called_one_at_a_time():
    """One Qwen tokenizer serves TableQuery (48 tokens) and the schema interpreter (128). Two
    requests encoding at once never overlap inside it."""
    import torch

    from engine.encoder import LiveQwen
    from engine.tables import TableQuery

    tok = _CountingTokenizer()
    qwen = lambda **enc: SimpleNamespace(last_hidden_state=torch.zeros((1, enc["input_ids"].shape[1], 4)))  # noqa: E731
    cells = TableQuery.__new__(TableQuery)
    cells.tok, cells.qwen, cells.hdim, cells.device = tok, qwen, 4, torch.device("cpu")
    summaries = LiveQwen(torch.device("cpu"), shared_qwen=qwen, shared_tok=tok)
    meet = threading.Barrier(2, timeout=5)

    def encode_cells():
        meet.wait()
        for _ in range(20):
            cells._encode_batch(["city", "amount"])

    def encode_summaries():
        meet.wait()
        for _ in range(20):
            summaries.encode(["a table of orders"], max_len=128, grad=False, bs=1)

    _run_together(encode_cells, encode_summaries)
    assert tok.most == 1, f"{tok.most} tokenizer calls overlapped"
    print("  PASS  the shared tokenizer is called one at a time")


def test_another_requests_eviction_never_fails_a_cache_hit():
    """Concurrent questions share the encode cache. A hit was read, then moved to the end in a second
    step; another request's eviction between the two raised KeyError out of the encoder. Here that
    eviction is started exactly between them: it waits for the hit to finish."""
    from collections import OrderedDict

    from engine.tables import TableQuery

    class Encoder(TableQuery):
        _ENCODE_CACHE_CAP = 4

        def __init__(self):
            self.qwen, self.hdim = object(), 2

        def _encode_batch(self, texts):
            return np.array([[len(t), sum(map(ord, t))] for t in texts], np.float32)

    encoder = Encoder()
    evictors = []

    class Cache(OrderedDict):
        armed = False

        def move_to_end(self, key, last=True):
            if Cache.armed:
                Cache.armed = False
                # Another request encodes four new texts: the cache's four entries are evicted.
                evictor = threading.Thread(target=encoder._encode, args=(["e", "f", "g", "h"],))
                evictors.append(evictor)
                evictor.start()
                evictor.join(0.5)                          # it cannot evict while this hit holds the cache
            return OrderedDict.move_to_end(self, key, last)

    encoder.__dict__["_encode_cache"] = Cache()
    encoder._encode(["a", "b", "c", "d"])
    Cache.armed = True
    try:
        out = encoder._encode(["a"])
    finally:
        for evictor in evictors:
            evictor.join(5)
    assert np.array_equal(out[0], encoder._encode_batch(["a"])[0])
    assert list(encoder.__dict__["_encode_cache"]) == ["e", "f", "g", "h"], "the other request's texts are cached"
    print("  PASS  another request's eviction never fails a cache hit")


def test_a_finished_program_never_lifts_a_running_programs_escalation():
    """A generated program's one-to-one relationship that finds several rows must fail, not pick a
    row. Each run set that escalation inside warnings.catch_warnings(), which saves and restores the
    process-wide filter list: the first program to finish removed it under the one still running."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SAWarning

    from engine.deterministic.emitter.py import GeneratedPackage
    from engine.deterministic.runtime import execute_python

    first_inside, second_inside, first_done = threading.Event(), threading.Event(), threading.Event()
    source = (
        "import warnings\n"
        "from sqlalchemy.exc import SAWarning\n"
        "class Analysis:\n"
        "    def __init__(self, session, row_limit=None):\n"
        "        self.events = __generated_manifest__['events']\n"
        "    def run(self):\n"
        "        first_inside, second_inside, first_done = self.events\n"
        "        if __generated_manifest__['role'] == 'first':\n"
        "            first_inside.set()\n"
        "            second_inside.wait(5)\n"
        "            return 'first'\n"
        "        first_inside.wait(5)\n"
        "        second_inside.set()\n"
        "        first_done.wait(5)\n"
        "        warnings.warn('Multiple rows returned with uselist=False for eagerly-loaded attribute', SAWarning)\n"
        "        return 'second'\n"
    )

    def package(role):
        return GeneratedPackage(files={"base.py": "", "analysis.py": source},
                                manifest={"slug": "probe", "entrypoint_class": "Analysis",
                                          "entrypoint_method": "run", "role": role,
                                          "events": (first_inside, second_inside, first_done)})

    bind = create_engine("sqlite://")
    outcome = {}

    def first():
        outcome["first"] = execute_python(package("first"), bind)
        first_done.set()

    def second():
        first_inside.wait(5)                               # the first program is running before this one starts
        try:
            outcome["second"] = execute_python(package("second"), bind)
        except SAWarning:
            outcome["second"] = "escalated"

    _run_together(first, second)
    assert outcome == {"first": "first", "second": "escalated"}, outcome
    print("  PASS  a finished program never lifts a running program's escalation")


def test_the_firebase_app_is_initialized_once_under_concurrent_requests():
    """The first requests after a start each found no firebase app and initialized one; the second
    initialize_app raised "The default Firebase app already exists" out of that request's auth."""
    from engine import trace

    state = {"app": None, "created": 0}

    def get_app():
        if state["app"] is None:
            time.sleep(0.05)                               # the other request checks in this window
            raise ValueError("no app")
        return state["app"]

    def initialize_app(options=None):
        if state["app"] is not None:
            raise ValueError("The default Firebase app already exists.")
        state["created"] += 1
        state["app"] = object()
        return state["app"]

    fake = SimpleNamespace(get_app=get_app, initialize_app=initialize_app)
    meet = threading.Barrier(2, timeout=5)

    def request():
        meet.wait()
        trace.ensure_app()

    with patch.dict(sys.modules, {"firebase_admin": fake}):
        _run_together(request, request)
    assert state["created"] == 1, state
    print("  PASS  the firebase app is initialized once under concurrent requests")


TESTS = [
    test_two_questions_run_at_once_each_on_its_own_stream,
    test_each_request_sees_only_its_own_planner_state,
    test_a_request_local_value_is_never_read_through_a_reused_id,
    test_a_request_closes_the_resolution_connection_it_opened,
    test_two_questions_over_one_table_prepare_it_once,
    test_the_shared_tokenizer_is_called_one_at_a_time,
    test_another_requests_eviction_never_fails_a_cache_hit,
    test_a_finished_program_never_lifts_a_running_programs_escalation,
    test_the_firebase_app_is_initialized_once_under_concurrent_requests,
]


# The encoders' tensors need torch, which public CI does not install; the engine image does.
NEEDS_TORCH = {test_the_shared_tokenizer_is_called_one_at_a_time}


def main():
    import importlib.util

    failed, skipped = 0, 0
    for test in TESTS:
        if test in NEEDS_TORCH and importlib.util.find_spec("torch") is None:
            skipped += 1
            print(f"  SKIP  {test.__name__}: needs torch (the engine image installs it)")
            continue
        try:
            test()
        except Exception as exc:                           # noqa: BLE001 — report every test
            failed += 1
            print(f"  FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\ntest_concurrent_requests: {len(TESTS) - failed - skipped} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
