"""request_state.py — per-request attributes of the engine's shared serving objects.

Questions run on concurrent threads that share ONE planner object graph (engine/server.py MODEL): its
weights, tokenizer and caches are shared on purpose. A few attributes of those objects describe the
request being served instead: the conversation's Postgres schema, the connections the request holds,
the question's own text, the typing evidence it captures. Stored on the shared object, they were safe
only while a process-wide lock served one question at a time; with two questions at once, one would
read the other's schema and answer over another conversation's tables.

`RequestLocal` keeps such an attribute per request. Declared on the class, it stores each value in a
ContextVar, so `self._pg_schema = schema` and every later read see only the current request's value.
`request_scope()` gives one served request a fresh set of values (engine/knowledge.py
KnowledgeReasoner.serve) and drops them after it. Outside a scope (startup, tests) the values belong
to the current thread, as instance attributes did to the one thread that served.
"""
from __future__ import annotations

import contextlib
import contextvars
import weakref

_STATE: contextvars.ContextVar = contextvars.ContextVar("request_state", default=None)


def _values(create: bool):
    values = _STATE.get()
    if values is None and create:
        values = {}
        _STATE.set(values)
    return values


class RequestLocal:
    """A class attribute whose value belongs to the current request, not to the shared instance.

    Values are keyed by the instance's id and checked against a weak reference, so an object created
    where a freed one stood never reads the freed one's value."""

    def __init__(self, default=None):
        self.default = default
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        values = _values(create=False)
        entry = values.get((id(obj), self.name)) if values is not None else None
        if entry is None or entry[0]() is not obj:
            return self.default
        return entry[1]

    def __set__(self, obj, value):
        _values(create=True)[(id(obj), self.name)] = (weakref.ref(obj), value)

    def __delete__(self, obj):
        values = _values(create=False)
        if values is not None:
            values.pop((id(obj), self.name), None)


@contextlib.contextmanager
def request_scope():
    """Serve one request with its own request-local values."""
    token = _STATE.set({})
    try:
        yield
    finally:
        _STATE.reset(token)
