"""conversations.py — conversation identity + the `chat` metadata schema.

The base schema is created by ``db/init.sql`` and upgraded by the privileged
``python -m db.sync.app_migrations`` command. Request handling only performs the
authorized DML below; it never creates or alters shared application tables.

The Postgres WORKING schema for a run is the CONVERSATION id (not the user), so a conversation's
uploaded tables and derived data are self-contained in one schema — inspectable, and archivable
to GCS as a unit (see db/sync/archive_conversation.py).

Security (the load-bearing part): the working schema is NEVER taken from the client on trust.
The user id comes from the verified Firebase token (engine.auth); a conversation id from the
client is ACCEPTED only after we confirm, in `chat.user_conversation`, that it belongs to that
user. Passing another user's conversation id fails the ownership check (no IDOR). A brand-new
conversation id is minted server-side.

The `chat` schema (in the same `world` database):
  user_profile(user_id PK, created_at, last_seen)          -- the Google identity (verified sub)
  conversation(conversation_id PK, initial_prompt, tables jsonb, created_at)
  user_conversation(user_id, conversation_id, created_at)  -- ownership link (PK both)
conversation_id doubles as the name of that conversation's data schema (validated `c_<32 hex>`).
"""
from __future__ import annotations
import json
import re
import uuid

from engine.pg import _pg

# conversation_id is also a Postgres schema name — keep it a safe, fixed-shape identifier.
_ID_RE = re.compile(r"^c_[0-9a-f]{32}$")


class NotOwned(Exception):
    """The conversation does not exist, or is not owned by this user (we do not distinguish — no enumeration)."""


def _new_id():
    return "c_" + uuid.uuid4().hex


def _store_tables(sheets):
    """The uploaded CSVs (name+data) kept so a conversation re-opens with its source in a fresh
    browser session, and so a GCS-archived schema can be re-hydrated end-to-end."""
    out = []
    for s in (sheets or []):
        if isinstance(s, dict) and (s.get("data") or "").strip():
            out.append({"name": s.get("name") or "table", "data": s["data"]})
    return out[:8]


def resolve_conversation(user_id, conversation_id, initial_prompt, sheets):
    """Return the conversation id to use as the working schema. Upserts the user profile.
    If `conversation_id` is given it MUST belong to `user_id` (else NotOwned). Otherwise a new
    conversation is minted, owned by `user_id`, storing the opening prompt + the uploaded tables.
    `user_id` is always the server-verified token subject — never client-supplied."""
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            cur.execute('INSERT INTO "chat"."user_profile" (user_id) VALUES (%s) '
                        'ON CONFLICT (user_id) DO UPDATE SET last_seen = now()', (user_id,))
            if conversation_id:
                if not _ID_RE.match(conversation_id):
                    raise NotOwned("bad conversation id")
                cur.execute('SELECT 1 FROM "chat"."user_conversation" '
                            'WHERE conversation_id = %s AND user_id = %s', (conversation_id, user_id))
                if not cur.fetchone():
                    raise NotOwned("conversation not found")     # not yours OR absent — same answer
                # Keep the stored source tables in step with the schema this run rebuilds, so a later
                # re-open (get_conversation) — and a GCS archive — never diverges from the live data.
                if sheets:
                    cur.execute('UPDATE "chat"."conversation" SET tables = %s WHERE conversation_id = %s',
                                (json.dumps(_store_tables(sheets)), conversation_id))
                conn.commit()
                return conversation_id
            cid = _new_id()
            cur.execute('INSERT INTO "chat"."conversation" (conversation_id, initial_prompt, tables) '
                        'VALUES (%s, %s, %s)',
                        (cid, (initial_prompt or "")[:2000], json.dumps(_store_tables(sheets))))
            cur.execute('INSERT INTO "chat"."user_conversation" (user_id, conversation_id) VALUES (%s, %s)',
                        (user_id, cid))
            conn.commit()
            return cid
        except Exception:                                    # never leave a half-written / aborted transaction
            try:
                conn.rollback()
            except Exception:                                # noqa: BLE001
                pass
            raise
    finally:
        conn.close()


def list_conversations(user_id, limit=50):
    """The user's conversations, newest first — for the drawer. Ownership-scoped by the join."""
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute('SELECT c.conversation_id, c.initial_prompt, c.created_at '
                    'FROM "chat"."conversation" c '
                    'JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id '
                    'WHERE uc.user_id = %s ORDER BY c.created_at DESC LIMIT %s', (user_id, int(limit)))
        rows = cur.fetchall()
        conn.commit()
        return [{"id": r[0], "question": r[1] or "", "ts": r[2].isoformat() if r[2] else ""} for r in rows]
    finally:
        conn.close()


def delete_conversation(user_id, conversation_id, *, rtdb_uid=None):
    """Delete a conversation the user OWNS: its metadata + its data schema. Ownership-checked (no IDOR);
    conversation_id is validated to the strict c_<32 hex> shape before it reaches SQL/DROP SCHEMA."""
    if not _ID_RE.match(conversation_id or ""):
        raise NotOwned("bad conversation id")
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            cur.execute('SELECT 1 FROM "chat"."user_conversation" WHERE conversation_id = %s AND user_id = %s',
                        (conversation_id, user_id))
            if not cur.fetchone():
                raise NotOwned("conversation not found")       # not yours OR absent — same answer
            from engine.trace import delete_traces
            trace_count = delete_traces(rtdb_uid, conversation_id)
            cur.execute('DELETE FROM "chat"."user_conversation" WHERE conversation_id = %s AND user_id = %s',
                        (conversation_id, user_id))
            cur.execute('DELETE FROM "chat"."conversation" WHERE conversation_id = %s', (conversation_id,))
            cur.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % conversation_id)   # validated c_<32hex> above
            conn.commit()
            return {"deleted": conversation_id, "deleted_traces": trace_count}
        except Exception:
            try:
                conn.rollback()
            except Exception:                                  # noqa: BLE001
                pass
            raise
    finally:
        conn.close()


def delete_all_conversations(user_id, *, rtdb_uid=None):
    """Delete ALL of a user's conversations (bulk cleanup). Only touches rows owned by user_id."""
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            cur.execute('SELECT conversation_id FROM "chat"."user_conversation" WHERE user_id = %s', (user_id,))
            ids = [r[0] for r in cur.fetchall() if _ID_RE.match(r[0] or "")]
            from engine.trace import delete_traces
            trace_count = delete_traces(rtdb_uid)
            for cid in ids:
                cur.execute('DELETE FROM "chat"."user_conversation" WHERE conversation_id = %s AND user_id = %s', (cid, user_id))
                cur.execute('DELETE FROM "chat"."conversation" WHERE conversation_id = %s', (cid,))
                cur.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % cid)
            conn.commit()
            return {"deleted": len(ids), "deleted_traces": trace_count}
        except Exception:
            try:
                conn.rollback()
            except Exception:                                  # noqa: BLE001
                pass
            raise
    finally:
        conn.close()


def get_conversation(user_id, conversation_id):
    """One conversation's opening prompt + stored tables + the last renderable snapshot (state), for
    re-opening it — after the same ownership check. Raises NotOwned if it isn't this user's conversation.
    `state` lets the client RESTORE what the user saw (turns, derived sheets, result) instead of re-running."""
    if not _ID_RE.match(conversation_id or ""):
        raise NotOwned("bad conversation id")
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute('SELECT c.initial_prompt, c.tables, c.state FROM "chat"."conversation" c '
                    'JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id '
                    'WHERE uc.user_id = %s AND c.conversation_id = %s', (user_id, conversation_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            raise NotOwned("conversation not found")
        return {"conversation_id": conversation_id, "question": row[0] or "", "tables": row[1] or [],
                "state": row[2] or None}
    finally:
        conn.close()


def save_state(user_id, conversation_id, state):
    """Persist a RENDERABLE snapshot of the conversation (the client's own turns + derived sheets + result +
    history) so a reload restores what the user saw instead of re-running the model. Ownership-checked; the
    snapshot is opaque display JSON — it is NEVER used as SQL, an identifier, or a schema name."""
    if not _ID_RE.match(conversation_id or ""):
        raise NotOwned("bad conversation id")
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            cur.execute('SELECT 1 FROM "chat"."user_conversation" WHERE conversation_id = %s AND user_id = %s',
                        (conversation_id, user_id))
            if not cur.fetchone():
                raise NotOwned("conversation not found")       # not yours OR absent
            cur.execute('UPDATE "chat"."conversation" SET state = %s WHERE conversation_id = %s',
                        (json.dumps(state), conversation_id))
            conn.commit()
            return {"saved": conversation_id}
        except Exception:
            try:
                conn.rollback()
            except Exception:                                  # noqa: BLE001
                pass
            raise
    finally:
        conn.close()
