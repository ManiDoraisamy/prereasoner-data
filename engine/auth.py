"""Firebase token verification and stable storage-principal resolution.

The Postgres owner is derived once from verified identity claims and then kept immutable;
existing Google users retain their Google subject. The RTDB trace key remains the verified Firebase UID.
Neither owner key is client-selectable.

Non-prod bypass: AUTH_TEST_SUB -> fixed sub, skips token verification (test-only).
"""
from __future__ import annotations
from engine.config import auth_test_sub

_FB_AUTH = None


def _verify_principal(token):
    """Verify a Firebase ID token; return (storage_principal, firebase_uid) or (None, None).
    Existing Google users retain their Google subject as the storage principal; users without a Google
    identity use a server-recorded Firebase UID mapping. RTDB remains keyed by the verified Firebase UID."""
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
    if not uid:
        return None, None
    return _storage_principal(str(uid), str(g[0]) if g else None), str(uid)


def _storage_principal(firebase_uid, google_sub):
    """Keep database ownership stable when a user adds or removes a sign-in provider.

    Existing Google accounts keep their historical Google subject. New accounts without a Google
    identity use their Firebase UID. Once recorded, the mapping is immutable for that Firebase UID.
    RTDB remains keyed by the verified Firebase UID, independently of this database subject.
    """
    from engine.pg import _pg

    fallback = google_sub or firebase_uid
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO "chat"."auth_principal" (firebase_uid, principal_id) '
            'VALUES (%s, %s) ON CONFLICT (firebase_uid) DO NOTHING',
            (firebase_uid, fallback),
        )
        cur.execute(
            'SELECT principal_id FROM "chat"."auth_principal" WHERE firebase_uid = %s',
            (firebase_uid,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            raise RuntimeError("account principal mapping is unavailable")
        conn.commit()
        return str(row[0])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _bearer(headers, body):
    h = headers.get("Authorization") or headers.get("authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return (body or {}).get("idToken")
