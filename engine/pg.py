"""Live multi-tenant PostgreSQL execution.

The typed planner is shared with SQLite, but each executor renders its own numeric
dialect. Uploaded fractional values use bounded PostgreSQL ``NUMERIC`` columns and
typed division is rendered with ``NUMERIC`` casts; SQLite uses the exact-decimal
functions from :mod:`engine.numeric`. The per-user schema always comes from the
verified identity, never from request data.
"""
from __future__ import annotations

import time

import psycopg2

from engine.config import (KB_PG_DB, KB_PG_HOST, KB_PG_PORT, KB_PG_SSLMODE, KB_PG_USER,
                           kb_pg_password)
from engine.numeric import parse_decimal, wire_decimal
from engine.sql_ast import render_query
from engine.tables import TableQuery, qident
from engine.knowledge_tables import KnowledgeTableQuery

# Uploaded fractional values are financial/reference data surprisingly often. PostgreSQL NUMERIC preserves
# their decimal representation and arithmetic exactly; binary DOUBLE PRECISION does not.
_PGTYPE = {"INTEGER": "BIGINT", "REAL": "NUMERIC(58,20)", "TEXT": "TEXT"}
_CONNECT_ATTEMPTS = 3
_NON_RETRYABLE_CONNECT_ERRORS = (
    "password authentication failed",
    "no pg_hba.conf entry",
    "does not exist",
)

# Keep NUMERIC exact. JSON-safe integral values remain integers; a fractional decimal crosses the wire as a
# canonical string only when no binary float can represent it exactly.
import psycopg2.extensions  # noqa: E402


def _numeric_to_py(v, cur):
    if v is None:
        return None
    return wire_decimal(parse_decimal(v, enforce_input_bounds=False))


psycopg2.extensions.register_type(psycopg2.extensions.new_type((1700,), "NUMERIC2PY", _numeric_to_py))


def _pg():
    """Connect to Postgres, retrying only transient transport failures.

    Authentication and database/role configuration errors fail immediately. The bounded retry is
    centralized here so request helpers do not each grow a different connection policy.
    """
    kw = dict(host=KB_PG_HOST, dbname=KB_PG_DB, user=KB_PG_USER,
              password=kb_pg_password(), connect_timeout=30)
    if not KB_PG_HOST.startswith("/"):
        kw["port"] = KB_PG_PORT
        kw["sslmode"] = KB_PG_SSLMODE
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            return psycopg2.connect(**kw)
        except psycopg2.OperationalError as exc:
            message = str(exc).lower()
            if any(marker in message for marker in _NON_RETRYABLE_CONNECT_ERRORS):
                raise
            if attempt + 1 == _CONNECT_ATTEMPTS:
                raise
            time.sleep(0.25 * (2 ** attempt))
    raise AssertionError("unreachable")


def _load_user_schema(cur, schema, sch, tablemap):
    """Create the per-user schema, (re)load the uploaded sheets as tables, set search_path = "<schema>", knowledgebase.
    `schema` MUST be a server-verified identifier (the Google sub); it is only ever quoted, never executed."""
    cur.execute(f'CREATE SCHEMA IF NOT EXISTS {qident(schema)}')
    # user schema + knowledgebase FIRST (they own every table the planner names); `public` LAST so the pgvector `vector`
    # type and its `<=>` operator (installed in public) resolve for the embedding bridge. No shadowing:
    # uploads live in <schema>, shared projections in knowledgebase; public holds only the raw Wikidata import + pgvector.
    cur.execute(f'SET search_path TO {qident(schema)}, knowledgebase, public')   # knowledgebase FIRST: bare world-table names resolve to qid-keyed projections
    by_t = {}
    for c in sch:
        by_t.setdefault(c["table"], []).append(c)
    for tname, cols in by_t.items():
        cur.execute(f'DROP TABLE IF EXISTS {qident(schema)}.{qident(tname)}')   # replace on re-upload
        cur.execute(f'CREATE TABLE {qident(schema)}.{qident(tname)} (' +
                    ", ".join(f'{qident(c["name"])} {_PGTYPE.get(c["affinity"], "TEXT")}' for c in cols) + ')')
        t = tablemap[tname]
        ins = f'INSERT INTO {qident(schema)}.{qident(tname)} VALUES (' + ",".join(["%s"] * len(cols)) + ')'
        for r in t["rows"]:
            rd = dict(zip(t["columns"], r))
            cur.execute(ins, [KnowledgeTableQuery._coerce(rd.get(c["name"]), c["affinity"]) for c in cols])


class _PgCon:
    """Quacks like the slice of sqlite3.Connection that KnowledgeTableQuery.serve() uses: .execute(sql) -> cursor
    with .description/.fetchall(). The answer SQL inlines its literals via qlit, so there are no '?' to translate."""
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        cur = self.conn.cursor()
        cur.execute(sql, params or ())
        return cur

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class _TableQueryPg(TableQuery):
    """Own-data path executor → Postgres (so uploads persist in the user schema and answers are consistent)."""
    _pg_schema = None

    def execute(self, tablemap, sch, sql, query=None):
        conn = _pg(); cur = conn.cursor()
        try:
            _load_user_schema(cur, self._pg_schema, sch, tablemap); conn.commit()
            execution_sql = render_query(query, dialect="postgres_numeric") if query is not None else sql
            cur.execute(execution_sql)
            return [d[0] for d in cur.description], cur.fetchall()
        finally:
            conn.close()


class PgQuery(KnowledgeTableQuery):
    """KnowledgeTableQuery planner + Postgres execution, scoped to a per-request verified schema."""

    @staticmethod
    def _numeric_aggregate(function, operand):
        return f"{function.upper()}( {operand} )"

    @staticmethod
    def _numeric_multiply(left, right):
        return f"({left} * {right})"

    def __init__(self, deploy_dir):
        super().__init__(deploy_dir)
        self.q11.__class__ = _TableQueryPg       # rebless the shared TableQuery (same loaded model) -> PG own-data path
        self._pg_schema = None
        self._con = None

    def serve(self, tables, question, as_of=None, schema=None, explicit_fks=()):
        if not schema:
            return {"error": "not signed in (no schema)", "question": question}
        self._pg_schema = schema
        self.q11._pg_schema = schema
        self._con = None
        try:
            return super().serve(tables, question, as_of, explicit_fks=explicit_fks)
        finally:
            if self._con is not None:
                self._con.close(); self._con = None

    def _table_freshness(self, con, table, as_of):
        """A world table with no per-row updated_at is judged from the maintenance catalog instead
        of passing silently. Read through the connection the query already holds: this runs inside
        serve()'s try, where ANY raised exception costs the user their answer, so opening a second
        connection here would turn a transient network blip into a lost answer. to_regclass keeps
        the un-migrated case to one ordinary statement rather than a swallowed exception, and the
        catalog is small enough (one indexed row) that reading it per request beats caching a value
        that a nightly refresh would leave falsely 'overdue'.

        Only two things are worth saying: the table is past its declared cadence, or nobody declared
        it. A table declared on-demand (cadence NULL) is an intentional, recorded state — warning
        about that on every geo query would be noise, not disclosure."""
        if con.execute("SELECT to_regclass('knowledgebase.schedule')").fetchone()[0] is None:
            return []                             # un-migrated database: say nothing, invent nothing
        row = con.execute('SELECT cadence_hours, last_refreshed_at FROM knowledgebase."schedule"'
                          " WHERE table_name = %s", (table,)).fetchone()
        if row is None:
            return [f"freshness: world table '{table}' has no maintenance record — "
                    f"declare it in db/sync/schedule.py:CATALOG"]
        cadence, last = row
        if cadence is None:
            return []
        if last is None:
            return [f"freshness: '{table}' is scheduled every {cadence}h but has never recorded a refresh"]
        stamp = last.date() if hasattr(last, "date") else last
        overdue_h = self._days(stamp, as_of) * 24
        if overdue_h > cadence:
            return [f"freshness: '{table}' last refreshed {stamp}, {int(overdue_h)}h before the "
                    f"as-of date {as_of} and past its {cadence}h cadence — may be stale"]
        return []

    def _connect(self, tablemap, sch, attach_world):
        """World path executor → Postgres. The world tables are visible via search_path (no ATTACH)."""
        conn = _pg(); cur = conn.cursor()
        _load_user_schema(cur, self._pg_schema, sch, tablemap); conn.commit()
        self._con = _PgCon(conn)
        return self._con

    def ambiguities(self, table, routed_col, wt):
        w = self.words[wt]; key = w["key"]; haskind = "country" in w.get("columns", [])
        # Match the uploaded value against the entity NAME column, not the key. For qid-keyed tables (city,
        # country, state) the key is the opaque QID, so `WHERE lower(qid)=<name>` never matched and NO
        # ambiguity was ever flagged. The name column is what the uploaded string actually corresponds to.
        name_col = "name" if "name" in w.get("columns", []) else key
        vals = [str(dict(zip(table["columns"], r)).get(routed_col)) for r in table["rows"]]
        vals = [v for v in vals if v and v != "None"]
        if not vals:
            return []
        conn = _pg(); cur = conn.cursor(); cur.execute("SET search_path TO knowledgebase")
        warns, seen = [], set()
        for v in vals:
            vl = v.lower()
            if vl in seen:
                continue
            seen.add(vl)
            if haskind:
                cur.execute(f'SELECT DISTINCT country FROM {qident(wt)} WHERE lower({qident(name_col)})=%s', (vl,))
                opts = [r[0] for r in cur.fetchall()]
                if len(opts) > 1:
                    warns.append(f"'{v}' is ambiguous in {wt}: {', '.join(sorted(opts))}")
            else:
                cur.execute(f'SELECT COUNT(*) FROM {qident(wt)} WHERE lower({qident(name_col)})=%s', (vl,))
                if cur.fetchone()[0] > 1:
                    warns.append(f"'{v}' is ambiguous in {wt}: multiple rows")
        conn.close()
        return warns

    def _world_rows(self, joins, seed_values, cap=12):
        conn = _pg(); cur = conn.cursor(); cur.execute("SET search_path TO knowledgebase")
        out, seeds = [], [str(v).lower() for v in seed_values if v not in (None, "")]
        for idx, j in enumerate(joins):
            wt, key, w = j["right_table"], j["right_col"], self.words[j["right_table"]]
            cols = [c for c in w["columns"]
                    if c not in ("updated_at", "source", "source_release_id", "valid_from", "valid_to")]
            seedset = sorted(set(seeds))
            if not seedset:
                break
            ph = ",".join(["%s"] * len(seedset))
            extra = " AND is_primary=1" if "is_primary" in w["columns"] else ""
            cur.execute(f'SELECT {", ".join(qident(c) for c in cols)} FROM {qident(wt)} '
                        f'WHERE lower({qident(key)}) IN ({ph}){extra} LIMIT {cap}', tuple(seedset))
            rows = cur.fetchall()
            rd = [[("" if v is None else v) for v in r] for r in rows]
            out.append({"name": wt, "columns": cols, "rows": rd})
            nxt = joins[idx + 1] if idx + 1 < len(joins) else None
            if nxt and nxt["left_table"] == wt and nxt["left_col"] in cols:
                ci = cols.index(nxt["left_col"]); seeds = [str(r[ci]).lower() for r in rd if r[ci] not in (None, "")]
            else:
                seeds = []
        conn.close()
        return out
