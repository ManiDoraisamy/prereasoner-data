"""The PostgreSQL connections of the serving process.

PostgreSQL holds the conversations, the saved references and the shared knowledgebase. Questions run on the
request's SQLite database (engine/prepared.py), which reads the knowledgebase from here when it prepares a table
and when a question names a word or converts a currency.
"""
from __future__ import annotations

import re
import threading
import time
import weakref

import psycopg2

from engine import request_timing
from engine.config import (
    KB_PG_DB,
    KB_PG_HOST,
    KB_PG_PORT,
    KB_PG_SSLMODE,
    KB_PG_USER,
    kb_pg_password,
)
from engine.numeric import parse_decimal, wire_decimal

_CONNECT_ATTEMPTS = 3
_NON_RETRYABLE_CONNECT_ERRORS = (
    "password authentication failed",
    "no pg_hba.conf entry",
    "does not exist",
)

# Keep NUMERIC exact. JSON-safe integral values remain integers; a fractional decimal crosses the wire as a
# canonical string only when no binary float can represent it exactly.
import psycopg2.extensions


def _numeric_to_py(v, cur):
    if v is None:
        return None
    return wire_decimal(parse_decimal(v, enforce_input_bounds=False))


psycopg2.extensions.register_type(psycopg2.extensions.new_type((1700,), "NUMERIC2PY", _numeric_to_py))


_SLOW_SQL_MS = 150                                       # a single statement slower than this gets a fingerprint line
_REDACT_STRINGS = re.compile(r"'(?:[^']|'')*'")          # engine SQL inlines literals via qlit — NEVER log them
_REDACT_NUMBERS = re.compile(r"\b\d+(?:\.\d+)?\b")


def _sql_fingerprint(query):
    """A privacy-safe template of one statement: string literals and numbers become '?', whitespace
    collapses. What remains is the STRUCTURE (verbs, tables, columns) — enough to find the code
    path, nothing of the user's cells or question.

    execute_values sends a whole statement, literals included, as bytes. str() of bytes is its repr,
    whose escaped quotes the literal pattern never matched, so uploaded cells reached the log line
    (three production lines on 2026-09-24). Bytes are decoded first; any other statement object is
    named by its type only, because its repr is not SQL the patterns can redact."""
    if isinstance(query, (bytes, bytearray, memoryview)):
        text = bytes(query).decode("utf-8", "replace")
    elif isinstance(query, str):
        text = query
    else:
        return f"<{type(query).__name__}>"
    text = _REDACT_STRINGS.sub("?", text)
    text = _REDACT_NUMBERS.sub("?", text)
    return " ".join(text.split())[:140]


class _TimedCursor(psycopg2.extensions.cursor):
    """Cursor that bills every statement to the request's timing line.

    Installed as the cursor_factory in `_pg`, the ONE connection owner for every serving path, so
    the `sql_ms`/`sql_n` totals cover all statements — planner, world lookups, bridge writes and
    the uploaded-sheet load alike — without a call site having to opt in. Outside a request the
    span/count helpers are no-ops. A statement slower than _SLOW_SQL_MS additionally logs its
    redacted fingerprint, so a production tail (one slow shape among 40 fast ones) is attributable
    without attaching a profiler.
    """

    def execute(self, query, vars=None):
        from engine.request_deadline import remaining
        budget = remaining()
        # An aborted transaction accepts only a rollback: the timeout statement sent before
        # SQLAlchemy's ROLLBACK TO SAVEPOINT failed, so the rollback never ran and the Python
        # program's SQL retry failed on the broken transaction (2026-10-04). A lock waits as long
        # as any statement may: a 5-second cap answered two questions in one conversation with 500.
        aborted = self.connection.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_INERROR
        if budget is not None and not aborted and _timeout_stale(self.connection, budget):
            milliseconds = str(max(1, int(budget * 1000)))
            super().execute("SELECT set_config('statement_timeout', %s, false), "
                            "set_config('lock_timeout', %s, false)", (milliseconds, milliseconds))
            self.connection.timeout_set = (time.monotonic(), budget)
        started = time.perf_counter()
        with request_timing.span("sql"):                 # the span publishes its own sql_n
            result = super().execute(query, vars)
        if _reverts_settings(query):
            self.connection.timeout_set = None
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms >= _SLOW_SQL_MS and request_timing.request_id() is not None:
            print(f"[timing] slow_sql rid={request_timing.request_id()} ms={elapsed_ms:.0f} "
                  f"sql={_sql_fingerprint(query)}", flush=True)
        return result

    def executemany(self, query, vars_list):
        # One CALL, but psycopg2 sends one statement per parameter set. Counting the call would
        # under-report the round trips by exactly the factor that makes executemany slow, so the
        # extra statements are counted explicitly.
        rows = list(vars_list)
        with request_timing.span("sql"):
            result = super().executemany(query, rows)
        request_timing.count("sql_n", max(0, len(rows) - 1))   # the span already counted one
        return result


# The statement and lock timeouts a connection carries end a statement at the request's deadline. They are
# session settings, sent before a statement only when the connection has none from this request yet, or when
# the one it has could outlast the deadline by more than this share of the budget it was sent with (a timeout
# set with the budget then left is a fixed length, and the deadline draws nearer): 4.8 s of a 240 s budget.
# Sending it before every statement cost a round trip per statement (about 68 a question, 2026-10-26).
_TIMEOUT_SLACK = 0.02


def _reverts_settings(query):
    """A rollback, to a savepoint too, reverts the set_config calls made since: the timeout must be sent again
    (SQLAlchemy rolls back to a savepoint with a statement)."""
    if isinstance(query, bytes):
        query = query.decode("utf-8", "replace")
    return isinstance(query, str) and query[:64].lstrip().upper().startswith(("ROLLBACK", "ABORT"))


def _timeout_stale(connection, budget):
    sent = getattr(connection, "timeout_set", None)
    if sent is None:
        return True
    sent_at, sent_budget = sent
    # The timeout outlasts the deadline by the time elapsed since it was sent; a larger budget (another
    # request's, on a reused connection) is replaced too.
    return time.monotonic() - sent_at > _TIMEOUT_SLACK * sent_budget or budget > sent_budget


# Serving connections are pooled per process. Opening one cost about 35 ms, and a question opened 12 (pg_connect
# p50 0.41 s, 2026-10-26). close() hands a connection back reset: the transaction rolled back, then DISCARD ALL
# (search_path, set_config values such as the timeouts, session advisory locks, prepared statements, temporary
# tables), autocommit off. A connection idle longer than _POOL_CHECK_SECONDS is checked before reuse.
# Four idle per instance: a question holds one to three at a time, and four engine instances keep at most 16 of
# the 50 connections a db-g1-small accepts.
_POOL_IDLE = 4
_POOL_CHECK_SECONDS = 30.0


class _PooledConnection(psycopg2.extensions.connection):
    """A serving connection whose close() returns it to the process pool."""

    timeout_set = None
    idle = False                                          # in the pool: closed, as far as its last user knows
    cursors = None                                        # this lease's cursors, closed when it ends

    def close(self):
        if self.idle:
            return                                        # closed twice: it is already back in the pool
        if not self.closed and _POOL.give_back(self):
            return
        super().close()

    def cursor(self, *args, **kwargs):
        if self.idle:
            raise psycopg2.InterfaceError("connection already closed")
        cursor = super().cursor(*args, **kwargs)
        if self.cursors is None:
            self.cursors = weakref.WeakSet()
        self.cursors.add(cursor)
        return cursor

    def end_lease(self):
        """Close the cursors its last user opened: one kept past close() fails as on a closed connection,
        never running on the next user's lease."""
        for cursor in list(self.cursors or ()):
            try:
                cursor.close()
            except psycopg2.Error:
                pass
        self.cursors = None

    def rollback(self):
        self.timeout_set = None                           # the rollback reverts a timeout sent in its transaction
        super().rollback()


class _Pool:
    def __init__(self, size):
        self._size = size
        self._idle = []                                   # (connection, idle since)
        self._lock = threading.Lock()

    def take(self):
        while True:
            with self._lock:
                if not self._idle:
                    return None
                connection, since = self._idle.pop()
                connection.idle = False
            if connection.closed:
                continue
            if time.monotonic() - since > _POOL_CHECK_SECONDS:
                try:
                    with connection.cursor(cursor_factory=psycopg2.extensions.cursor) as cursor:
                        cursor.execute("SELECT 1")
                    connection.rollback()
                except psycopg2.Error:
                    psycopg2.extensions.connection.close(connection)
                    continue
            return connection

    def give_back(self, connection):
        connection.end_lease()
        try:
            connection.rollback()
            connection.autocommit = True
            with connection.cursor(cursor_factory=psycopg2.extensions.cursor) as cursor:
                cursor.execute("DISCARD ALL")
            connection.autocommit = False
        except psycopg2.Error:
            return False
        connection.timeout_set = None
        with self._lock:
            if len(self._idle) >= self._size:
                return False
            connection.idle = True
            self._idle.append((connection, time.monotonic()))
        return True


_POOL = _Pool(_POOL_IDLE)


def _pg():
    """A Postgres connection: a pooled one when the pool holds one, else a new one, retrying only transient
    transport failures.

    Authentication and database/role configuration errors fail immediately. The bounded retry is
    centralized here so request helpers do not each grow a different connection policy. Callers close()
    the connection as before; the pool takes it back (_PooledConnection).
    """
    pooled = _POOL.take()
    if pooled is not None:
        request_timing.count("pg_reused")
        return pooled
    kw = dict(host=KB_PG_HOST, dbname=KB_PG_DB, user=KB_PG_USER, password=kb_pg_password(),
              connect_timeout=30, cursor_factory=_TimedCursor, connection_factory=_PooledConnection)
    from engine.request_deadline import remaining
    if not KB_PG_HOST.startswith("/"):
        kw["port"] = KB_PG_PORT
        kw["sslmode"] = KB_PG_SSLMODE
    for attempt in range(_CONNECT_ATTEMPTS):
        # Each attempt waits only for what is left of the request's budget: the budget was read once,
        # so three attempts of a 2-second budget could each wait 2 seconds (release review, 2026-10-07).
        budget = remaining()                              # raises once the budget is spent
        if budget is not None:
            kw['connect_timeout'] = max(1, min(30, int(budget)))
        try:
            with request_timing.span("pg_connect"):      # the span publishes its own pg_connect_n
                return psycopg2.connect(**kw)
        except psycopg2.OperationalError as exc:
            message = str(exc).lower()
            if any(marker in message for marker in _NON_RETRYABLE_CONNECT_ERRORS):
                raise
            pause = 0.25 * (2 ** attempt)
            budget = remaining()
            if attempt + 1 == _CONNECT_ATTEMPTS or (budget is not None and budget <= pause):
                raise
            time.sleep(pause)
    raise AssertionError("unreachable")
