"""master.py — per-user MASTER (reference) data: the user's own "world model" for the PRIVATE entities
Wikidata doesn't know (their products, reps, regions, SKUs...). It's stored in a per-user Postgres schema
`m_<hash(sub)>` in the same `world` database as the wikipedia/world schemas, so it persists across ALL of
that user's conversations.

At reasoning time, relevant_tables loads only references connected to the uploaded data by the planner's own
foreign-key detector. Selected references become ordinary typed planner tables, so the existing AST path joins
uploads + private reference data (+ public world data) without a second SQL implementation.

Security mirrors engine.conversations: the user_id is ALWAYS the verified token subject (engine.auth), never
client-supplied. The schema name is DERIVED from it (md5), so a user can only ever read/write their own
master data — there is no client-controlled schema/table path.
"""
from __future__ import annotations

import hashlib
import logging

from engine.pg import _pg
from engine.relations import discover_fks
from engine.tables import table_from_rows, table_name

MAX_COLS = 64
MAX_ROWS = 50000
LOG = logging.getLogger(__name__)


def master_schema(user_id):
    """The per-user master schema name: `m_<32 hex>` (same safe fixed shape as a conversation's `c_<32 hex>`)."""
    if not user_id:
        raise ValueError("verified user id is required")
    return "m_" + hashlib.md5(
        str(user_id).encode("utf-8"), usedforsecurity=False
    ).hexdigest()


def _identifier(name, label="identifier"):
    value = str(name if name is not None else "").strip()
    if not value:
        raise ValueError(f"{label} cannot be empty")
    if "\x00" in value or len(value.encode("utf-8")) > 63:
        raise ValueError(f"{label} must be at most 63 UTF-8 bytes")
    return value


def _qi(name):
    """Quote a validated PostgreSQL identifier without silently truncating it."""
    return '"' + _identifier(name).replace('"', '""') + '"'


def _validated_table(name, columns, rows):
    """Validate and normalize one reference overwrite before the transaction starts."""
    name = _identifier(name, "table name")
    columns = list(columns or [])
    if not columns:
        raise ValueError("at least one column is required")
    if len(columns) > MAX_COLS:
        raise ValueError(f"reference tables support at most {MAX_COLS} columns")
    columns = [_identifier(column, f"column {index + 1} name") for index, column in enumerate(columns)]
    folded = [column.casefold() for column in columns]
    if len(set(folded)) != len(folded):
        raise ValueError("column names must be unique")
    rows = list(rows or [])
    if len(rows) > MAX_ROWS:
        raise ValueError(f"reference tables support at most {MAX_ROWS} rows")

    normalized = []
    keys = set()
    for index, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            raise ValueError(f"row {index + 1} must be an array")
        values = list(row)[:len(columns)] + [None] * max(0, len(columns) - len(row))
        values = [None if value is None or str(value).strip() == "" else str(value).strip() for value in values]
        if not any(value is not None for value in values):
            continue
        if values[0] is None:
            raise ValueError(f"row {index + 1} is missing its {columns[0]} key")
        key = values[0].casefold()
        if key in keys:
            raise ValueError(f"duplicate {columns[0]} key: {values[0]}")
        keys.add(key)
        normalized.append(values)
    return name, columns, normalized


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
        cur.execute('SELECT * FROM "%s".%s ORDER BY %s LIMIT %s' % (sch, _qi(name), _qi(cols[0]), MAX_ROWS))
        rows = [["" if v is None else str(v) for v in r] for r in cur.fetchall()]
        conn.commit()
        return {"name": name, "columns": cols, "rows": rows}
    finally:
        conn.close()


def load_master_tables(user_id, row_limit=MAX_ROWS):
    """Load all reference tables through one connection for request-time selection."""
    sch = master_schema(user_id)
    limit = max(0, min(int(row_limit), MAX_ROWS))
    conn = _pg()
    try:
        cur = conn.cursor()
        _ensure_schema(cur, sch)
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name", (sch,))
        out = []
        for (name,) in cur.fetchall():
            columns = _cols(cur, sch, name)
            cur.execute('SELECT * FROM "%s".%s ORDER BY %s LIMIT %%s' %
                        (sch, _qi(name), _qi(columns[0])), (limit,))
            rows = [["" if value is None else str(value) for value in row] for row in cur.fetchall()]
            out.append({"name": name, "columns": columns, "rows": rows})
        conn.commit()
        return out
    finally:
        conn.close()


def _execution_values(table, column):
    index = table["columns"].index(column)
    return {row[index] for row in table["rows"] if index < len(row) and row[index] not in (None, "")}


def relevant_tables(user_id, source_tables, limit, row_limit):
    """Return saved references that the production FK graph can join to this request.

    Selection intentionally delegates relationship inference to ``discover_fks``. An additional exact-value
    check keeps selection consistent with the emitted SQL equality join, which is case-sensitive for text.
    Store failures are reported as warnings so an answer is never silently presented as reference-aware.
    """
    if limit <= 0:
        return {"tables": [], "warnings": ["Saved references were not used because the request reached the table limit."]}
    try:
        stored = load_master_tables(user_id, row_limit)
    except Exception:                                        # noqa: BLE001 - degrade to uploaded data, but disclose it
        LOG.exception("saved reference data could not be loaded")
        return {"tables": [], "warnings": ["Saved reference data was unavailable; the answer used uploaded tables only."]}

    selected = []
    warnings = []
    used_names = {table["name"] for table in source_tables}
    candidates = []
    for full in stored:
        name = table_name(full.get("name"), len(source_tables) + len(candidates))
        if name in used_names:
            continue
        if len(full.get("columns") or []) < 2 or not full.get("rows"):
            continue
        candidate = table_from_rows(name, full["columns"], full["rows"][:row_limit])
        used_names.add(name)
        candidates.append((full["name"], candidate))

    remaining = candidates
    normalized_only = set()
    while remaining and len(selected) < limit:
        working = [*source_tables, *selected]
        working_names = {table["name"] for table in working}
        added = []
        for stored_name, candidate in remaining:
            edges = [edge for edge in discover_fks([*working, candidate])
                     if edge["from_table"] in working_names and edge["to_table"] == candidate["name"]
                     and edge["to_col"] == candidate["columns"][0]]
            exact = False
            for edge in edges:
                source = next(table for table in working if table["name"] == edge["from_table"])
                child = _execution_values(source, edge["from_col"])
                parent = _execution_values(candidate, edge["to_col"])
                if child and len(child & parent) / len(child) >= 0.9:
                    exact = True
                    break
            if exact:
                selected.append(candidate)
                added.append(candidate["name"])
                normalized_only.discard(stored_name)
                if len(selected) >= limit:
                    break
            elif edges:
                normalized_only.add(stored_name)
        if not added:
            break
        remaining = [(stored_name, candidate) for stored_name, candidate in remaining
                     if candidate["name"] not in added]

    for stored_name in sorted(normalized_only):
        warnings.append(f'Saved reference "{stored_name}" matched only after text normalization and was not used.')
    return {"tables": selected, "warnings": warnings}


def save_master(user_id, name, columns, rows):
    """Create-or-REPLACE a master table (drop + create + insert — a full overwrite of that reference table).
    All columns are stored as text; the FIRST column is the key that links to the user's data (the join key
    ``relevant_tables`` matches against uploaded columns)."""
    name, columns, rows = _validated_table(name, columns, rows)
    sch = master_schema(user_id)
    conn = _pg()
    try:
        try:
            cur = conn.cursor()
            _ensure_schema(cur, sch)
            tq = _qi(name)
            cur.execute('DROP TABLE IF EXISTS "%s".%s' % (sch, tq))
            definitions = [_qi(column) + " text" + (" PRIMARY KEY" if index == 0 else "")
                           for index, column in enumerate(columns)]
            cur.execute('CREATE TABLE "%s".%s (%s)' % (sch, tq, ", ".join(definitions)))
            if rows:
                ph = "(" + ",".join(["%s"] * len(columns)) + ")"
                cur.executemany('INSERT INTO "%s".%s VALUES %s' % (sch, tq, ph), rows)
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
