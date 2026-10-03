"""Ownership-scoped durable retry records, shared across serving instances."""
from __future__ import annotations

import json
import time
import uuid
from contextvars import ContextVar


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
                cur.execute('SELECT payload_hash, lease_owner, lease_until > now(), response '
                            'FROM chat.request_job WHERE route=%s AND subject_key=%s AND job_id=%s FOR UPDATE', key)
                digest, held_by, active, response = cur.fetchone()
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
            # Waiting callers never hold a database transaction or the model mutex.
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
                        "expires_at=now()+interval '1 day' WHERE route=%s AND subject_key=%s AND job_id=%s "
                        'AND lease_owner=%s', (body, *key,
                         self._owner.get()[1] if self._owner.get() and self._owner.get()[0] == key else None))
            conn.commit()
            self._owner.set(None)
        finally:
            if conn is not None:
                conn.close()
            request_deadline.end(token)

    def release(self, key):
        self.finish(key, (503, '', 'application/json', None))


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
