"""The serving connection owner (engine/pg.py:_pg) against PostgreSQL (the isolated runtime only):
a request deadline's statement timeouts, and the process pool's reset.

The engine set a statement timeout before every statement of a request with a deadline, including
the ROLLBACK TO SAVEPOINT that SQLAlchemy sends after a failed statement. An aborted transaction
accepts only a rollback, so the timeout statement failed, the rollback never ran, and the Python
program's SQL retry failed on the broken transaction (2026-10-04).

Since 2026-10-27 the timeout is sent once per request and connection (a round trip per statement
before), and connections are pooled per process (a question opened 12, about 35 ms each)."""
import os
import sys


def test_a_rollback_reaches_an_aborted_transaction():
    from engine import request_deadline
    from engine.pg import _pg
    token = request_deadline.begin(30)
    try:
        conn = _pg()
        try:
            cur = conn.cursor()
            cur.execute('SAVEPOINT deadline_probe')
            try:
                cur.execute('SELECT 1/0')
                raise AssertionError('division by zero succeeded')
            except Exception as error:                   # noqa: BLE001 — the aborted transaction is the point
                if isinstance(error, AssertionError):
                    raise
            cur.execute('ROLLBACK TO SAVEPOINT deadline_probe')
            cur.execute('SELECT 1')
            assert cur.fetchone() == (1,)
            cur.execute("SELECT current_setting('lock_timeout'), current_setting('statement_timeout')")
            lock, statement = cur.fetchone()
            assert lock == statement, (lock, statement)
        finally:
            conn.rollback()
            conn.close()
    finally:
        request_deadline.end(token)


def _timeout(cur):
    cur.execute("SELECT current_setting('statement_timeout')")
    return cur.fetchone()[0]


def test_a_request_s_timeout_is_sent_once_and_again_after_a_rollback():
    """A rolled-back transaction reverts the set_config it carried: a connection that believed it still had its
    timeout would run the next statements with none."""
    from engine import request_deadline
    from engine.pg import _pg
    token = request_deadline.begin(240)                 # the serving budget: a 4.8 s slack
    conn = _pg()
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1')
        sent = conn.timeout_set
        assert sent is not None
        for _ in range(5):
            cur.execute('SELECT 1')
        assert conn.timeout_set is sent, 'the timeout was sent again within its slack'
        assert _timeout(cur) != '0'
        conn.rollback()
        assert _timeout(cur) != '0', 'the timeout was not sent again after a rollback reverted it'
        cur.execute('SAVEPOINT reverted')
        cur.execute("SELECT set_config('statement_timeout', '0', false)")
        cur.execute('ROLLBACK TO SAVEPOINT reverted')
        assert conn.timeout_set is None, 'a rollback to a savepoint must be treated as reverting the timeout'
    finally:
        conn.rollback()
        conn.close()
        request_deadline.end(token)


def test_a_pooled_connection_comes_back_reset():
    from engine import request_timing
    from engine.pg import _POOL, _pg
    conn = _pg()
    cur = conn.cursor()
    cur.execute('SELECT pg_backend_pid()')
    pid = cur.fetchone()[0]
    cur.execute("SELECT current_setting('search_path')")
    search_path = cur.fetchone()[0]
    conn.commit()
    conn.autocommit = True
    cur.execute('SET search_path TO pg_catalog')
    cur.execute("SELECT set_config('statement_timeout', '1234', false)")
    cur.execute('SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))', ('pool-test', str(pid)))
    cur.execute('CREATE TEMP TABLE pooled_probe (x int)')
    conn.close()
    conn.close()                                          # twice: the pool must hold it once
    assert sum(1 for c, _ in _POOL._idle if c is conn) == 1
    try:
        cur.execute('SELECT 1')
        raise AssertionError('a closed connection ran a statement')
    except Exception as error:                            # noqa: BLE001 — cursor() on an idle connection
        if isinstance(error, AssertionError):
            raise

    token = request_timing.begin('pool-test')
    try:
        again = _pg()
        reused = request_timing._CTX.get().counters.get('pg_reused')
    finally:
        request_timing.end(token)
    try:
        assert again is conn and reused == 1, reused
        assert again.autocommit is False and again.timeout_set is None
        cur = again.cursor()
        cur.execute('SELECT pg_backend_pid()')
        assert cur.fetchone()[0] == pid
        cur.execute("SELECT current_setting('search_path')")
        assert cur.fetchone()[0] == search_path
        assert _timeout(cur) != '1234ms'
        cur.execute("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s", (pid,))
        assert cur.fetchone()[0] == 0, 'a session advisory lock outlived the request that took it'
        cur.execute("SELECT to_regclass('pg_temp.pooled_probe')")
        assert cur.fetchone()[0] is None
    finally:
        again.rollback()
        again.close()


def test_the_orm_connection_is_never_pooled():
    """Its NUMERIC caster is registered on the connection and cannot be removed: a pooled one would hand
    Decimals to a caller expecting the serving cursor's values."""
    from engine.pg import _POOL, _orm_pg
    conn = _orm_pg()
    conn.close()
    assert conn.closed and all(c is not conn for c, _ in _POOL._idle)


TESTS = [
    test_a_rollback_reaches_an_aborted_transaction,
    test_a_request_s_timeout_is_sent_once_and_again_after_a_rollback,
    test_a_pooled_connection_comes_back_reset,
    test_the_orm_connection_is_never_pooled,
]


def main():
    if not os.environ.get('KB_PG_PASSWORD'):
        raise RuntimeError('The connection checks require the isolated PostgreSQL runtime')
    failures = 0
    for test in TESTS:
        try:
            test()
            print(f'  ok   {test.__name__}', flush=True)
        except AssertionError as error:
            failures += 1
            print(f'  FAIL {test.__name__}: {error}', flush=True)
    print(f'{len(TESTS) - failures}/{len(TESTS)} passed', flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
