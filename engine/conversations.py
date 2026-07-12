"""conversations.py — conversation identity + the `chat` metadata schema.

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

_CHAT_DDL = (
    'CREATE SCHEMA IF NOT EXISTS "chat"',
    'CREATE TABLE IF NOT EXISTS "chat"."user_profile" ('
    ' user_id text PRIMARY KEY,'
    ' created_at timestamptz NOT NULL DEFAULT now(),'
    ' last_seen timestamptz NOT NULL DEFAULT now())',
    'CREATE TABLE IF NOT EXISTS "chat"."conversation" ('
    ' conversation_id text PRIMARY KEY,'
    ' initial_prompt text,'
    ' tables jsonb,'
    ' created_at timestamptz NOT NULL DEFAULT now())',
    'CREATE TABLE IF NOT EXISTS "chat"."user_conversation" ('
    ' user_id text NOT NULL REFERENCES "chat"."user_profile"(user_id),'
    ' conversation_id text NOT NULL REFERENCES "chat"."conversation"(conversation_id),'
    ' created_at timestamptz NOT NULL DEFAULT now(),'
    ' PRIMARY KEY (user_id, conversation_id))',
    'CREATE INDEX IF NOT EXISTS ix_user_conv ON "chat"."user_conversation" (user_id, created_at DESC)',
)

# conversation_id is also a Postgres schema name — keep it a safe, fixed-shape identifier.
_ID_RE = re.compile(r"^c_[0-9a-f]{32}$")


class NotOwned(Exception):
    """The conversation does not exist, or is not owned by this user (we do not distinguish — no enumeration)."""


def _ensure(cur):
    for ddl in _CHAT_DDL:
        cur.execute(ddl)


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
        cur = conn.cursor()
        _ensure(cur)
        cur.execute('INSERT INTO "chat"."user_profile" (user_id) VALUES (%s) '
                    'ON CONFLICT (user_id) DO UPDATE SET last_seen = now()', (user_id,))
        if conversation_id:
            if not _ID_RE.match(conversation_id):
                raise NotOwned("bad conversation id")
            cur.execute('SELECT 1 FROM "chat"."user_conversation" '
                        'WHERE conversation_id = %s AND user_id = %s', (conversation_id, user_id))
            if not cur.fetchone():
                raise NotOwned("conversation not found")     # not yours OR absent — same answer
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
    finally:
        conn.close()


def list_conversations(user_id, limit=50):
    """The user's conversations, newest first — for the drawer. Ownership-scoped by the join."""
    conn = _pg()
    try:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute('SELECT c.conversation_id, c.initial_prompt, c.created_at '
                    'FROM "chat"."conversation" c '
                    'JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id '
                    'WHERE uc.user_id = %s ORDER BY c.created_at DESC LIMIT %s', (user_id, int(limit)))
        rows = cur.fetchall()
        conn.commit()
        return [{"id": r[0], "question": r[1] or "", "ts": r[2].isoformat() if r[2] else ""} for r in rows]
    finally:
        conn.close()


def get_conversation(user_id, conversation_id):
    """One conversation's opening prompt + stored tables, for re-opening it — after the same
    ownership check. Raises NotOwned if it isn't this user's conversation."""
    if not _ID_RE.match(conversation_id or ""):
        raise NotOwned("bad conversation id")
    conn = _pg()
    try:
        cur = conn.cursor()
        _ensure(cur)
        cur.execute('SELECT c.initial_prompt, c.tables FROM "chat"."conversation" c '
                    'JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id '
                    'WHERE uc.user_id = %s AND c.conversation_id = %s', (user_id, conversation_id))
        row = cur.fetchone()
        conn.commit()
        if not row:
            raise NotOwned("conversation not found")
        return {"conversation_id": conversation_id, "question": row[0] or "", "tables": row[1] or []}
    finally:
        conn.close()
