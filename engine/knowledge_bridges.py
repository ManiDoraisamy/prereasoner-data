"""Persistence and execution for connected and semantic request-local bridges."""
from __future__ import annotations

import numpy as np

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
        '"country" TEXT, "world_qid" TEXT)'
    )
    TYPE_QID = {
        "city": "Q515", "country": "Q6256", "state": "Q35657",
        "continent": "Q5107", "element": "Q11344",
    }

    def _conn_bridge_name(self, main_table):
        return f"{main_table} connected to wikipedia"

    def _materialize(self, inner_sql):
        cursor = self._rconn().cursor()
        cursor.execute(f"SELECT b.cell, b.wk FROM ({inner_sql}) AS b(cell, wk)")
        return cursor.fetchall()

    def _persist_connected(self, main_table, route_column, world_type, pairs):
        schema = self._pg_schema
        bridge_name = self._conn_bridge_name(main_table)
        cursor = self._rconn().cursor()
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {qident(schema)}")
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {qident(schema)}.{qident(bridge_name)} {self.CONN_DDL}"
        )
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
        seen = set()
        rows = []
        for cell, key in pairs:
            if key and (cell, key) not in seen:
                seen.add((cell, key))
                rows.append((route_column, cell, world_type, key, countries.get(key), type_qid))
        if rows:
            cursor.executemany(
                f"INSERT INTO {qident(schema)}.{qident(bridge_name)} VALUES (%s,%s,%s,%s,%s,%s)",
                rows,
            )
        self._rconn().commit()
        self._emit_resolutions(rows)
        return (
            f'SELECT "value", "world_key" FROM {qident(schema)}.{qident(bridge_name)} '
            f'WHERE "column" = {qlit(route_column)}'
        )

    @staticmethod
    def _emit_resolutions(rows):
        """Stream a bounded, best-effort cell-to-world trace."""
        try:
            from engine.trace import ctx_emit

            unsafe = str.maketrans({".": "_", "$": "_", "#": "_", "[": "_", "]": "_", "/": "_"})
            resolved = {}
            seen = set()
            for _column, cell, _world_type, key, country, _type_qid in rows:
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

        unconnected = f"{table_name} unconnected to wikipedia"
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
        unconnected = f"{table_name} unconnected to wikipedia"
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
