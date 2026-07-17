"""master.py — per-user MASTER (reference) data: the user's own "world model" for the PRIVATE entities
Wikidata doesn't know (their products, reps, regions, SKUs...). It's stored in a per-user Postgres schema
`m_<hash(sub)>` in the same `world` database as the wikipedia/world schemas, so it persists across ALL of
that user's conversations and (Phase 3) a query's search_path can span
`"<conversation>", "<master>", wikipedia, world, public` — joining private + public data in one query.

Security mirrors engine.conversations: the user_id is ALWAYS the verified token subject (engine.auth), never
client-supplied. The schema name is DERIVED from it (md5), so a user can only ever read/write their own
master data — there is no client-controlled schema/table path.
"""
from __future__ import annotations

import hashlib

from engine.pg import _pg

MAX_COLS = 64
MAX_ROWS = 50000


def master_schema(user_id):
    """The per-user master schema name: `m_<32 hex>` (same safe fixed shape as a conversation's `c_<32 hex>`)."""
    return "m_" + hashlib.md5((user_id or "").encode("utf-8")).hexdigest()


def _qi(name):
    """A safe quoted SQL identifier from an arbitrary user string (trim to 63 bytes, double any quote, reject
    empty). Quoting — not sanitizing — preserves the user's real table/column names ('Price ($)')."""
    s = str(name if name is not None else "").strip().replace('"', '""')[:63]
    if not s:
        raise ValueError("empty identifier")
    return '"' + s + '"'


def _ensure_schema(cur, schema):
    cur.execute('CREATE SCHEMA IF NOT EXISTS "%s"' % schema)   # schema is m_<32hex> (derived, injection-safe)


def _cols(cur, schema, table):
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, table))
    return [r[0] for r in cur.fetchall()]


def list_master(user_id):
    """Every master table this user has, with its columns + row count — for the overview / cross-conversation load."""
    sch = master_schema(user_id)
    conn = _pg()
    try:
        cur = conn.cursor()
        _ensure_schema(cur, sch)
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name", (sch,))
        names = [r[0] for r in cur.fetchall()]
        out = []
        for n in names:
            cols = _cols(cur, sch, n)
            cur.execute('SELECT count(*) FROM "%s".%s' % (sch, _qi(n)))
            out.append({"name": n, "columns": cols, "rows": int(cur.fetchone()[0])})
        conn.commit()
        return out
    finally:
        conn.close()


def get_master(user_id, name):
    """One master table's columns + rows (for editing). None if it doesn't exist. Rows capped for transport."""
    sch = master_schema(user_id)
    conn = _pg()
    try:
        cur = conn.cursor()
        _ensure_schema(cur, sch)
        cols = _cols(cur, sch, str(name or ""))
        if not cols:
            return None
        cur.execute('SELECT * FROM "%s".%s LIMIT %s' % (sch, _qi(name), MAX_ROWS))
        rows = [["" if v is None else str(v) for v in r] for r in cur.fetchall()]
        conn.commit()
        return {"name": name, "columns": cols, "rows": rows}
    finally:
        conn.close()


def save_master(user_id, name, columns, rows):
    """Create-or-REPLACE a master table (drop + create + insert — a full overwrite of that reference table).
    All columns are stored as text; the FIRST column is the key that links to the user's data (Phase 3)."""
    name = str(name or "").strip()
    columns = [str(c).strip() for c in (columns or []) if str(c).strip()]
    if not name or not columns:
        raise ValueError("a table name and at least one column are required")
    columns = columns[:MAX_COLS]
    rows = (rows or [])[:MAX_ROWS]
    sch = master_schema(user_id)
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            _ensure_schema(cur, sch)
            tq = _qi(name)
            cur.execute('DROP TABLE IF EXISTS "%s".%s' % (sch, tq))
            cur.execute('CREATE TABLE "%s".%s (%s)' % (sch, tq, ", ".join(_qi(c) + " text" for c in columns)))
            if rows:
                ph = "(" + ",".join(["%s"] * len(columns)) + ")"
                norm = []
                for r in rows:
                    r = list(r)[:len(columns)] + [None] * (len(columns) - len(r))
                    norm.append([None if v is None or v == "" else str(v) for v in r])
                cur.executemany('INSERT INTO "%s".%s VALUES %s' % (sch, tq, ph), norm)
            conn.commit()
            return {"name": name, "columns": columns, "rows": len(rows)}
        except Exception:
            try:
                conn.rollback()
            except Exception:                                 # noqa: BLE001
                pass
            raise
    finally:
        conn.close()


def delete_master(user_id, name):
    """Drop a master table."""
    sch = master_schema(user_id)
    conn = _pg()
    try:
        cur = conn.cursor()
        _ensure_schema(cur, sch)
        cur.execute('DROP TABLE IF EXISTS "%s".%s' % (sch, _qi(name)))
        conn.commit()
        return {"deleted": name}
    finally:
        conn.close()
