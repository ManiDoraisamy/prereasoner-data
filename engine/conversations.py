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
  conversation(conversation_id PK, source/state bytes, activity/expiry timestamps)
  user_conversation(user_id, conversation_id, created_at)  -- ownership link (PK both)
conversation_id doubles as the name of that conversation's data schema (validated `c_<32 hex>`).
"""
from __future__ import annotations
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from engine import config
from engine.pg import _pg

# conversation_id is also a Postgres schema name — keep it a safe, fixed-shape identifier.
_ID_RE = re.compile(r"^c_[0-9a-f]{32}$")
MAX_STATE_BYTES = 1 * 1024 * 1024
MAX_PAGE_SIZE = 100


class NotOwned(Exception):
    """The conversation does not exist, or is not owned by this user (we do not distinguish — no enumeration)."""


class QuotaExceeded(ValueError):
    """The authenticated user has reached a bounded durable-storage quota."""


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


def _encoded_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _expiry(now=None):
    current = now or datetime.now(timezone.utc)
    return current + timedelta(days=config.conversation_retention_days())


def _user_metadata_bytes(cur, user_id) -> int:
    cur.execute('SELECT COALESCE(SUM(c.source_bytes + c.state_bytes), 0) '
                'FROM "chat"."conversation" c '
                'JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id '
                'WHERE uc.user_id = %s', (user_id,))
    return int(cur.fetchone()[0] or 0)


def _check_storage(cur, user_id, *, previous=0, replacement=0):
    projected = _user_metadata_bytes(cur, user_id) - int(previous) + int(replacement)
    if projected > config.max_conversation_storage_bytes():
        raise QuotaExceeded("conversation storage limit reached")


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
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                        (f"prereasoner-conversation-quota:{user_id}",))
            stored_tables = _store_tables(sheets)
            source_bytes = _encoded_size(stored_tables)
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
                    cur.execute('SELECT source_bytes FROM "chat"."conversation" WHERE conversation_id = %s',
                                (conversation_id,))
                    previous = int(cur.fetchone()[0] or 0)
                    _check_storage(cur, user_id, previous=previous, replacement=source_bytes)
                    cur.execute('UPDATE "chat"."conversation" SET tables = %s, source_bytes = %s, '
                                'last_active_at = now(), expires_at = %s WHERE conversation_id = %s',
                                (json.dumps(stored_tables), source_bytes, _expiry(), conversation_id))
                else:
                    cur.execute('UPDATE "chat"."conversation" SET last_active_at = now(), expires_at = %s '
                                'WHERE conversation_id = %s', (_expiry(), conversation_id))
                conn.commit()
                return conversation_id
            cur.execute('SELECT count(*) FROM "chat"."user_conversation" WHERE user_id = %s', (user_id,))
            if int(cur.fetchone()[0]) >= config.max_conversations_per_user():
                raise QuotaExceeded("conversation limit reached")
            _check_storage(cur, user_id, replacement=source_bytes)
            cid = _new_id()
            cur.execute('INSERT INTO "chat"."conversation" '
                        '(conversation_id, initial_prompt, tables, source_bytes, expires_at) '
                        'VALUES (%s, %s, %s, %s, %s)',
                        (cid, (initial_prompt or "")[:2000], json.dumps(stored_tables), source_bytes, _expiry()))
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


def load_dataset_ops(conversation_id):
    """The conversation's append-only dataset-semantics op log (engine/dataset_semantics.py).

    Called AFTER resolve_conversation authorized the id, so ownership is already settled. A
    pre-migration database (no dataset_ops column) reads as an empty log rather than an error —
    the feature simply stays inert until chat migration v4 runs."""
    conn = _pg()
    try:
        cur = conn.cursor()
        try:
            cur.execute('SELECT dataset_ops FROM "chat"."conversation" WHERE conversation_id = %s',
                        (conversation_id,))
        except Exception:                                    # noqa: BLE001 — column absent pre-migration
            conn.rollback()
            return []
        row = cur.fetchone()
        ops = row[0] if row else None
        return ops if isinstance(ops, list) else []
    finally:
        conn.close()


def append_dataset_ops(conversation_id, new_ops):
    """Append validated ops to the conversation's log and return the FULL updated log.

    Append-only by construction: the log is the audit history (a later `set` supersedes an earlier
    one at REPLAY time, in dataset_semantics.effective — never by rewriting the stored list)."""
    if not new_ops:
        return load_dataset_ops(conversation_id)
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute(
            'UPDATE "chat"."conversation" SET dataset_ops = '
            "COALESCE(dataset_ops, '[]'::jsonb) || %s::jsonb "
            'WHERE conversation_id = %s RETURNING dataset_ops',
            (json.dumps(new_ops), conversation_id))
        row = cur.fetchone()
        conn.commit()
        return row[0] if row and isinstance(row[0], list) else list(new_ops)
    finally:
        conn.close()


def conversation_page(user_id, limit=50, before=None):
    """One cursor page of the user's conversations, newest first."""
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    if before:
        try:
            before_time, before_id = str(before).rsplit("|", 1)
            before = datetime.fromisoformat(before_time.replace("Z", "+00:00"))
            if not _ID_RE.fullmatch(before_id):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError("before cursor is invalid") from exc
    conn = _pg()
    try:
        cur = conn.cursor()
        where = 'WHERE uc.user_id = %s '
        params = [user_id]
        if before:
            where += 'AND (c.created_at, c.conversation_id) < (%s, %s) '
            params.extend((before, before_id))
        params.append(limit + 1)
        cur.execute('SELECT c.conversation_id, c.initial_prompt, c.created_at '
                    'FROM "chat"."conversation" c '
                    'JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id '
                    f'{where}ORDER BY c.created_at DESC, c.conversation_id DESC LIMIT %s', params)
        rows = cur.fetchall()
        conn.commit()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [{"id": r[0], "question": r[1] or "", "ts": r[2].isoformat() if r[2] else ""}
                 for r in rows]
        cursor = (f'{items[-1]["ts"]}|{items[-1]["id"]}' if has_more and items else None)
        return {"conversations": items, "next_cursor": cursor}
    finally:
        conn.close()


def list_conversations(user_id, limit=50):
    return conversation_page(user_id, limit)["conversations"]


def delete_conversation(user_id, conversation_id, *, rtdb_uid=None):
    """Delete a conversation the user OWNS: its metadata + its data schema. Ownership-checked (no IDOR);
    conversation_id is validated to the strict c_<32 hex> shape before it reaches SQL/DROP SCHEMA."""
    if not _ID_RE.match(conversation_id or ""):
        raise NotOwned("bad conversation id")
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            cur.execute('SELECT c.source_bytes, c.state_bytes FROM "chat"."conversation" c '
                        'JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id '
                        'WHERE c.conversation_id = %s AND uc.user_id = %s',
                        (conversation_id, user_id))
            owned = cur.fetchone()
            if not owned:
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
        if row:
            cur.execute('UPDATE "chat"."conversation" SET last_active_at = now(), expires_at = %s '
                        'WHERE conversation_id = %s', (_expiry(), conversation_id))
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
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                        (f"prereasoner-conversation-quota:{user_id}",))
            cur.execute('SELECT c.source_bytes, c.state_bytes FROM "chat"."conversation" c '
                        'JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id '
                        'WHERE c.conversation_id = %s AND uc.user_id = %s',
                        (conversation_id, user_id))
            owned = cur.fetchone()
            if not owned:
                raise NotOwned("conversation not found")       # not yours OR absent
            encoded = json.dumps(state)
            state_bytes = len(encoded.encode("utf-8"))
            if state_bytes > MAX_STATE_BYTES:
                raise QuotaExceeded("conversation state is too large")
            _check_storage(cur, user_id, previous=int(owned[1] or 0), replacement=state_bytes)
            cur.execute('UPDATE "chat"."conversation" SET state = %s, state_bytes = %s, '
                        'last_active_at = now(), expires_at = %s WHERE conversation_id = %s',
                        (encoded, state_bytes, _expiry(), conversation_id))
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


def cleanup_expired_conversations(*, limit=500, now=None) -> int:
    """Delete expired conversation schemas and metadata in one bounded transaction."""
    limit = max(1, min(int(limit), 5000))
    current = now or datetime.now(timezone.utc)
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            cur.execute('SELECT conversation_id FROM "chat"."conversation" '
                        'WHERE expires_at <= %s ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED',
                        (current, limit))
            ids = [row[0] for row in cur.fetchall() if _ID_RE.fullmatch(row[0] or "")]
            for cid in ids:
                cur.execute('DELETE FROM "chat"."user_conversation" WHERE conversation_id = %s', (cid,))
                cur.execute('DELETE FROM "chat"."conversation" WHERE conversation_id = %s', (cid,))
                cur.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % cid)
            conn.commit()
            return len(ids)
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
