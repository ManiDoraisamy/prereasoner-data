"""Live multi-tenant PostgreSQL execution.

The typed planner is shared with SQLite, but each executor renders its own numeric
dialect. Uploaded fractional values use bounded PostgreSQL ``NUMERIC`` columns and
typed division is rendered with ``NUMERIC`` casts; SQLite uses the exact-decimal
functions from :mod:`engine.numeric`. The per-user schema always comes from the
verified identity, never from request data.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

import psycopg2
from psycopg2.extras import execute_values

from engine import request_timing
from engine.config import (
    APP_ENV,
    KB_PG_DB,
    KB_PG_HOST,
    KB_PG_PORT,
    KB_PG_SSLMODE,
    KB_PG_USER,
    deterministic_execution_mode,
    deterministic_persist_generated,
    deterministic_python_row_limit,
    kb_pg_password,
)
from engine.knowledge_tables import KnowledgeTableQuery
from engine.numeric import parse_decimal, wire_decimal
from engine.sql_ast import render_query
from engine.tables import TableQuery, qident

# Uploaded fractional values are financial/reference data surprisingly often. PostgreSQL NUMERIC preserves
# their decimal representation and arithmetic exactly; binary DOUBLE PRECISION does not.
_PGTYPE = {"INTEGER": "BIGINT", "REAL": "NUMERIC(58,20)", "TEXT": "TEXT"}
_CONNECT_ATTEMPTS = 3
_UPLOAD_PAGE_SIZE = 500          # rows per INSERT statement; bounds statement size on wide 5000-row sheets
_NON_RETRYABLE_CONNECT_ERRORS = (
    "password authentication failed",
    "no pg_hba.conf entry",
    "does not exist",
)

# Keep NUMERIC exact. JSON-safe integral values remain integers; a fractional decimal crosses the wire as a
# canonical string only when no binary float can represent it exactly.
import psycopg2.extensions


def _numeric_to_py(v, cur):
    if v is None:
        return None
    return wire_decimal(parse_decimal(v, enforce_input_bounds=False))


psycopg2.extensions.register_type(psycopg2.extensions.new_type((1700,), "NUMERIC2PY", _numeric_to_py))


_SLOW_SQL_MS = 150                                       # a single statement slower than this gets a fingerprint line
_REDACT_STRINGS = re.compile(r"'(?:[^']|'')*'")          # engine SQL inlines literals via qlit — NEVER log them
_REDACT_NUMBERS = re.compile(r"\b\d+(?:\.\d+)?\b")


def _sql_fingerprint(query):
    """A privacy-safe template of one statement: string literals and numbers become '?', whitespace
    collapses. What remains is the STRUCTURE (verbs, tables, columns) — enough to find the code
    path, nothing of the user's cells or question."""
    text = query if isinstance(query, str) else str(query)
    text = _REDACT_STRINGS.sub("?", text)
    text = _REDACT_NUMBERS.sub("?", text)
    return " ".join(text.split())[:140]


class _TimedCursor(psycopg2.extensions.cursor):
    """Cursor that bills every statement to the request's timing line.

    Installed as the cursor_factory in `_pg`, the ONE connection owner for every serving path, so
    the `sql_ms`/`sql_n` totals cover all statements — planner, world lookups, bridge writes and
    the uploaded-sheet load alike — without a call site having to opt in. Outside a request the
    span/count helpers are no-ops. A statement slower than _SLOW_SQL_MS additionally logs its
    redacted fingerprint, so a production tail (one slow shape among 40 fast ones) is attributable
    without attaching a profiler.
    """

    def execute(self, query, vars=None):
        started = time.perf_counter()
        with request_timing.span("sql"):                 # the span publishes its own sql_n
            result = super().execute(query, vars)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms >= _SLOW_SQL_MS and request_timing.request_id() is not None:
            print(f"[timing] slow_sql rid={request_timing.request_id()} ms={elapsed_ms:.0f} "
                  f"sql={_sql_fingerprint(query)}", flush=True)
        return result

    def executemany(self, query, vars_list):
        # One CALL, but psycopg2 sends one statement per parameter set. Counting the call would
        # under-report the round trips by exactly the factor that makes executemany slow, so the
        # extra statements are counted explicitly.
        rows = list(vars_list)
        with request_timing.span("sql"):
            result = super().executemany(query, rows)
        request_timing.count("sql_n", max(0, len(rows) - 1))   # the span already counted one
        return result


def _pg():
    """Connect to Postgres, retrying only transient transport failures.

    Authentication and database/role configuration errors fail immediately. The bounded retry is
    centralized here so request helpers do not each grow a different connection policy.
    """
    kw = dict(host=KB_PG_HOST, dbname=KB_PG_DB, user=KB_PG_USER,
              password=kb_pg_password(), connect_timeout=30, cursor_factory=_TimedCursor)
    if not KB_PG_HOST.startswith("/"):
        kw["port"] = KB_PG_PORT
        kw["sslmode"] = KB_PG_SSLMODE
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            with request_timing.span("pg_connect"):      # the span publishes its own pg_connect_n
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
    """Create the conversation schema and load only changed source tables.

    A source table keeps the canonical CSV stem (``orders.csv`` -> ``orders``) for the
    lifetime of the conversation.  ``chat.working_table`` holds a deterministic content
    hash, so an unchanged follow-up reuses the existing table instead of dropping and
    rebuilding it. Workbook freshness is owned by the conversation's dataset version;
    this request-local working-table manifest must not invalidate unrelated analyses
    merely because two questions select different reference or enrichment tables.

    `schema` MUST be a server-authorized conversation id; it is only ever quoted, never executed."""
    # Cloud Run instances can serve the same conversation concurrently. Serialize schema replacement
    # across instances so one request cannot drop a table while another is planning against it.
    cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                (f"prereasoner-conversation:{schema}",))
    cur.execute(f'CREATE SCHEMA IF NOT EXISTS {qident(schema)}')
    # user schema + knowledgebase FIRST (they own every table the planner names); `public` LAST so the pgvector `vector`
    # type and its `<=>` operator (installed in public) resolve for the embedding bridge. No shadowing:
    # uploads live in <schema>, shared projections in knowledgebase; public holds only the raw Wikidata import + pgvector.
    cur.execute(f'SET search_path TO {qident(schema)}, knowledgebase, public')   # knowledgebase FIRST: bare world-table names resolve to qid-keyed projections
    by_t = {}
    for c in sch:
        by_t.setdefault(c["table"], []).append(c)
    cur.execute('SELECT table_name FROM "chat"."working_table" WHERE conversation_id = %s',
                (schema,))
    removed = sorted(str(row[0]) for row in cur.fetchall() if str(row[0]) not in by_t)
    if removed:
        for table_name in removed:
            cur.execute(f'DROP TABLE IF EXISTS {qident(schema)}.{qident(table_name)} CASCADE')
        cur.execute('DELETE FROM "chat"."working_table" '
                    'WHERE conversation_id = %s AND table_name = ANY(%s)', (schema, removed))
    with request_timing.span("upload"):
        for tname, cols in by_t.items():
            t = tablemap[tname]
            fingerprint = hashlib.sha256(json.dumps(
                {"columns": [(c["name"], c["affinity"]) for c in cols], "rows": t["rows"]},
                ensure_ascii=False, separators=(",", ":"), default=str,
            ).encode("utf-8")).hexdigest()
            cur.execute('SELECT content_hash FROM "chat"."working_table" '
                        'WHERE conversation_id = %s AND table_name = %s', (schema, tname))
            previous = cur.fetchone()
            cur.execute('SELECT to_regclass(%s)', (f'{qident(schema)}.{qident(tname)}',))
            relation_exists = cur.fetchone()[0] is not None
            if previous and previous[0] == fingerprint and relation_exists:
                continue
            # Persistent analysis snapshots are metadata, not database dependencies. CASCADE is
            # still deliberate: conversation-local bridge relations may depend on an edited input.
            cur.execute(f'DROP TABLE IF EXISTS {qident(schema)}.{qident(tname)} CASCADE')
            cur.execute(f'CREATE TABLE {qident(schema)}.{qident(tname)} (' +
                        ", ".join(f'{qident(c["name"])} {_PGTYPE.get(c["affinity"], "TEXT")}' for c in cols) + ')')
            # ONE multi-row INSERT per page instead of one statement per row. Each row is still built by
            # the same `_coerce`, so the values handed to psycopg2 — and therefore NUMERIC exactness and
            # every adapter — are byte-for-byte what the per-row loop passed. Only the statement count
            # changes: an uploaded sheet cost one network round trip PER ROW, which made request latency
            # scale linearly with the upload (measured: 1000 rows took 138s at a 133ms RTT, 0.26s batched).
            # Paged so a wide 5000-row sheet cannot build one unbounded statement.
            rows = [
                [KnowledgeTableQuery._coerce(rd.get(c["name"]), c["affinity"]) for c in cols]
                for rd in (dict(zip(t["columns"], r)) for r in t["rows"])
            ]
            if rows:
                execute_values(cur, f'INSERT INTO {qident(schema)}.{qident(tname)} VALUES %s',
                               rows, page_size=_UPLOAD_PAGE_SIZE)
            cur.execute(
                'INSERT INTO "chat"."working_table" '
                '(conversation_id, table_name, content_hash, row_count) VALUES (%s, %s, %s, %s) '
                'ON CONFLICT (conversation_id, table_name) DO UPDATE SET '
                'content_hash = EXCLUDED.content_hash, row_count = EXCLUDED.row_count, updated_at = now()',
                (schema, tname, fingerprint, len(t["rows"])),
            )
            request_timing.count("upload_rows", len(t["rows"]))


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
            # _connect keeps the working-table advisory lock for the full world query. Persist the
            # validated source replacement and release that lock only after the reasoning path ends.
            if self.conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_INERROR:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
            self.conn.close()


class _TableQueryPg(TableQuery):
    """Own-data path executor → Postgres (so uploads persist in the user schema and answers are consistent)."""
    _pg_schema = None

    def execute(self, tablemap, sch, sql, query=None, deterministic_plan=None):
        conn = _pg(); cur = conn.cursor()
        try:
            _load_user_schema(cur, self._pg_schema, sch, tablemap)
            if deterministic_plan is not None:
                conn.commit()
                conn.close()
                conn = None
                return self._execute_deterministic(tablemap, deterministic_plan)
            execution_sql = render_query(query, dialect="postgres_numeric") if query is not None else sql
            cur.execute(execution_sql)
            columns, rows = [d[0] for d in cur.description], cur.fetchall()
            conn.commit()
            return columns, rows
        except Exception:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    def _execute_deterministic(self, tablemap, plan):
        from sqlalchemy import create_engine
        from sqlalchemy.pool import NullPool

        from engine.deterministic.context import (
            current_analysis_context,
            set_execution_record,
        )
        from engine.deterministic.service import DeterministicAnalysis

        context = current_analysis_context()
        if context is None:
            raise RuntimeError("deterministic execution requires a named-analysis context")
        engine = create_engine(
            "postgresql+psycopg2://",
            creator=_pg,
            poolclass=NullPool,
        )
        try:
            result = DeterministicAnalysis(
                plan,
                conversation_schema=self._pg_schema,
                dataset_version=context.dataset_version,
            ).run(
                engine,
                mode=deterministic_execution_mode(),
                estimated_rows=sum(
                    len(tablemap[name].get("rows") or ())
                    for name in plan.views[0].tables
                    if name in tablemap
                ),
                python_row_limit=deterministic_python_row_limit(),
                app_env=APP_ENV,
                persist_generated=deterministic_persist_generated(),
                conversation_id=context.conversation_id,
                revision=context.revision,
            )
            set_execution_record(result.record())
            final_view = plan.views[-1]
            if result.rows:
                columns = list(result.rows[0])
            elif hasattr(final_view, "aggregates"):
                columns = [value.name for value in final_view.group_by + final_view.aggregates]
            else:
                columns = [value.name for value in final_view.values]
            rows = [tuple(row.get(column) for column in columns) for row in result.rows]
            return columns, rows
        finally:
            engine.dispose()


class PgQuery(KnowledgeTableQuery):
    """KnowledgeTableQuery planner + Postgres execution, scoped to a per-request verified schema."""

    @staticmethod
    def _numeric_aggregate(function, operand):
        return f"{function.upper()}( {operand} )"

    @staticmethod
    def _numeric_multiply(left, right):
        return f"({left} * {right})"

    @staticmethod
    def _calculation_dialect():
        return "postgres_numeric"

    def __init__(self, deploy_dir):
        super().__init__(deploy_dir)
        self.q11.__class__ = _TableQueryPg       # rebless the shared TableQuery (same loaded model) -> PG own-data path
        self._pg_schema = None
        self._con = None

    def serve(self, tables, question, as_of=None, schema=None, explicit_fks=(), dataset_semantics=()):
        if not schema:
            return {"error": "not signed in (no schema)", "question": question}
        self._pg_schema = schema
        self.q11._pg_schema = schema
        self._con = None
        try:
            return super().serve(tables, question, as_of, explicit_fks=explicit_fks,
                                 dataset_semantics=dataset_semantics)
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
        try:
            _load_user_schema(cur, self._pg_schema, sch, tablemap)
        except Exception:
            conn.rollback()
            conn.close()
            raise
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
