"""LIVE multi-tenant Postgres serving.

Reuses the WorldTableQuery PLANNER **unchanged** (route/meaning-graph/SQL assembly) and only swaps EXECUTION:
the generated SQL runs against live Postgres in `search_path = "<schema>", wikipedia, world, public` instead
of in-memory SQLite + ATTACH words.db. `<schema>` = the caller's VERIFIED Google sub (per-user, set by the
server from a verified Firebase token — never client-supplied). Uploaded CSVs are loaded into that schema as
real tables (persisted). The shared world/wikipedia schemas are built by the db/sync pipeline.

qident -> "double quotes" and qlit -> 'single quotes' are already Postgres-compatible, and the upload
affinities INTEGER/REAL/TEXT are valid Postgres types, so the planner's SQL ports verbatim; only the executor
(SQLite ":memory:" + "?" placeholders + ATTACH) changes here.
"""
from __future__ import annotations

import psycopg2

from engine.config import (WORLD_PG_DB, WORLD_PG_HOST, WORLD_PG_PORT, WORLD_PG_SSLMODE, WORLD_PG_USER,
                           world_pg_password)
from engine.tables import TableQuery, qident
from engine.world_tables import WorldTableQuery

# SQLite affinities -> WIDER Postgres types: SQLite INTEGER is 64-bit and REAL is 8-byte, but Postgres INTEGER
# is 32-bit (overflows on large SKUs/IDs) and REAL is 4-byte. Map to BIGINT / double precision to match SQLite.
_PGTYPE = {"INTEGER": "BIGINT", "REAL": "DOUBLE PRECISION", "TEXT": "TEXT"}

# Postgres NUMERIC/DECIMAL (returned by SUM(bigint), AVG, …) -> psycopg2 Decimal, which json.dumps can't
# serialize -> 500. Cast NUMERIC (OID 1700) to float so aggregate results are JSON-safe (the SQLite path
# returned int/float too). Registered process-wide.
import psycopg2.extensions  # noqa: E402


def _numeric_to_py(v, cur):
    if v is None:
        return None
    if "." not in v and "e" not in v and "E" not in v:   # whole NUMERIC (e.g. SUM(bigint)) -> exact int "300" (not 300.0)
        return int(v)
    f = float(v)
    return int(f) if f.is_integer() else f               # AVG/fractional -> float


psycopg2.extensions.register_type(psycopg2.extensions.new_type((1700,), "NUMERIC2PY", _numeric_to_py))


def _pg():
    """Connect to the Postgres world DB (unix socket on Cloud Run, host:port + SSL over TCP)."""
    kw = dict(host=WORLD_PG_HOST, dbname=WORLD_PG_DB, user=WORLD_PG_USER,
              password=world_pg_password(), connect_timeout=30)
    if not WORLD_PG_HOST.startswith("/"):
        kw["port"] = WORLD_PG_PORT
        kw["sslmode"] = WORLD_PG_SSLMODE
    return psycopg2.connect(**kw)


def _load_user_schema(cur, schema, sch, tablemap):
    """Create the per-user schema, (re)load the uploaded sheets as tables, set search_path = "<schema>", world.
    `schema` MUST be a server-verified identifier (the Google sub); it is only ever quoted, never executed."""
    cur.execute(f'CREATE SCHEMA IF NOT EXISTS {qident(schema)}')
    # user schema + world FIRST (they own every table the planner names); `public` LAST so the pgvector `vector`
    # type and its `<=>` operator (installed in public) resolve for the embedding bridge. No shadowing:
    # uploads live in <schema>, world tables/views in world; public holds only the raw Wikidata import + pgvector.
    cur.execute(f'SET search_path TO {qident(schema)}, wikipedia, world, public')   # wikipedia FIRST: bare world-table names (city/country) resolve to the qid-keyed wikipedia schema
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
            cur.execute(ins, [WorldTableQuery._coerce(rd.get(c["name"]), c["affinity"]) for c in cols])


class _PgCon:
    """Quacks like the slice of sqlite3.Connection that WorldTableQuery.serve() uses: .execute(sql) -> cursor
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

    def execute(self, tablemap, sch, sql):
        conn = _pg(); cur = conn.cursor()
        try:
            _load_user_schema(cur, self._pg_schema, sch, tablemap); conn.commit()
            cur.execute(sql)
            return [d[0] for d in cur.description], cur.fetchall()
        finally:
            conn.close()


class PgQuery(WorldTableQuery):
    """WorldTableQuery planner + Postgres execution, scoped to a per-request verified schema."""

    def __init__(self, deploy_dir):
        super().__init__(deploy_dir)
        self.q11.__class__ = _TableQueryPg       # rebless the shared TableQuery (same loaded model) -> PG own-data path
        self._pg_schema = None
        self._con = None

    def serve(self, tables, question, as_of=None, schema=None):
        if not schema:
            return {"error": "not signed in (no schema)", "question": question}
        self._pg_schema = schema
        self.q11._pg_schema = schema
        self._con = None
        try:
            return super().serve(tables, question, as_of)
        finally:
            if self._con is not None:
                self._con.close(); self._con = None

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
        conn = _pg(); cur = conn.cursor(); cur.execute("SET search_path TO wikipedia, world")
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
        conn = _pg(); cur = conn.cursor(); cur.execute("SET search_path TO wikipedia, world")
        out, seeds = [], [str(v).lower() for v in seed_values if v not in (None, "")]
        for idx, j in enumerate(joins):
            wt, key, w = j["right_table"], j["right_col"], self.words[j["right_table"]]
            cols = [c for c in w["columns"] if c not in ("updated_at", "source", "valid_from", "valid_to")]
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
