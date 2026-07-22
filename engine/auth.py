"""auth.py — Firebase (Google) token verification, shared by /api/reason and /api/knowledge.

The per-user Postgres schema is ALWAYS the verified Google sub (never client-supplied), and the RTDB stream
key is the verified Firebase uid — a client cannot choose another user's schema OR another user's stream.
These helpers are security-critical and are kept exactly as they ran in production; only the module name and
the test-bypass env var (AUTH_TEST_SUB) changed.

Non-prod bypass: AUTH_TEST_SUB -> fixed sub, skips token verification (test-only).
"""
from __future__ import annotations
import re

from engine.config import auth_test_sub

_FB_AUTH = None


def _slug(name, i):
    s = re.sub(r"\.csv$", "", (name or "").strip(), flags=re.I)
    s = re.sub(r"[^0-9A-Za-z_]+", "_", s).strip("_").lower()
    return s or f"t{i}"


def _verify_principal(token):
    """Verify a Firebase ID token; return (schema_sub, firebase_uid) from ONE verify, or (None, None).
    schema_sub = the Google sub (the per-user Postgres schema — stable across devices/sessions).
    firebase_uid = dec['uid'] = the browser's auth.uid = the RTDB /runs/{uid} key the security rules gate on
    (auth.uid === $uid). Both are derived from the verified token — a client cannot choose another user's schema
    OR another user's RTDB stream."""
    test = auth_test_sub()
    if test:
        return test, test
    if not token:
        return None, None
    global _FB_AUTH
    if _FB_AUTH is None:
        import firebase_admin
        from firebase_admin import auth as fb_auth
        try:
            firebase_admin.get_app()
        except ValueError:
            from engine.trace import ensure_app          # ADC creds + the RTDB databaseURL (the trace stream)
            ensure_app()                                  # one app for both auth and the reasoning-trace stream
        _FB_AUTH = fb_auth
    try:
        dec = _FB_AUTH.verify_id_token(token)
    except Exception:                                    # noqa: BLE001
        return None, None
    ident = (dec.get("firebase") or {}).get("identities") or {}
    g = ident.get("google.com") or []
    uid = dec.get("uid")
    return (str(g[0]) if g else uid), uid


def _bearer(headers, body):
    h = headers.get("Authorization") or headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return (body or {}).get("idToken")
