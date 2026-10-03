"""Durable retries across independent serving objects, against disposable PostgreSQL."""
import os
import threading
import uuid


def main():
    if not os.environ.get('KB_PG_PASSWORD'):
        raise RuntimeError('Durable replay gate requires the isolated PostgreSQL runtime')
    from engine.pg import _pg
    from engine.request_replay import DurableResponseReplay, ReplayConflict
    from engine import request_deadline

    subject = 'replay_gate_' + uuid.uuid4().hex
    key = ('/api/reason', subject, 'same_turn')
    one, two = DurableResponseReplay(), DurableResponseReplay()
    response = (200, '{"result":{"rows":[[17]]}}', 'application/json', None)
    try:
        assert one.claim(key, 'input_a') == (True, None)
        results = []
        waiting = threading.Event()
        def retry():
            token = request_deadline.begin(5)
            try:
                waiting.set()
                results.append(two.claim(key, 'input_a'))
            finally:
                request_deadline.end(token)
        worker = threading.Thread(target=retry, daemon=True)
        worker.start(); assert waiting.wait(1)
        one.finish(key, response)
        worker.join(6)
        assert not worker.is_alive() and results == [(False, response)], results
        assert DurableResponseReplay().claim(key, 'input_a') == (False, response)
        try:
            two.claim(key, 'changed_input')
        except ReplayConflict:
            pass
        else:
            raise AssertionError('Changed payload replayed an earlier answer')
        transient = ('/api/reason', subject, 'transient')
        assert one.claim(transient, 'input_b') == (True, None)
        one.finish(transient, (503, '{}', 'application/json', None))
        assert two.claim(transient, 'input_b') == (True, None)
        two.finish(transient, response)
        stale = ('/api/reason', subject, 'stale')
        assert one.claim(stale, 'input_c') == (True, None)
        conn = _pg()
        try:
            conn.cursor().execute("UPDATE chat.request_job SET lease_until=now()-interval '1 second' WHERE subject_key=%s AND job_id='stale'", (subject,))
            conn.commit()
        finally:
            conn.close()
        assert two.claim(stale, 'input_c') == (True, None)
        one.finish(stale, (200, 'obsolete', 'text/plain', None))
        two.finish(stale, response)
        assert DurableResponseReplay().claim(stale, 'input_c') == (False, response)
        print('PASS durable replay: concurrent instance, restart, changed input, transient retry, stale owner', flush=True)
    finally:
        conn = _pg()
        try:
            conn.cursor().execute('DELETE FROM chat.request_job WHERE subject_key=%s', (subject,))
            conn.commit()
        finally:
            conn.close()


if __name__ == '__main__':
    main()
