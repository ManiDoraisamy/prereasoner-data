"""Connected and semantic bridges, in the request's database (engine/prepared.py)."""
from __future__ import annotations

import re
from datetime import date

import numpy as np

from engine.prepared import preparations, vector_blob
from engine.tables import qident, qlit

ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")      # a day a date column names, whose exchange rates a conversion reads


def _norm_vec(value):
    vector = np.asarray(value, np.float32)
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 1e-9 else vector


class KnowledgeBridgeMixin:
    """Own the bridge tables a world join reads and the hybrid search over free text."""

    TYPE_QID = {
        "city": "Q515", "country": "Q6256", "state": "Q35657",
        "continent": "Q5107", "element": "Q11344",
    }

    def _conn_bridge_name(self, main_table):
        return f"{main_table} connected to knowledgebase"

    def _bridge_select(self, main_table, route_column, fields=("value", "world_key"), names=None):
        """The SELECT of one routed column's bridge rows: the cell and what it names, each ``fields`` column under
        its name in ``names`` when given (SQLite names a subquery's columns only by their aliases)."""
        selected = ", ".join(qident(field) + (f" AS {qident(name)}" if name else "")
                             for field, name in zip(fields, names or (None,) * len(fields)))
        return (f'SELECT {selected} FROM main.{qident(self._conn_bridge_name(main_table))} '
                f'WHERE "column" = {qlit(route_column)}')

    def _bridge_rows(self, table, route_column, world_type, pairs):
        """The bridge rows for ``pairs`` [(cell or (cell, row country), key)]: (column, value, world_type, world_key,
        country, world_qid, entity_qid, context). A city's key is its qid; another type's key is its canonical name,
        whose country and qid its table's preparation holds (engine/prepared.py)."""
        bridge = self._prepared(table).columns.get(route_column, {})
        type_qid = self.TYPE_QID.get(world_type)
        seen = set()
        rows = []
        for cell, key in pairs:
            if not key or (cell, key) in seen:
                continue
            seen.add((cell, key))
            cell, context = cell if isinstance(cell, tuple) else (cell, "")
            if world_type == "city":
                country, entity = bridge.get("countries", {}).get(key), None
            else:
                # A canonical name's country, else the name itself; a country no row names is itself.
                named = bridge.get("names", {}).get(key)
                country = named["country"] if named else (key if world_type == "country" else None)
                entity = named["qid"] if named else None
            if entity is None and re.fullmatch(r"Q\d+", key):
                entity = key                                 # a city's or a non-geographic entity's key is its qid
            rows.append((route_column, cell, world_type, key, country, type_qid, entity, context))
        return rows

    def _persist_connected(self, table, route_column, world_type, pairs):
        """Hold the bridge rows of ``pairs`` for ``route_column`` of ``table`` in the request's database, stream the
        matches, and return the SELECT of the cell and its key."""
        rows = self._bridge_rows(table, route_column, world_type, pairs)
        self._database().replace_bridge(self._conn_bridge_name(table["name"]), route_column, rows)
        self._emit_resolutions(rows)
        return self._bridge_select(table["name"], route_column)

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

    @staticmethod
    def _table(norm, main_table):
        return next((table for table in norm if table["name"] == main_table), None)

    def _city_bridge_sql(self, norm, main_table, route_column):
        pairs = self._city_pairs(norm, main_table, route_column)
        if not pairs:
            return None
        return self._persist_connected(self._table(norm, main_table), route_column, "city", pairs)

    def _cell_bridge_sql(self, norm, main_table, route_column, world_type):
        """The bridge of a country, state or element column."""
        pairs = self._name_pairs(norm, main_table, route_column)
        if not pairs:
            return None
        return self._persist_connected(self._table(norm, main_table), route_column, world_type, pairs)

    def _city_bridge_disamb_sql(self, norm, main_table, route_column, country_column):
        pairs = self._city_disamb_pairs(norm, main_table, route_column, country_column)
        if not pairs:
            return None
        self._persist_connected(self._table(norm, main_table), route_column, "city", pairs)
        return self._bridge_select(main_table, route_column, ("value", "context", "entity_qid"))

    def link_request(self, as_of=None, explicit_fks=()):
        """Register this request's links (``request_links``) on its database, built when the own-data path first reads
        the request's workbook (engine/prepared.py RequestDatabase.links). ``as_of``: the day a money column of a table
        with no date column is converted on, today when the request names none."""
        day = str(as_of) if as_of else date.today().isoformat()
        self._database().link_with(lambda tables: self.request_links(tables, day, explicit_fks))

    def request_links(self, tables, as_of, explicit_fks=()):
        """What production connects for the request's ``tables`` (engine/prepared.py RequestLinks), each table named by
        its tab: every routed column (its preparation's routes) with its bridge, built here with production's own
        pairing (``_city_bridge_sql``, ``_cell_bridge_sql``); every column of named entities its preparation types,
        not routed as a place, with the bridge of its cells' matches, exact and nearest (``_nongeo_matches``); the
        single-column keys between the tables (engine/prepared.py table_links over ``ingest``'s relationships); and each table's money columns in its rows' currencies
        (engine/currency_intent.py), with the rates of every day its date column names and of ``as_of`` loaded on the
        request's database. Reads the knowledgebase."""
        from engine.currency_intent import is_currency_measure_column, is_currency_source_column
        from engine.dataset_semantics import is_synthetic_currency_column
        from engine.entities import WORLD_TABLE_TYPE
        from engine.prepared import EntityLink, MoneyLink, PlaceLink, RequestLinks, table_links

        database = self._database()
        norm, fks = self.ingest(tables, explicit_fks=explicit_fks)
        tab = {table["name"]: str(table["grid"]["name"]) for table in norm}
        routes = {}
        for table in norm:
            routes.update(self.route(table))
        places = []
        for (table_name, column), world in sorted(routes.items()):
            table = self._table(norm, table_name)
            world_type = WORLD_TABLE_TYPE.get(world)
            select = (self._city_bridge_sql(norm, table_name, column) if world_type == "city"
                      else self._cell_bridge_sql(norm, table_name, column, world_type) if world_type else None)
            bridge = self._conn_bridge_name(table_name)
            matched = database.connection.execute(
                f'SELECT COUNT(DISTINCT "value") FROM main.{qident(bridge)} WHERE "column" = ?', (column,)).fetchone()[0] if select else 0
            places.append(PlaceLink(tab[table_name], column, table["columns"].index(column), world_type, bridge, bool(select),
                                    len(self._cells(table, column)), matched))
        entities = []
        for table in norm:
            preparation = self._prepared(table)
            for column, held in sorted(preparation.nongeo.items()):
                placed = (table["name"], column) in routes
                bridged, matched, entity_table, has_country = False, 0, "", False
                if not placed:     # a routed column's bridge is its place's: one bridge per column
                    pairs = [(cell, qid) for cell, qid in sorted(self._nongeo_matches(table, column).items()) if qid]
                    if pairs:
                        self._persist_connected(table, column, held["label"], pairs)
                        entity_table = held["label"][:63]
                        columns = {row[1] for row in database.connection.execute(
                            f"PRAGMA knowledgebase.table_info({qident(entity_table)})").fetchall()}
                        bridged, matched, has_country = True, len(pairs), {"country", "qid"} <= columns
                entities.append(EntityLink(tab[table["name"]], column, table["columns"].index(column), str(held.get("label") or ""),
                                           entity_table, self._conn_bridge_name(table["name"]), placed, bridged, matched, has_country))
        own = table_links(norm, fks)
        sch, _colidx, _tablemap = self.schema(norm, fks)
        rate_columns = tuple(sorted(column for column in self.words.get("exchange_rate", {}).get("columns", [])
                                    if column.startswith("rate_to_")))
        money = []
        for table in norm:
            mine = [column for column in sch if column["table"] == table["name"]]
            currency = next((column["name"] for column in mine
                             if is_currency_source_column(column["name"]) and not is_synthetic_currency_column(column["name"])), None)
            measures = [column["name"] for column in mine
                        if column.get("affinity") in ("INTEGER", "REAL") and is_currency_measure_column(column["name"])]
            if not currency or not measures or not rate_columns:
                continue
            dated = next((column["name"] for column in mine if column.get("is_date")), None)
            days = {as_of}
            if dated:
                index = table["columns"].index(dated)
                days |= {str(row[index]) for row in table["rows"]
                         if index < len(row) and row[index] is not None and ISO_DAY.fullmatch(str(row[index]))}
            database.load_rates(list(rate_columns), sorted(days))
            money.append(MoneyLink(tab[table["name"]], table["columns"].index(currency), table["columns"].index(dated) if dated else None,
                                   tuple(table["columns"].index(name) for name in measures), rate_columns, as_of))
        return RequestLinks(own.columns, tuple(places), tuple(entities), own.keys, tuple(money))

    def _hold_unconnected(self, database, table, planner_schema, plan):
        """Hold the searched table with a row key, and one vector per free-text cell, in the request's database.
        A column's vectors are kept with the table's preparation in this process (engine/prepared.py)."""
        table_name = table["name"]
        columns = table["columns"]
        rows = table["rows"]
        keyed = f"{table_name} searched"
        affinities = {
            column["name"]: column["affinity"]
            for column in planner_schema if column["table"] == table_name
        }
        connection = database.connection
        connection.execute(f"DROP TABLE IF EXISTS main.{qident(keyed)}")
        definitions = ['"__pk" INTEGER'] + [
            f'{qident(column)} '
            f'{"TEXT COLLATE decimal" if affinities.get(column) == "REAL" else affinities.get(column, "TEXT")}'
            for column in columns
        ]
        connection.execute(f"CREATE TABLE main.{qident(keyed)} ({', '.join(definitions)})")
        from engine.prepared import _cell
        connection.executemany(
            f"INSERT INTO main.{qident(keyed)} VALUES ({','.join('?' * (len(columns) + 1))})",
            ([primary_key, *(_cell(row[index] if index < len(row) else None, affinities.get(columns[index], "TEXT"))
                             for index in range(len(columns)))]
             for primary_key, row in enumerate(rows)))
        unconnected = f"{table_name} unconnected to knowledgebase"
        connection.execute(f"DROP TABLE IF EXISTS main.{qident(unconnected)}")
        connection.execute(f'CREATE TABLE main.{qident(unconnected)} '
                           '("__pk" INTEGER, "column" TEXT, "value" TEXT, "embedding" BLOB)')
        preparation = self._prepared(table)
        for column in plan["unconn"]:
            index = columns.index(column)
            texts = ["" if row[index] is None else str(row[index]) for row in rows]
            # The column's vectors are encoded once per table version in this process and kept with its
            # preparation: a free-text column larger than the encoder's cache was encoded again by every search.
            vectors = preparation.extras.get(("vectors", column))
            if vectors is None:
                vectors = self._encode(texts)
                preparations().keep_extra(preparation, ("vectors", column), vectors, vectors.nbytes)
            connection.executemany(
                f"INSERT INTO main.{qident(unconnected)} VALUES (?, ?, ?, ?)",
                ([primary_key, column, text, vector_blob(_norm_vec(vector))]
                 for primary_key, (text, vector) in enumerate(zip(texts, vectors)) if text.strip()))
        connection.commit()
        return keyed, unconnected

    def _serve_hybrid(self, norm, fks, planner_schema, question, predicate, plan,
                      country, as_of, schema, mention=(), filters=()):
        """World filter + semantic rank over the free-text column.

        ``mention`` holds the search's content words with their plural forms. When the sheet's free text
        contains every one of them ('trench coat' in 'Gabardine Trench Coat'), the search is literal: only
        the rows that mention them answer it, and the similarity orders them. With no verbatim mention
        anywhere in the sheet, the search stays semantic and ranks every row that passes the world filter.
        ``filters`` are the cell values the question quotes, as (column, value) on this sheet: exact
        filters ('in Paris'), never search words. ``country`` is the named country's label.
        """
        del fks, schema
        table = plan["table"]
        table_name = table["name"]
        freetext = plan["freetext"]
        predicate_vector = vector_blob(_norm_vec(self._encode([predicate])[0]))
        connected = self._conn_bridge_name(table_name)
        route_column = plan["conn"][0][0] if plan["conn"] else None
        text_index = table["columns"].index(freetext)

        def mentions(value):
            words = set(re.findall(r"[a-z0-9]+", str(value or "").lower()))
            return all(words & set(forms) for _word, forms in mention)

        literal = bool(mention) and any(mentions(row[text_index]) for row in table["rows"])
        patterns = [r"\y(" + "|".join(forms) + r")\y" for _word, forms in mention] if literal else []
        database = self._database()
        keyed, unconnected = self._hold_unconnected(database, table, planner_schema, plan)
        for column, world_type in plan["conn"]:
            if world_type == "city":
                self._city_bridge_sql(norm, table_name, column)
            elif world_type:
                self._cell_bridge_sql(norm, table_name, column, world_type)
        display = ", ".join(f"m.{qident(column)}" for column in table["columns"])
        conditions = []
        if country and route_column and database.has_table(connected):
            conditions.append(
                f"EXISTS (SELECT 1 FROM main.{qident(connected)} c "
                f"WHERE c.\"column\" = {qlit(route_column)} "
                f"AND lower(c.\"value\") = lower(m.{qident(route_column)}) "
                f"AND c.\"country\" = {qlit(country)})"
            )
        conditions += [f"lower(m.{qident(column)}) = lower(?)" for column, _value in filters]
        conditions += [f"regexp_matches(m.{qident(freetext)}, ?)" for _ in patterns]
        sql = (
            f"SELECT {display} FROM main.{qident(keyed)} m "
            f"JOIN main.{qident(unconnected)} u ON u.\"__pk\" = m.\"__pk\" "
            f"AND u.\"column\" = {qlit(freetext)} "
            + (f"WHERE {' AND '.join(conditions)} " if conditions else "")
            # The row key breaks a tie in similarity, so equal scores keep the sheet's order.
            + f'ORDER BY vector_distance(u."embedding", ?), m."__pk" LIMIT {self.HYBRID_LIMIT}'
        )
        cursor = database.connection.execute(
            sql, [*(value for _column, value in filters), *patterns, predicate_vector])
        result_columns = [description[0] for description in cursor.description]
        result_rows = [["" if value is None else value for value in row] for row in cursor.fetchall()]

        shown = []
        if country and route_column:
            shown.append(
                f"EXISTS (SELECT 1 FROM {qident(connected)} c "
                f"WHERE lower(c.\"value\")=lower(m.{qident(route_column)}) "
                f"AND c.\"country\"={qlit(country)})"
            )
        shown += [f"lower(m.{qident(column)}) = lower({qlit(value)})" for column, value in filters]
        shown += [f"m.{qident(freetext)} ~* {qlit(pattern)}" for pattern in patterns]
        display_sql = (
            f"SELECT {display} FROM {qident(table_name)} m "
            f"JOIN {qident(unconnected)} u ON u.\"__pk\"=m.\"__pk\" "
            f"AND u.\"column\"={qlit(freetext)} "
            + (f"WHERE {' AND '.join(shown)} " if shown else "")
            + f"ORDER BY u.\"embedding\" <=> embed({predicate!r}) LIMIT {self.HYBRID_LIMIT}"
        )
        return {
            "question": question, "as_of": as_of, "sql": display_sql,
            "result": {"columns": result_columns, "rows": result_rows}, "error": None,
            "routed": {"table": table_name, "freetext_col": freetext,
                       "connected": [column for column, _ in plan["conn"]]},
            "meaning_join": {"country": country, "predicate": predicate,
                             "mentions": [word for word, _forms in mention] if literal else [],
                             "filters": [f"{column} = {value}" for column, value in filters],
                             "connected_bridge": connected, "unconnected_bridge": unconnected},
            "provenance": None, "warnings": [], "dims": None,
            "model": "engine - unified encoder: world bridge + semantic rank",
        }
