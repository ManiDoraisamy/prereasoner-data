"""A request deadline's statement timeouts against PostgreSQL (the isolated runtime only).

The engine set a statement timeout before every statement of a request with a deadline, including
the ROLLBACK TO SAVEPOINT that SQLAlchemy sends after a failed statement. An aborted transaction
accepts only a rollback, so the timeout statement failed, the rollback never ran, and the Python
program's SQL retry failed on the broken transaction (2026-10-04)."""
import os


def main():
    if not os.environ.get('KB_PG_PASSWORD'):
        raise RuntimeError('The deadline check requires the isolated PostgreSQL runtime')
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
    print('PASS pg deadline: a rollback reaches an aborted transaction; locks wait the statement budget', flush=True)


if __name__ == '__main__':
    main()
