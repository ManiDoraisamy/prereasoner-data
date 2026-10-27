"""trace.py — stream the reasoning trace + each view to Firebase RTDB (/runs/{uid}/{jobId}) so the browser
renders slides LIVE, decoupled from the 60s HTTP proxy timeout: the container keeps writing past it, and the
client subscribes to RTDB instead of waiting on the POST. Admin writes bypass the RTDB rules, and a client
reads only its OWN /runs/{uid} (database.rules.json, uid = the verified Firebase uid). Every write is
best-effort — streaming must NEVER break the answer (the endpoint still returns the full JSON as a fallback).

RTDB_URL is OPTIONAL: when it is unset the emitter functions become clean no-ops and the frontend falls back
to the full-JSON HTTP response.
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime
import json
import threading
import time
from decimal import Decimal

from engine import request_timing
from engine.config import RTDB_URL, rtdb_trace_retention_days
from engine.numeric import wire_value, json_dumps

_NOOP = lambda *a, **k: None


def _rtdb_scalar(value):
    if isinstance(value, Decimal):
        return wire_value(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not RTDB serializable")


def rtdb_safe(value):
    """Normalize a trace through the same exact-scalar contract as the HTTP response.

    Firebase Admin serializes values independently of ``engine.server``. Saved reference and
    calculation rows can contain ``Decimal`` values, so a response could succeed while its live
    result write failed with ``TypeError`` and left the browser with a partial trace.
    """
    return json.loads(json_dumps(value, default=_rtdb_scalar, allow_nan=False))


def rtdb_encode(value):
    """Protect arrays from RTDB's removal of nulls and sparse-array coercion.

    HTTP and persisted snapshots keep ordinary JSON. The browser decodes this
    versioned transport envelope before using the same table validator. Encode
    complete arrays (including nested arrays) so empty/trailing-null rows survive.
    """
    def encode(item):
        if isinstance(item, list):
            return {"__pr_wire__": "array/v1", "json": json.dumps(item, allow_nan=False,
                                                                  separators=(",", ":"))}
        if isinstance(item, dict):
            return {key: encode(child) for key, child in item.items()}
        return item
    return encode(rtdb_safe(value))


_APP_LOCK = threading.Lock()                                     # requests run concurrently: one initializes the app


def ensure_app():
    """Idempotently ensure the default firebase-admin app exists. When RTDB_URL is set the app carries the
    databaseURL (RTDB needs it); without it the app is still initialized (ADC creds) so token verification
    works. Shared by the auth path (engine.auth) and the emitter, so the one app serves both."""
    import firebase_admin
    with _APP_LOCK:
        try:
            firebase_admin.get_app()
        except ValueError:
            opts = {"databaseURL": RTDB_URL} if RTDB_URL else None
            firebase_admin.initialize_app(options=opts)           # ADC creds (+ the RTDB url when configured)


# How long a response waits for its request's queued trace writes (drain_request): the terminal status
# lands in RTDB no later than the HTTP answer unless RTDB is this slow, and then it follows on its own.
TERMINAL_WAIT_SECONDS = 2.0

# The writers this request created, drained before its response is sent (drain_request). A context
# variable, so concurrent requests each drain only their own.
_WRITERS: contextvars.ContextVar = contextvars.ContextVar("trace_writers", default=None)

# A writer's thread ends once its queue has been empty this long, and a later write starts another: a writer
# nobody drains holds no thread.
WRITER_IDLE_SECONDS = 30.0


def _overlaps(path, other):
    return path.startswith(other + "/") or other.startswith(path + "/")


def write_batches(writes):
    """``writes`` ((node, encoded value, merge) in emit order) as RTDB multi-path updates applied in order.

    One update replaces each path it names and is atomic, so consecutive writes coalesce: a later write to the
    same path replaces the earlier one (the trace's status moves running -> done in one update), and a merge
    of a dict writes each of its keys as a path. RTDB refuses an update whose paths nest, so a write to a path
    nested in (or holding) one already in the batch starts the next batch."""
    batches, batch = [], {}
    for node, value, merge in writes:
        if merge and isinstance(value, dict):
            paths = {(f"{node}/{key}" if node else str(key)): child for key, child in value.items()}
        else:
            paths = {node: value}
        for path, child in paths.items():
            if any(_overlaps(path, other) for other in batch):
                batches.append(batch)
                batch = {}
            batch[path] = child
    if batch:
        batches.append(batch)
    return batches


class _Writer:
    """One request's trace writes, delivered in order by a background thread.

    Each RTDB write is a network round trip (13 per question on 2026-10-26, rtdb p50 0.65 s and p90 5.0 s on
    the serving thread). ``emit`` encodes the value at once (a snapshot the caller may go on mutating) and
    queues it; the thread sends everything queued as few multi-path updates as RTDB accepts (write_batches),
    so a burst of view writes costs one round trip. Best-effort like every trace write."""

    def __init__(self, base, reference):
        self._base = base
        self._reference = reference
        self._pending = []
        self._sending = False
        self._closed = False
        self._lock = threading.Condition()
        self._thread = None

    def __call__(self, node, value, merge=False):
        if not node and not (merge and isinstance(value, dict)):
            print("[trace] emit_failed error=JobNodeWrite", flush=True)      # writes go to nodes under the job
            return
        try:
            encoded = rtdb_encode(value)
        except Exception as e:                           # noqa: BLE001 — best-effort; never break the answer
            print(f"[trace] emit_failed error={type(e).__name__}", flush=True)
            return
        request_timing.count("rtdb_writes")
        with self._lock:
            self._pending.append((node, encoded, merge))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="trace-writer", daemon=True)
                self._thread.start()
            self._lock.notify_all()

    def _run(self):
        while True:
            with self._lock:
                idle_until = time.monotonic() + WRITER_IDLE_SECONDS
                while not self._pending and not self._closed and time.monotonic() < idle_until:
                    self._lock.wait(idle_until - time.monotonic())
                if not self._pending:
                    self._thread = None
                    return
                writes, self._pending, self._sending = self._pending, [], True
            try:
                for batch in write_batches(writes):
                    self._reference(self._base).update(batch)
            except Exception as e:                       # noqa: BLE001 — streaming is best-effort
                print(f"[trace] emit_failed error={type(e).__name__}", flush=True)
            finally:
                with self._lock:
                    self._sending = False
                    self._lock.notify_all()

    def drain(self, timeout):
        """Stop taking writes and wait up to ``timeout`` seconds for the queued ones; whether they all landed.
        Writes still queued then go out on their own."""
        deadline = time.monotonic() + timeout
        with self._lock:
            self._closed = True
            self._lock.notify_all()
            while self._pending or self._sending:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._lock.wait(left)
        return True


def drain_request(timeout=TERMINAL_WAIT_SECONDS):
    """Wait (bounded) for every trace write this request queued, before its response is sent."""
    writers = _WRITERS.get()
    if not writers:
        return
    _WRITERS.set(None)
    with request_timing.span("rtdb"):
        deadline = time.monotonic() + timeout
        for writer in writers:
            if not writer.drain(max(0.0, deadline - time.monotonic())):
                print("[trace] drain_timeout", flush=True)


def emitter(uid, job_id):
    """Return emit(node, value, merge=False) bound to /runs/{uid}/{job_id}; a NO-OP if RTDB_URL is unset,
    uid/job_id absent, or RTDB is unavailable, so callers can always `emit = emitter(...)` and call it
    unconditionally. Writes are queued and delivered in order off the serving thread (_Writer); the server
    waits for them before it responds (drain_request)."""
    if not RTDB_URL or not uid or not job_id:
        return _NOOP
    base = f"runs/{uid}/{job_id}"
    try:
        ensure_app()
        from firebase_admin import db
    except Exception as e:                               # noqa: BLE001 — no RTDB -> just don't stream
        print(f"[trace] unavailable error={type(e).__name__}", flush=True)
        return _NOOP

    writer = _Writer(base, db.reference)
    _WRITERS.set([*(_WRITERS.get() or ()), writer])
    created_at = time.time()
    writer("", {"created_at": created_at,
                "expires_at": created_at + (rtdb_trace_retention_days() * 86400)}, merge=True)
    return writer


def cleanup_expired_traces(uid=None, *, now=None, max_jobs=2000):
    """Delete RTDB trace jobs past their explicit expiry timestamp.

    The cleanup job runs with Firebase Admin credentials.  ``shallow=True`` keeps the
    scan bounded to keys; individual metadata reads avoid downloading trace payloads.
    """
    if not RTDB_URL:
        return 0
    ensure_app()
    from firebase_admin import db
    now = time.time() if now is None else float(now)
    root = db.reference("runs")
    user_keys = [str(uid)] if uid else list((root.get(shallow=True) or {}).keys())
    deleted = 0
    for user_id in user_keys:
        user_ref = root.child(user_id)
        job_keys = list((user_ref.get(shallow=True) or {}).keys())
        for job_id in job_keys[:max_jobs - deleted]:
            job_ref = user_ref.child(str(job_id))
            expiry = job_ref.child("expires_at").get()
            if expiry is None:
                created = job_ref.child("created_at").get()
                expiry = (float(created) + rtdb_trace_retention_days() * 86400
                          if created is not None else None)
            try:
                expired = expiry is not None and float(expiry) <= now
            except (TypeError, ValueError):
                expired = False
            if expired:
                job_ref.delete()
                deleted += 1
        if deleted >= max_jobs:
            break
    return deleted


def delete_traces(uid, conversation_id):
    """Delete one conversation's indexed traces for the verified Firebase uid. "Clear all" in the web app
    deletes conversations one at a time, each with its traces; a trace indexed to no conversation expires.

    Unlike streaming writes, deletion is not best-effort: callers must not report a successful
    privacy deletion while a configured RTDB store still retains the matching records.
    """
    if not RTDB_URL:
        return 0
    if not uid:
        raise ValueError("verified Firebase uid is required to delete RTDB traces")
    ensure_app()
    from firebase_admin import db
    base = db.reference(f"runs/{uid}")
    matches = base.order_by_child("conversation_id").equal_to(conversation_id).get() or {}
    for job_id in matches:
        base.child(str(job_id)).delete()
    return len(matches)


def stream_final(emit, res):
    """Emit the TERMINAL state (clarify / error / result+done) so the client renders the answer even if the
    HTTP response 502s at the 60s proxy. The engine already streamed `status` + each `view` live during serve."""
    try:
        if not isinstance(res, dict):
            return
        # Views can arrive after the Hosting proxy has already abandoned the HTTP
        # response. Stream the authoritative backend before the terminal status so
        # the workbook never has to infer Python-versus-SQL from source availability.
        if res.get("execution") is not None:
            emit("execution", res["execution"])
        if res.get("low_confidence"):
            emit("low_confidence", True); emit("status", "clarify")   # conversational (not a data query) -> in-chat fallback
        elif res.get("clarify"):
            emit("clarify", {k: res.get(k) for k in (
                "proposed", "bindings", "dropped", "original_sql", "reason", "unmet",
                "calculations", "currency",
            )
                             if res.get(k) is not None})
            emit("status", "clarify")
        elif res.get("error"):
            emit("error", str(res.get("error"))); emit("status", "error")
        else:
            if res.get("result"):
                emit("result", res["result"])
            if res.get("present"):
                emit("present", True)                         # UI uses the shared deterministic renderer
            emit("status", "done")
    except Exception:                                    # noqa: BLE001 — streaming is best-effort
        pass


class StreamBuffer:
    """Coalesce a growing text into bounded RTDB writes on a background thread.

    Built for streaming LLM prose: per-token writes through `emit` would put one ~40ms synchronous
    network round trip on the serving path PER TOKEN (measured rtdb_ms 328-540 for just 10-15 writes),
    so the producer calls `update(text_so_far)` as often as it likes — it only stores the latest value —
    and the flusher writes AT MOST once per `interval`. Writes are FULL-STATE (the whole text so far),
    which makes them idempotent and self-healing: a reconnecting subscriber reads the node's current
    value and is caught up, no sequence numbers to replay. `close(final)` writes the authoritative
    final text synchronously and stops the thread; it is the ONE write callers may rely on.
    Best-effort like every trace write — a failed flush never breaks the answer."""

    # How long close() waits for a flush already in flight. A flush that lands after the caller's
    # authoritative write leaves the node holding a prefix; the RTDB client's own timeout bounds it.
    DRAIN_SECONDS = 30

    def __init__(self, emit, node, interval=0.1):
        self._emit = emit
        self._node = node
        self._interval = interval
        self._lock = threading.Lock()
        self._latest = None                              # newest unflushed text (None = nothing pending)
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name=f"stream-{node}", daemon=True)
        self._thread.start()

    def update(self, text):
        """Record the newest text-so-far. Non-blocking; coalesces with any unflushed value."""
        with self._lock:
            self._latest = text
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait()
            with self._lock:
                if self._stop:
                    return
                text, self._latest = self._latest, None
                self._wake.clear()
            if text is not None:
                try:
                    self._emit(self._node, text)
                except Exception:                        # noqa: BLE001 — streaming is best-effort
                    pass
            time.sleep(self._interval)                   # rate limit BETWEEN flushes, not before the first

    def close(self, final=None):
        """Stop the flusher; when `final` is given, write it synchronously as the authoritative value.

        A flush already in flight is waited for first: writes are full-state, so one that lands after
        the authoritative write wins. A first chat reply was saved as "Your rest" when its first
        flush outlasted a 2 s wait (Chrome gate, 2026-10-01)."""
        with self._lock:
            self._stop = True
            self._latest = None
        self._wake.set()
        self._thread.join(timeout=self.DRAIN_SECONDS)
        if final is not None:
            try:
                self._emit(self._node, final)
            except Exception:                            # noqa: BLE001
                pass


# --- per-request emit CONTEXT ------------------------------------------------------------------------------------
# Lets DEEP resolution code (the bridge build, several inheritance layers down) stream the cell→qid lookup LIVE
# without threading `emit` through every method signature. Questions run concurrently, so the emitter is the
# request's own (a ContextVar), never a process-wide slot that another request could point at its stream.
_EMIT: contextvars.ContextVar = contextvars.ContextVar("trace_emit", default=None)


@contextlib.contextmanager
def request_stream(emit):
    """Send `ctx_emit` calls made while serving one request to that request's ``emit``."""
    token = _EMIT.set(emit)
    try:
        yield
    finally:
        _EMIT.reset(token)


def ctx_emit(node, value, merge=False):
    """Emit on the current request's stream, if any (a no-op otherwise). Best-effort — never breaks the answer."""
    e = _EMIT.get()
    if e:
        try:
            e(node, value, merge)
        except Exception:                                # noqa: BLE001
            pass
