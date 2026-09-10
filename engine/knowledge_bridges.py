"""Persistence and execution for connected and semantic request-local bridges."""
from __future__ import annotations

import hashlib
import re

import numpy as np
from psycopg2.extras import execute_values

from engine import request_timing
from engine.embeddings import pgvector_literal
from engine.knowledge_tables import KnowledgeTableQuery
from engine.pg import _PGTYPE, _pg
from engine.tables import qident, qlit


def _norm_vec(value):
    vector = np.asarray(value, np.float32)
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 1e-9 else vector


class KnowledgeBridgeMixin:
    """Own the physical bridge tables and hybrid pgvector query path."""

    CONN_DDL = (
        '("column" TEXT, "value" TEXT, "world_type" TEXT, "world_key" TEXT, '
        '"country" TEXT, "world_qid" TEXT, "entity_qid" TEXT, "context" TEXT)'
    )
    TYPE_QID = {
        "city": "Q515", "country": "Q6256", "state": "Q35657",
        "continent": "Q5107", "element": "Q11344",
    }

    def _conn_bridge_name(self, main_table):
        return f"{main_table} connected to knowledgebase"

    def _materialize(self, inner_sql):
        cursor = self._rconn().cursor()
        cursor.execute(f"SELECT b.cell, b.wk FROM ({inner_sql}) AS b(cell, wk)")
        return cursor.fetchall()

    # ---- bridge state hash: rebuild only what changed ---------------------------------------------------------
    # The connected bridge is a pure function of (route column, world type, resolved pairs) and the
    # world snapshot they were resolved against. A follow-up question on unchanged tables used to
    # re-run the whole persist anyway — DROP legacy, CREATE, catalog check, DELETE, one INSERT per
    # row, COMMIT — per routed column, per turn. The hash gate skips that rewrite when nothing it
    # depends on changed; the resolution slides still stream (the browser expects them every turn).
    _BRIDGE_STATE = "_bridge_state"                       # per-conversation-schema hash ledger

    def _bridge_world_version(self):
        """Salt for the state hash: the world data's newest refresh + the model revision set. Either
        moving invalidates every stored bridge hash, so a resync or a promoted model rebuilds."""
        stamp = self._kb_rows(
            "SELECT COALESCE(max(last_refreshed_at)::text, '') FROM knowledgebase.\"schedule\"")
        from engine import model_revisions
        revisions = sorted((k, str(v)) for k, v in vars(model_revisions).items() if k.isupper())
        return f"{stamp[0][0] if stamp else ''}|{revisions}"

    def _bridge_state_hash(self, route_column, world_type, pairs):
        try:
            payload = repr(("entity-qid-v2", route_column, world_type, sorted({(c, k) for c, k in pairs if k}),
                            self._bridge_world_version()))
            return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()
        except Exception:                                 # noqa: BLE001 — no hash -> just rebuild
            return None

    def _bridge_rows(self, cursor, route_column, world_type, pairs):
        """The bridge rows for `pairs` — shared by the write path and the reuse path (slides only)."""
        keys = sorted({key for _, key in pairs if key})
        countries = {}
        if keys and world_type == "city":
            cursor.execute(
                'SELECT qid, canon_country FROM knowledgebase."words" '
                "WHERE type='city' AND qid = ANY(%s)",
                (keys,),
            )
            for qid, country in cursor.fetchall():
                if country and not countries.get(qid):
                    countries[qid] = country
        elif keys:
            cursor.execute(
                'SELECT canonical, canon_country FROM knowledgebase."words" '
                "WHERE type=%s AND canonical = ANY(%s)",
                (world_type, keys),
            )
            countries = {canonical: (country or canonical)
                         for canonical, country in cursor.fetchall()}
            if world_type == "country":
                for key in keys:
                    countries.setdefault(key, key)
        type_qid = self.TYPE_QID.get(world_type)
        entity_qids = {key if world_type == "city" else key.lower(): key
                       for key in keys if re.fullmatch(r"Q\d+", key)}
        if keys and world_type != "city":
            # Match the existing name-bridge's canonical -> QID tie rule exactly.
            cursor.execute(
                'SELECT DISTINCT ON (lower(canonical)) lower(canonical), qid '
                'FROM knowledgebase."words" WHERE type=%s AND lower(canonical)=ANY(%s) '
                'AND qid IS NOT NULL ORDER BY lower(canonical), qid',
                (world_type, [key.lower() for key in keys]),
            )
            entity_qids.update(cursor.fetchall())
        seen = set()
        rows = []
        for cell, key in pairs:
            if key and (cell, key) not in seen:
                seen.add((cell, key))
                cell, context = cell if isinstance(cell, tuple) else (cell, "")
                rows.append((route_column, cell, world_type, key, countries.get(key), type_qid,
                             entity_qids.get(key if world_type == "city" else key.lower()), context))
        return rows

    def _persist_connected(self, main_table, route_column, world_type, pairs):
        schema = self._pg_schema
        bridge_name = self._conn_bridge_name(main_table)
        cursor = self._rconn().cursor()
        select_sql = (
            f'SELECT "value", "world_key" FROM {qident(schema)}.{qident(bridge_name)} '
            f'WHERE "column" = {qlit(route_column)}'
        )
        state_hash = self._bridge_state_hash(route_column, world_type, pairs)
        if state_hash:
            # Fresh = same hash recorded AND the bridge table actually exists (a dropped table with
            # a surviving ledger row must rebuild, not serve a SELECT against nothing). The ledger
            # table is probed via to_regclass FIRST — naming it in a query before it exists is a
            # plan-time error, not an empty result.
            cursor.execute(
                "SELECT to_regclass(%s) IS NOT NULL AND to_regclass(%s) IS NOT NULL",
                (f'"{schema}"."{self._BRIDGE_STATE}"', f'"{schema}"."{bridge_name}"'),
            )
            if cursor.fetchone()[0]:
                cursor.execute(
                    f'SELECT "hash" FROM {qident(schema)}.{qident(self._BRIDGE_STATE)} '
                    'WHERE "bridge" = %s AND "column" = %s',
                    (bridge_name, route_column),
                )
                row = cursor.fetchone()
                if row and row[0] == state_hash:
                    self._emit_resolutions(self._bridge_rows(cursor, route_column, world_type, pairs))
                    request_timing.count("bridge_reuse_n")
                    return select_sql
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {qident(schema)}")
        legacy_bridge = f"{main_table} connected to wikipedia"          # pre-rename bridge in existing conversations
        cursor.execute(f"DROP TABLE IF EXISTS {qident(schema)}.{qident(legacy_bridge)}")
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {qident(schema)}.{qident(bridge_name)} {self.CONN_DDL}"
        )
        cursor.execute(f'ALTER TABLE {qident(schema)}.{qident(bridge_name)} '
                       'ADD COLUMN IF NOT EXISTS "entity_qid" TEXT')
        cursor.execute(f'ALTER TABLE {qident(schema)}.{qident(bridge_name)} '
                       'ADD COLUMN IF NOT EXISTS "context" TEXT')
        cursor.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
            "AND column_name='world_qid'",
            (schema, bridge_name),
        )
        if not cursor.fetchone():
            cursor.execute(
                f'ALTER TABLE {qident(schema)}.{qident(bridge_name)} '
                'ADD COLUMN IF NOT EXISTS "world_qid" TEXT'
            )
        cursor.execute(
            f'DELETE FROM {qident(schema)}.{qident(bridge_name)} WHERE "column" = %s',
            (route_column,),
        )
        rows = self._bridge_rows(cursor, route_column, world_type, pairs)
        if rows:
            # ONE paged multi-row INSERT — the same per-row-round-trip fix as the sheet upload.
            execute_values(
                cursor,
                f'INSERT INTO {qident(schema)}.{qident(bridge_name)} '
                '("column", "value", "world_type", "world_key", "country", "world_qid", "entity_qid", "context") VALUES %s',
                rows, page_size=500,
            )
        if state_hash:
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {qident(schema)}.{qident(self._BRIDGE_STATE)} "
                '("bridge" TEXT, "column" TEXT, "hash" TEXT, PRIMARY KEY ("bridge", "column"))'
            )
            cursor.execute(
                f"INSERT INTO {qident(schema)}.{qident(self._BRIDGE_STATE)} VALUES (%s,%s,%s) "
                'ON CONFLICT ("bridge", "column") DO UPDATE SET "hash" = EXCLUDED."hash"',
                (bridge_name, route_column, state_hash),
            )
        self._rconn().commit()
        self._emit_resolutions(rows)
        return select_sql

    @staticmethod
    def _emit_resolutions(rows):
        """Stream a bounded, best-effort cell-to-world trace."""
        try:
            from engine.trace import ctx_emit

            unsafe = str.maketrans({".": "_", "$": "_", "#": "_", "[": "_", "]": "_", "/": "_"})
            resolved = {}
            seen = set()
            for _column, cell, _world_type, key, country, _type_qid, *_entity_qid in rows:
                text = str(cell)
                if text in seen:
                    continue
                seen.add(text)
                resolved[text.translate(unsafe)[:120] or "_"] = (
                    f"{key} · {country}" if country and country != key else str(key)
                )
                if len(resolved) >= 24:
                    break
            if resolved:
                ctx_emit("resolve", resolved, merge=True)
        except Exception:  # noqa: BLE001 - trace transport must not change an answer
            pass

    def _city_bridge_sql(self, norm, main_table, route_column, context_country):
        inner = super()._city_bridge_sql(norm, main_table, route_column, context_country)
        if not inner:
            return None
        return self._persist_connected(
            main_table, route_column, "city", self._materialize(inner),
        )

    def _cell_bridge_sql(self, norm, main_table, route_column, world_type, context_country=None):
        inner = super()._cell_bridge_sql(
            norm, main_table, route_column, world_type, context_country,
        )
        if not inner:
            return None
        return self._persist_connected(
            main_table, route_column, world_type, self._materialize(inner),
        )

    def _city_bridge_disamb_sql(self, norm, main_table, route_column, country_column):
        inner = super()._city_bridge_disamb_sql(norm, main_table, route_column, country_column)
        if not inner:
            return None
        cursor = self._rconn().cursor()
        cursor.execute(f"SELECT * FROM ({inner}) AS resolved(cell, context, qid)")
        pairs = [((cell, context), qid) for cell, context, qid in cursor.fetchall()]
        self._persist_connected(main_table, route_column, "city", pairs)
        return (f'SELECT "value", "context", "entity_qid" FROM '
                f'{qident(self._pg_schema)}.{qident(self._conn_bridge_name(main_table))} '
                f'WHERE "column"={qlit(route_column)}')

    def _persist_main_unconnected(self, cursor, schema, table, planner_schema, plan):
        table_name = table["name"]
        columns = table["columns"]
        rows = table["rows"]
        affinities = {
            column["name"]: column["affinity"]
            for column in planner_schema if column["table"] == table_name
        }
        cursor.execute(f"DROP TABLE IF EXISTS {qident(schema)}.{qident(table_name)} CASCADE")
        definitions = ['"__pk" BIGINT'] + [
            f'{qident(column)} {_PGTYPE.get(affinities.get(column, "TEXT"), "TEXT")}'
            for column in columns
        ]
        cursor.execute(
            f"CREATE TABLE {qident(schema)}.{qident(table_name)} ({', '.join(definitions)})"
        )
        placeholders = ",".join(["%s"] * (len(columns) + 1))
        insert = f"INSERT INTO {qident(schema)}.{qident(table_name)} VALUES ({placeholders})"
        for primary_key, row in enumerate(rows):
            values = [
                KnowledgeTableQuery._coerce(row[index], affinities.get(columns[index], "TEXT"))
                for index in range(len(columns))
            ]
            cursor.execute(insert, [primary_key, *values])

        unconnected = f"{table_name} unconnected to knowledgebase"
        legacy_unconnected = f"{table_name} unconnected to wikipedia"      # pre-rename bridge in existing conversations
        cursor.execute(f"DROP TABLE IF EXISTS {qident(schema)}.{qident(legacy_unconnected)}")
        cursor.execute(f"DROP TABLE IF EXISTS {qident(schema)}.{qident(unconnected)}")
        cursor.execute(
            f"CREATE TABLE {qident(schema)}.{qident(unconnected)} "
            f'("__pk" BIGINT, "column" TEXT, "value" TEXT, "embedding" vector({self.hdim}))'
        )
        insert_vector = (
            f"INSERT INTO {qident(schema)}.{qident(unconnected)} VALUES (%s,%s,%s,%s::vector)"
        )
        for column in plan["unconn"]:
            index = columns.index(column)
            texts = ["" if row[index] is None else str(row[index]) for row in rows]
            vectors = self._encode(texts)
            for primary_key, (text, vector) in enumerate(zip(texts, vectors)):
                if text.strip():
                    cursor.execute(
                        insert_vector,
                        [primary_key, column, text, pgvector_literal(_norm_vec(vector))],
                    )

    def _serve_hybrid(self, norm, fks, planner_schema, question, predicate, plan,
                      country, as_of, schema):
        del fks
        table = plan["table"]
        table_name = table["name"]
        self._pg_schema = schema
        predicate_vector = pgvector_literal(_norm_vec(self._encode([predicate])[0]))
        connected = self._conn_bridge_name(table_name)
        unconnected = f"{table_name} unconnected to knowledgebase"
        route_column = plan["conn"][0][0] if plan["conn"] else None
        connection = _pg()
        try:
            cursor = connection.cursor()
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {qident(schema)}")
            cursor.execute(f"SET search_path TO {qident(schema)}, knowledgebase, public")
            self._persist_main_unconnected(cursor, schema, table, planner_schema, plan)
            connection.commit()
            for column, world_type in plan["conn"]:
                if world_type == "city":
                    self._city_bridge_sql(norm, table_name, column, country)
                elif world_type:
                    self._cell_bridge_sql(norm, table_name, column, world_type, country)
            display = ", ".join(f"m.{qident(column)}" for column in table["columns"])
            sql = (
                f"SELECT {display} FROM {qident(schema)}.{qident(table_name)} m "
                f"JOIN {qident(schema)}.{qident(unconnected)} u ON u.\"__pk\" = m.\"__pk\" "
                f"AND u.\"column\" = {qlit(plan['freetext'])} "
            )
            if country and route_column:
                sql += (
                    f"WHERE EXISTS (SELECT 1 FROM {qident(schema)}.{qident(connected)} c "
                    f"WHERE c.\"column\" = {qlit(route_column)} "
                    f"AND lower(c.\"value\") = lower(m.{qident(route_column)}) "
                    f"AND c.\"country\" = {qlit(country)}) "
                )
            sql += f'ORDER BY u."embedding" <=> %s::vector LIMIT {self.HYBRID_LIMIT}'
            cursor.execute(sql, [predicate_vector])
            result_columns = [description[0] for description in cursor.description]
            result_rows = [["" if value is None else value for value in row]
                           for row in cursor.fetchall()]
            connection.commit()
        finally:
            connection.close()

        display_sql = (
            f"SELECT {display} FROM {qident(table_name)} m "
            f"JOIN {qident(unconnected)} u ON u.\"__pk\"=m.\"__pk\" "
            f"AND u.\"column\"={qlit(plan['freetext'])} "
        )
        if country and route_column:
            display_sql += (
                f"WHERE EXISTS (SELECT 1 FROM {qident(connected)} c "
                f"WHERE lower(c.\"value\")=lower(m.{qident(route_column)}) "
                f"AND c.\"country\"={qlit(country)}) "
            )
        display_sql += f"ORDER BY u.\"embedding\" <=> embed({predicate!r}) LIMIT {self.HYBRID_LIMIT}"
        return {
            "question": question, "as_of": as_of, "sql": display_sql,
            "result": {"columns": result_columns, "rows": result_rows}, "error": None,
            "routed": {"table": table_name, "freetext_col": plan["freetext"],
                       "connected": [column for column, _ in plan["conn"]]},
            "meaning_join": {"country": country, "predicate": predicate,
                             "connected_bridge": connected, "unconnected_bridge": unconnected},
            "provenance": None, "warnings": [], "dims": None,
            "model": "engine - unified encoder: persisted world bridge + semantic pgvector rank",
        }
