"""trace.py — stream the reasoning trace + each view to Firebase RTDB (/runs/{uid}/{jobId}) so the browser
renders slides LIVE, decoupled from the 60s HTTP proxy timeout: the container keeps writing past it, and the
client subscribes to RTDB instead of waiting on the POST. Admin writes bypass the RTDB rules, and a client
reads only its OWN /runs/{uid} (database.rules.json, uid = the verified Firebase uid). Every write is
best-effort — streaming must NEVER break the answer (the endpoint still returns the full JSON as a fallback).

RTDB_URL is OPTIONAL: when it is unset the emitter functions become clean no-ops and the frontend falls back
to the full-JSON HTTP response.
"""
from __future__ import annotations

import datetime
import json
import time
from decimal import Decimal

from engine import request_timing
from engine.config import RTDB_URL, rtdb_trace_retention_days
from engine.numeric import wire_value

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
    return json.loads(json.dumps(value, default=_rtdb_scalar, allow_nan=False))


def ensure_app():
    """Idempotently ensure the default firebase-admin app exists. When RTDB_URL is set the app carries the
    databaseURL (RTDB needs it); without it the app is still initialized (ADC creds) so token verification
    works. Shared by the auth path (engine.auth) and the emitter, so the one app serves both."""
    import firebase_admin
    try:
        firebase_admin.get_app()
    except ValueError:
        opts = {"databaseURL": RTDB_URL} if RTDB_URL else None
        firebase_admin.initialize_app(options=opts)               # ADC creds (+ the RTDB url when configured)


def emitter(uid, job_id):
    """Return emit(node, value, merge=False) bound to /runs/{uid}/{job_id}; a NO-OP if RTDB_URL is unset,
    uid/job_id absent, or RTDB is unavailable, so callers can always `emit = emitter(...)` and call it
    unconditionally."""
    if not RTDB_URL or not uid or not job_id:
        return _NOOP
    base = f"runs/{uid}/{job_id}"
    try:
        ensure_app()
        from firebase_admin import db
    except Exception as e:                               # noqa: BLE001 — no RTDB -> just don't stream
        print(f"[trace] unavailable error={type(e).__name__}", flush=True)
        return _NOOP

    created_at = time.time()
    try:
        db.reference(base).update({
            "created_at": created_at,
            "expires_at": created_at + (rtdb_trace_retention_days() * 86400),
        })
    except Exception as e:                           # noqa: BLE001 — streaming is best-effort
        print(f"[trace] metadata_write_failed error={type(e).__name__}", flush=True)

    def emit(node, value, merge=False):
        # Each write is a SYNCHRONOUS network round trip on the serving thread. rtdb_ms is what makes
        # that cost visible, and it is the number that decides whether prose streaming can write
        # per-token or has to batch behind a background writer.
        try:
            with request_timing.span("rtdb"):            # the span publishes its own rtdb_n
                ref = db.reference(f"{base}/{node}" if node else base)
                (ref.update if merge else ref.set)(rtdb_safe(value))
        except Exception as e:                           # noqa: BLE001 — best-effort; never break the answer
            print(f"[trace] emit_failed error={type(e).__name__}", flush=True)
    return emit


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


def delete_traces(uid, conversation_id=None):
    """Delete one conversation's indexed traces, or every trace for the verified Firebase uid.

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
    if conversation_id is None:
        existing = base.get(shallow=True) or {}
        base.delete()
        return len(existing)
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
                emit("present", True)                         # real answer, human phrasing -> UI presents it via Sonnet
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

    def __init__(self, emit, node, interval=0.1):
        import threading
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
        """Stop the flusher; when `final` is given, write it synchronously as the authoritative value."""
        with self._lock:
            self._stop = True
            self._latest = None
        self._wake.set()
        self._thread.join(timeout=2)
        if final is not None:
            try:
                self._emit(self._node, final)
            except Exception:                            # noqa: BLE001
                pass


# --- per-request emit CONTEXT ------------------------------------------------------------------------------------
# Lets DEEP resolution code (the bridge build, several inheritance layers down) stream the cell→qid lookup LIVE
# without threading `emit` through every method signature. The server sets it INSIDE its request LOCK (one request
# per model at a time), so there's no cross-request race within a process.
_CTX = {"emit": None}


def set_ctx(emit):
    _CTX["emit"] = emit


def ctx_emit(node, value, merge=False):
    """Emit on the current request's stream, if any (a no-op otherwise). Best-effort — never breaks the answer."""
    e = _CTX["emit"]
    if e:
        try:
            e(node, value, merge)
        except Exception:                                # noqa: BLE001
            pass
