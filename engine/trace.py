"""trace.py — stream the reasoning trace + each view to Firebase RTDB (/runs/{uid}/{jobId}) so the browser
renders slides LIVE, decoupled from the 60s HTTP proxy timeout: the container keeps writing past it, and the
client subscribes to RTDB instead of waiting on the POST. Admin writes bypass the RTDB rules, and a client
reads only its OWN /runs/{uid} (database.rules.json, uid = the verified Firebase uid). Every write is
best-effort — streaming must NEVER break the answer (the endpoint still returns the full JSON as a fallback).

RTDB_URL is OPTIONAL: when it is unset the emitter functions become clean no-ops and the frontend falls back
to the full-JSON HTTP response.
"""
from __future__ import annotations

from engine.config import RTDB_URL

_NOOP = lambda *a, **k: None


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
        print(f"[trace] RTDB unavailable, streaming disabled: {e}", flush=True)
        return _NOOP

    def emit(node, value, merge=False):
        try:
            ref = db.reference(f"{base}/{node}" if node else base)
            (ref.update if merge else ref.set)(value)
        except Exception as e:                           # noqa: BLE001 — best-effort; never break the answer
            print(f"[trace] emit({node!r}) failed: {e}", flush=True)
    return emit


def stream_final(emit, res):
    """Emit the TERMINAL state (clarify / error / result+done) so the client renders the answer even if the
    HTTP response 502s at the 60s proxy. The engine already streamed `status` + each `view` live during serve."""
    try:
        if not isinstance(res, dict):
            return
        if res.get("low_confidence"):
            emit("low_confidence", True); emit("status", "clarify")   # conversational (not a data query) -> in-chat fallback
        elif res.get("clarify"):
            emit("clarify", {k: res.get(k) for k in ("proposed", "bindings", "dropped", "original_sql")
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
