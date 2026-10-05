"""Ownership-scoped durable retry records, shared across serving instances.

A finished response is kept for RETENTION only: long enough for the chat service to ask again for a
response it lost, and no longer, because it holds the user's result rows. It was kept for a day, and
deleting a conversation did not remove it (2026-10-04). Expired records are never replayed; each
finished request removes its user's expired records, and deleting a conversation removes them all
(`engine/conversations.py`)."""
from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar

RETENTION = "10 minutes"


class ReplayConflict(ValueError):
    """A request ID was reused for a different payload."""


class DurableResponseReplay:
    def __init__(self):
        self._owner = ContextVar('replay_owner', default=None)

    def claim(self, key, fingerprint=None):
        from engine.pg import _pg
        from engine.request_deadline import remaining
        owner = uuid.uuid4().hex
        while True:
            remaining()
            conn = _pg()
            try:
                cur = conn.cursor()
                cur.execute('INSERT INTO chat.request_job(route,subject_key,job_id,payload_hash,lease_owner,lease_until) '
                            "VALUES (%s,%s,%s,%s,%s,now()+interval '250 seconds') ON CONFLICT DO NOTHING",
                            (*key, fingerprint, owner))
                cur.execute('SELECT payload_hash, lease_owner, lease_until > now(), response, expires_at < now() '
                            'FROM chat.request_job WHERE route=%s AND subject_key=%s AND job_id=%s FOR UPDATE', key)
                digest, held_by, active, response, expired = cur.fetchone()
                if expired and not active:
                    # A finished record past its retention is gone: this request runs anew.
                    cur.execute("UPDATE chat.request_job SET payload_hash=%s, response=NULL, lease_owner=%s, "
                                "lease_until=now()+interval '250 seconds', "
                                f"expires_at=now()+interval '{RETENTION}' "
                                'WHERE route=%s AND subject_key=%s AND job_id=%s', (fingerprint, owner, *key))
                    conn.commit()
                    self._owner.set((key, owner))
                    return True, None
                if digest != fingerprint:
                    raise ReplayConflict('This request ID belongs to a different input. Start a new request.')
                if response is not None:
                    conn.commit()
                    return False, tuple(response)
                if held_by == owner or not active:
                    cur.execute("UPDATE chat.request_job SET lease_owner=%s, lease_until=now()+interval '250 seconds' "
                                'WHERE route=%s AND subject_key=%s AND job_id=%s', (owner, *key))
                    conn.commit()
                    self._owner.set((key, owner))
                    return True, None
                conn.commit()
            finally:
                conn.close()
            # Waiting callers never hold a database transaction.
            time.sleep(0.25)

    def finish(self, key, response):
        from engine.pg import _pg
        from engine import request_deadline
        token = request_deadline.begin(10)
        conn = None
        try:
            conn = _pg()
            cur = conn.cursor()
            # Transient transport/budget failures may be retried with the same input.
            body = None if response[0] in {429, 500, 503} else json.dumps(response)
            cur.execute('UPDATE chat.request_job SET response=%s::jsonb, lease_until=now(), '
                        f"expires_at=now()+interval '{RETENTION}' WHERE route=%s AND subject_key=%s AND job_id=%s "
                        'AND lease_owner=%s', (body, *key,
                         self._owner.get()[1] if self._owner.get() and self._owner.get()[0] == key else None))
            cur.execute('DELETE FROM chat.request_job WHERE subject_key=%s AND expires_at < now() '
                        'AND lease_until < now()', (key[1],))
            conn.commit()
            self._owner.set(None)
        finally:
            if conn is not None:
                conn.close()
            request_deadline.end(token)

    def release(self, key):
        self.finish(key, (503, '', 'application/json', None))


def delete_subject_jobs(cur, subject_key):
    """Remove every retry record of one user, inside the caller's transaction (conversation delete)."""
    cur.execute('DELETE FROM chat.request_job WHERE subject_key=%s', (subject_key,))


def cleanup_expired_jobs():
    from engine.pg import _pg
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute('DELETE FROM chat.request_job WHERE expires_at < now() AND lease_until < now()')
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()
