"""admin.py — admin-only dashboard backend.

Read: list users (+ their Google identity name/email/avatar, conversation counts, last seen) and conversations
(+ whether the data schema still exists, its size, table count). Write (DESTRUCTIVE): drop a conversation's
schema + metadata, delete a user (their conversations/schemas + profile, optionally the Firebase auth account),
and sweep orphan schemas (c_* schemas with no chat.conversation row).

Identity: chat.user_profile stores ONLY the user_id, which is the Google *sub* (not the firebase uid) — so the
name/email/photo come from Firebase Auth, looked up by the google.com PROVIDER identity (get_users with a
ProviderIdentifier), NOT by uid. Enrichment is best-effort: if auth is unconfigured/unpermitted it degrades to
showing the sub, and the rest of the dashboard still works.

Auth: gated to an explicit email allowlist (ADMIN_EMAILS; empty by default). NOT the normal user auth - a logged-in
non-admin gets 403. Every destructive op RE-VALIDATES the schema id shape (c_<32 hex>) before DROP SCHEMA, so
a bug or crafted id can never drop an arbitrary schema (knowledgebase/public/chat are never matched).
"""
from __future__ import annotations
import os
import re

from engine.pg import _pg

_ID_RE = re.compile(r"^c_[0-9a-f]{32}$")           # a conversation id == its data-schema name; fixed, safe shape


def _admins():
    raw = os.environ.get("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def _fb():
    """Ensure the firebase-admin app exists and return the auth module. Raises if firebase is unavailable."""
    import firebase_admin
    from firebase_admin import auth as fb_auth
    try:
        firebase_admin.get_app()
    except ValueError:
        from engine.trace import ensure_app
        ensure_app()
    return fb_auth


def verify_admin(token):
    """Verify the Firebase token AND require its email be in the admin allowlist. -> admin email | None.
    Honors the AUTH_TEST_SUB bypass for local testing (returns 'test-admin')."""
    from engine.config import auth_test_sub
    if auth_test_sub():
        return "test-admin"
    if not token:
        return None
    try:
        dec = _fb().verify_id_token(token)
    except Exception:                                # noqa: BLE001
        return None
    email = (dec.get("email") or "").lower()
    return email if (email and email in _admins()) else None


# ---------------- identity (Firebase Auth, keyed by the Google sub) ----------------
def _identities(subs):
    """Map Google subs -> {email, name, photo, firebase_uid} via Firebase Auth. Our user_id IS the Google sub,
    so we look up by the google.com provider identity (not by uid). Best-effort: a sub simply stays absent from
    the result if auth is unconfigured/unpermitted, so the dashboard still renders with just the sub."""
    subs = [s for s in dict.fromkeys(subs) if s]           # de-dup, drop blanks, preserve order
    if not subs:
        return {}
    from engine.config import auth_test_sub
    if auth_test_sub():                                    # local/test: no real auth backend
        return {}
    try:
        fb_auth = _fb()
    except Exception:                                      # noqa: BLE001
        return {}
    out = {}
    for i in range(0, len(subs), 100):                     # get_users caps at 100 identifiers per call
        chunk = subs[i:i + 100]
        try:
            res = fb_auth.get_users([fb_auth.ProviderIdentifier("google.com", s) for s in chunk])
        except Exception:                                  # noqa: BLE001 — perms/network: degrade to sub-only
            continue
        for u in res.users:
            g = next((p for p in (u.provider_data or []) if p.provider_id == "google.com"), None)
            sub = g.uid if g else None                     # the google.com provider uid == our stored user_id
            if not sub:
                continue
            out[sub] = {"email": u.email or (g.email if g else None),
                        "name": u.display_name or (g.display_name if g else None),
                        "photo": u.photo_url or (g.photo_url if g else None),
                        "firebase_uid": u.uid}
    return out


def _firebase_uid_for(fb_auth, sub):
    """Resolve our stored user_id (a Google sub) to the firebase uid that delete_user requires. Falls back to
    treating the id as a uid directly (for any non-Google account). -> uid | None."""
    try:
        res = fb_auth.get_users([fb_auth.ProviderIdentifier("google.com", sub)])
        if res.users:
            return res.users[0].uid
    except Exception:                                      # noqa: BLE001
        pass
    try:
        return fb_auth.get_user(sub).uid                   # sub may already be a firebase uid
    except Exception:                                      # noqa: BLE001
        return None


# ---------------- reads ----------------
def _schema_stats(cur):
    """{schema_name: (size_bytes, n_tables)} for every c_* conversation schema, in ONE query."""
    cur.execute(
        "SELECT n.nspname, COALESCE(SUM(pg_total_relation_size(c.oid)),0)::bigint, "
        "COUNT(c.oid) FILTER (WHERE c.relkind IN ('r','m','v')) "
        "FROM pg_namespace n LEFT JOIN pg_class c ON c.relnamespace = n.oid "
        r"WHERE n.nspname ~ '^c_[0-9a-f]{32}$' GROUP BY n.nspname")
    return {r[0]: (int(r[1]), int(r[2])) for r in cur.fetchall()}


def list_users():
    """Every user with their Google identity (name/email/photo), conversation count + timestamps, newest-active
    first. Identity is best-effort (see _identities); user_id (the Google sub) is always present."""
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT p.user_id, p.created_at, p.last_seen, COUNT(uc.conversation_id) AS n_conv '
            'FROM "chat"."user_profile" p '
            'LEFT JOIN "chat"."user_conversation" uc ON uc.user_id = p.user_id '
            'GROUP BY p.user_id, p.created_at, p.last_seen ORDER BY p.last_seen DESC')
        users = [{"user_id": r[0], "created_at": r[1].isoformat() if r[1] else None,
                  "last_seen": r[2].isoformat() if r[2] else None, "n_conversations": int(r[3])}
                 for r in cur.fetchall()]
    finally:
        conn.close()
    ident = _identities([u["user_id"] for u in users])
    for u in users:
        info = ident.get(u["user_id"], {})
        u["name"] = info.get("name")
        u["email"] = info.get("email")
        u["photo"] = info.get("photo")
        u["firebase_uid"] = info.get("firebase_uid")
    return users


def list_conversations(user_id=None):
    """Conversations (optionally for one user), each with schema-exists + size + table count."""
    conn = _pg()
    try:
        cur = conn.cursor()
        stats = _schema_stats(cur)
        q = ('SELECT c.conversation_id, uc.user_id, c.initial_prompt, c.created_at '
             'FROM "chat"."conversation" c '
             'LEFT JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id ')
        args = ()
        if user_id:
            q += 'WHERE uc.user_id = %s '
            args = (user_id,)
        q += 'ORDER BY c.created_at DESC'
        cur.execute(q, args)
        out = []
        for cid, uid, prompt, created in cur.fetchall():
            size, ntab = stats.get(cid, (0, 0))
            out.append({"conversation_id": cid, "user_id": uid, "initial_prompt": prompt,
                        "created_at": created.isoformat() if created else None,
                        "schema_exists": cid in stats, "size_bytes": size, "n_tables": ntab})
        return out
    finally:
        conn.close()


def list_orphans():
    """c_* data schemas with NO chat.conversation row — leaked schemas safe to sweep."""
    conn = _pg()
    try:
        cur = conn.cursor()
        stats = _schema_stats(cur)
        cur.execute('SELECT conversation_id FROM "chat"."conversation"')
        known = {r[0] for r in cur.fetchall()}
        return [{"schema": s, "size_bytes": stats[s][0], "n_tables": stats[s][1]}
                for s in stats if s not in known]
    finally:
        conn.close()


# ---------------- destructive ----------------
def _drop_schema(cur, cid):
    """Drop ONE conversation data schema — only after re-validating the id shape (never an arbitrary schema)."""
    if not _ID_RE.match(cid or ""):
        raise ValueError(f"refusing to drop non-conversation schema {cid!r}")
    cur.execute(f'DROP SCHEMA IF EXISTS "{cid}" CASCADE')       # cid is _ID_RE-validated -> safe to interpolate


def delete_conversation(cid):
    """Drop the data schema + delete its chat metadata (ownership link + conversation row)."""
    if not _ID_RE.match(cid or ""):
        raise ValueError("invalid conversation id")
    conn = _pg()
    try:
        cur = conn.cursor()
        _drop_schema(cur, cid)
        cur.execute('DELETE FROM "chat"."user_conversation" WHERE conversation_id = %s', (cid,))
        cur.execute('DELETE FROM "chat"."conversation" WHERE conversation_id = %s', (cid,))
        conn.commit()
        return {"deleted_conversation": cid}
    finally:
        conn.close()


def delete_user(user_id, also_auth=False):
    """Delete a user: drop every one of their conversation schemas, remove their metadata + profile, and
    (optionally) delete the Firebase auth account so the identity is fully gone."""
    conn = _pg()
    dropped = []
    try:
        cur = conn.cursor()
        cur.execute('SELECT conversation_id FROM "chat"."user_conversation" WHERE user_id = %s', (user_id,))
        cids = [r[0] for r in cur.fetchall()]
        for cid in cids:
            if _ID_RE.match(cid or ""):
                _drop_schema(cur, cid)
                dropped.append(cid)
            cur.execute('DELETE FROM "chat"."user_conversation" WHERE conversation_id = %s', (cid,))
            cur.execute('DELETE FROM "chat"."conversation" WHERE conversation_id = %s', (cid,))
        cur.execute('DELETE FROM "chat"."user_profile" WHERE user_id = %s', (user_id,))
        conn.commit()
    finally:
        conn.close()
    auth_deleted = False
    if also_auth:
        try:
            fb_auth = _fb()
            uid = _firebase_uid_for(fb_auth, user_id)          # user_id is the Google sub; delete_user needs the uid
            if not uid:
                raise ValueError("no Firebase auth account for this Google identity")
            fb_auth.delete_user(uid)
            auth_deleted = True
        except Exception as e:                                 # noqa: BLE001 — data is already gone; report the auth miss
            return {"deleted_user": user_id, "dropped_schemas": dropped, "auth_deleted": False,
                    "auth_error": f"{type(e).__name__}: {e}"}
    return {"deleted_user": user_id, "dropped_schemas": dropped, "auth_deleted": auth_deleted}


def delete_orphans():
    """Sweep every orphan c_* schema (no conversation row)."""
    orphans = [o["schema"] for o in list_orphans()]
    conn = _pg()
    try:
        cur = conn.cursor()
        for s in orphans:
            _drop_schema(cur, s)
        conn.commit()
    finally:
        conn.close()
    return {"dropped_schemas": orphans, "count": len(orphans)}
