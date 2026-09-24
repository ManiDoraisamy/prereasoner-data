"""Durable Google Sheets add-on conversation selection and sidebar state.

The browser sidebar is disposable: Google may recreate its iframe whenever the document or add-on
is reopened.  This module binds one authenticated user's Google spreadsheet id to the conversation
that should be resumed, and stores the small renderable sidebar snapshot separately from the full
web workbook state kept on ``chat.conversation``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from engine import config
from engine.conversations import NotOwned, QuotaExceeded, source_snapshot_hash
from engine.pg import _pg


_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,256}$")
_CONVERSATION_ID_RE = re.compile(r"^c_[0-9a-f]{32}$")
MAX_SHEET_SESSIONS_PER_USER = 100
MAX_SIDEBAR_STATE_BYTES = 512 * 1024


def _spreadsheet_id(value) -> str:
    normalized = str(value or "").strip()
    if not _SPREADSHEET_ID_RE.fullmatch(normalized):
        raise ValueError("spreadsheet id is invalid")
    return normalized


def _expiry(now=None):
    current = now or datetime.now(timezone.utc)
    return current + timedelta(days=config.conversation_retention_days())


def _state_payload(state):
    if not isinstance(state, dict):
        raise ValueError("sidebar state must be an object")
    encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    size = len(encoded.encode("utf-8"))
    if size > MAX_SIDEBAR_STATE_BYTES:
        raise QuotaExceeded("sidebar state is too large")
    return encoded, size


def _decoded_state(value):
    if isinstance(value, dict) or value is None:
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _ensure_user(cur, user_id):
    cur.execute(
        'INSERT INTO "chat"."user_profile" (user_id) VALUES (%s) '
        'ON CONFLICT (user_id) DO UPDATE SET last_seen = now()',
        (user_id,),
    )


def _check_new_session_limit(cur, user_id):
    cur.execute('SELECT count(*) FROM "chat"."sheet_session" WHERE user_id = %s', (user_id,))
    if int(cur.fetchone()[0] or 0) >= MAX_SHEET_SESSIONS_PER_USER:
        raise QuotaExceeded("spreadsheet session limit reached")


def _session_host(value):
    host = str(value or "sheets").strip().lower()
    if host not in ("sheets", "excel"):
        raise ValueError("spreadsheet host is invalid")
    return host


def restore_sheet_session(user_id, spreadsheet_id, sheets, host="sheets"):
    """Return the current sidebar session for one sheet, creating a durable blank marker if absent.

    Existing deployments did not record spreadsheet ids.  On the first open after this feature is
    installed, an exact uploaded-source hash may bind the most recently active owned conversation.
    The heuristic runs only when no sheet-session row exists; an explicit New chat stores a null
    conversation marker and therefore never resurrects an older conversation on reload.
    """
    sid = _spreadsheet_id(spreadsheet_id)
    host = _session_host(host)
    current_hash = source_snapshot_hash(sheets)
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                        (f"prereasoner-sheet-session:{user_id}:{sid}",))
            _ensure_user(cur, user_id)
            cur.execute(
                'SELECT ss.conversation_id, ss.sidebar_state, c.initial_prompt, c.source_hash, '
                'c.dataset_version FROM "chat"."sheet_session" ss '
                'LEFT JOIN "chat"."conversation" c ON c.conversation_id = ss.conversation_id '
                'WHERE ss.user_id = %s AND ss.spreadsheet_id = %s AND ss.host = %s FOR UPDATE OF ss',
                (user_id, sid, host),
            )
            row = cur.fetchone()
            legacy = False
            if row is None and host == "sheets":
                cur.execute(
                    'SELECT c.conversation_id, c.initial_prompt, c.source_hash, c.dataset_version '
                    'FROM "chat"."conversation" c '
                    'JOIN "chat"."user_conversation" uc ON uc.conversation_id = c.conversation_id '
                    'WHERE uc.user_id = %s AND c.source_hash = %s AND c.expires_at > now() '
                    'ORDER BY c.last_active_at DESC, c.created_at DESC, c.conversation_id DESC LIMIT 1',
                    (user_id, current_hash),
                )
                candidate = cur.fetchone()
                _check_new_session_limit(cur, user_id)
                if candidate:
                    row = (candidate[0], None, candidate[1], candidate[2], candidate[3])
                    legacy = True
                else:
                    row = (None, None, "", "", 0)
                cur.execute(
                    'INSERT INTO "chat"."sheet_session" '
                    '(user_id, spreadsheet_id, host, conversation_id, expires_at) '
                    'VALUES (%s, %s, %s, %s, %s)',
                    (user_id, sid, host, row[0], _expiry()),
                )
            elif row is None:
                _check_new_session_limit(cur, user_id)
                row = (None, None, "", "", 0)
                cur.execute(
                    'INSERT INTO "chat"."sheet_session" '
                    '(user_id, spreadsheet_id, host, conversation_id, expires_at) '
                    'VALUES (%s, %s, %s, NULL, %s)',
                    (user_id, sid, host, _expiry()),
                )
            else:
                cur.execute(
                    'UPDATE "chat"."sheet_session" SET updated_at = now(), expires_at = %s '
                    'WHERE user_id = %s AND spreadsheet_id = %s AND host = %s',
                    (_expiry(), user_id, sid, host),
                )

            conversation_id, sidebar_state, question, stored_hash, dataset_version = row
            if conversation_id:
                cur.execute(
                    'UPDATE "chat"."conversation" SET last_active_at = now(), expires_at = %s '
                    'WHERE conversation_id = %s',
                    (_expiry(), conversation_id),
                )
            conn.commit()
            return {
                "conversation_id": conversation_id,
                "state": _decoded_state(sidebar_state),
                "question": question or "",
                "source_hash": stored_hash or "",
                "dataset_version": int(dataset_version or 0),
                "source_changed": bool(conversation_id and stored_hash and stored_hash != current_hash),
                "legacy": bool(conversation_id and (legacy or sidebar_state is None)),
                "host": host,
            }
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
    finally:
        conn.close()


def save_sheet_session(user_id, spreadsheet_id, conversation_id, state, host="sheets"):
    """Select an owned conversation for a sheet and atomically persist its renderable sidebar."""
    sid = _spreadsheet_id(spreadsheet_id)
    host = _session_host(host)
    cid = str(conversation_id or "")
    if not _CONVERSATION_ID_RE.fullmatch(cid):
        raise NotOwned("conversation not found")
    encoded, state_bytes = _state_payload(state)
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                        (f"prereasoner-sheet-session:{user_id}:{sid}",))
            _ensure_user(cur, user_id)
            cur.execute(
                'SELECT 1 FROM "chat"."user_conversation" '
                'WHERE user_id = %s AND conversation_id = %s',
                (user_id, cid),
            )
            if not cur.fetchone():
                raise NotOwned("conversation not found")
            cur.execute(
                'SELECT state_bytes FROM "chat"."sheet_session" '
                'WHERE user_id = %s AND spreadsheet_id = %s AND host = %s FOR UPDATE',
                (user_id, sid, host),
            )
            if cur.fetchone() is None:
                _check_new_session_limit(cur, user_id)
            cur.execute(
                'INSERT INTO "chat"."sheet_session" '
                '(user_id, spreadsheet_id, host, conversation_id, sidebar_state, state_bytes, expires_at) '
                'VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) '
                'ON CONFLICT (user_id, host, spreadsheet_id) DO UPDATE SET '
                'host = EXCLUDED.host, conversation_id = EXCLUDED.conversation_id, sidebar_state = EXCLUDED.sidebar_state, '
                'state_bytes = EXCLUDED.state_bytes, updated_at = now(), expires_at = EXCLUDED.expires_at',
                (user_id, sid, host, cid, encoded, state_bytes, _expiry()),
            )
            conn.commit()
            return {"saved": cid, "spreadsheet_id": sid}
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
    finally:
        conn.close()


def clear_sheet_session(user_id, spreadsheet_id, host="sheets"):
    """Persist an explicit blank sidebar so reload does not revive an older matching conversation."""
    sid = _spreadsheet_id(spreadsheet_id)
    host = _session_host(host)
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                        (f"prereasoner-sheet-session:{user_id}:{sid}",))
            _ensure_user(cur, user_id)
            cur.execute(
                'SELECT 1 FROM "chat"."sheet_session" WHERE user_id = %s AND spreadsheet_id = %s AND host = %s',
                (user_id, sid, host),
            )
            if cur.fetchone() is None:
                _check_new_session_limit(cur, user_id)
            cur.execute(
                'INSERT INTO "chat"."sheet_session" '
                '(user_id, spreadsheet_id, host, conversation_id, sidebar_state, state_bytes, expires_at) '
                'VALUES (%s, %s, %s, NULL, NULL, 0, %s) '
                'ON CONFLICT (user_id, host, spreadsheet_id) DO UPDATE SET '
                'host = EXCLUDED.host, conversation_id = NULL, sidebar_state = NULL, state_bytes = 0, '
                'updated_at = now(), expires_at = EXCLUDED.expires_at',
                (user_id, sid, host, _expiry()),
            )
            conn.commit()
            return {"cleared": sid}
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
    finally:
        conn.close()


def cleanup_expired_sheet_sessions(now=None):
    current = now or datetime.now(timezone.utc)
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute('DELETE FROM "chat"."sheet_session" WHERE expires_at <= %s', (current,))
        deleted = int(cur.rowcount or 0)
        conn.commit()
        return deleted
    finally:
        conn.close()
